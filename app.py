import os
import re
import secrets
from datetime import date, timedelta
from functools import wraps

import snowflake.connector
import pandas as pd
import io
from flask import Flask, render_template, jsonify, request, redirect, url_for, session, send_file

SNOWFLAKE_ACCOUNT = "lalywxa-ja51028"

# Token mode: when SNOWFLAKE_TOKEN (a programmatic access token) is set, the app
# connects as SNOWFLAKE_USER with that token and skips the browser login.
SNOWFLAKE_TOKEN = os.environ.get("SNOWFLAKE_TOKEN")
SNOWFLAKE_USER  = os.environ.get("SNOWFLAKE_USER")
TOKEN_MODE      = bool(SNOWFLAKE_TOKEN)

if TOKEN_MODE and not SNOWFLAKE_USER:
    raise RuntimeError("SNOWFLAKE_USER must be set when SNOWFLAKE_TOKEN is set.")

# Sample mode: when started with --sample-data (or SAMPLE_DATA=1), the app reads
# made-up data from sample_data/ via sample_db.py and never contacts Snowflake.
SAMPLE_MODE = os.environ.get("SAMPLE_DATA", "").lower() in ("1", "true", "yes")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))


def default_date_range():
    today = date.today()
    return (today - timedelta(days=30)).isoformat(), today.isoformat()

# In-memory connection store — keyed by session id (localhost only)
_connections: dict = {}

# Single shared connection used in token mode
_token_connection = None


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def _open_connection(username: str):
    return snowflake.connector.connect(
        account=SNOWFLAKE_ACCOUNT,
        user=username,
        authenticator="externalbrowser",
        warehouse="USER_WH",
    )


def _open_token_connection():
    return snowflake.connector.connect(
        account=SNOWFLAKE_ACCOUNT,
        user=SNOWFLAKE_USER,
        authenticator="PROGRAMMATIC_ACCESS_TOKEN",
        token=SNOWFLAKE_TOKEN,
        warehouse="USER_WH",
    )


def get_token_connection():
    global _token_connection
    if _token_connection is not None:
        try:
            _token_connection.cursor().execute("SELECT 1")
            return _token_connection
        except Exception:
            _token_connection = None
    _token_connection = _open_token_connection()
    return _token_connection


def get_user_connection():
    if SAMPLE_MODE:
        import sample_db
        try:
            return sample_db.get_connection()
        except Exception as e:
            app.logger.error("Loading sample data failed: %s", e)
            return None

    if TOKEN_MODE:
        try:
            return get_token_connection()
        except Exception as e:
            app.logger.error("Snowflake token connection failed: %s", e)
            return None

    sid = session.get("sid")
    if not sid or sid not in _connections:
        return None
    conn = _connections[sid]
    try:
        conn.cursor().execute("SELECT 1")
        return conn
    except Exception:
        del _connections[sid]
        return None


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if get_user_connection() is None:
            if request.path.startswith("/api/"):
                return jsonify({"error": "session_expired", "redirect": "/login"}), 401
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if get_user_connection():
        return redirect(url_for("index"))

    error = None
    if SAMPLE_MODE:
        error = "Could not load the sample data in sample_data/. Check the server log."
    elif TOKEN_MODE:
        # No form to fill in — the token connection itself failed
        error = "Could not connect to Snowflake with SNOWFLAKE_TOKEN. Check the server log."
    elif request.method == "POST":
        username = request.form.get("username", "").strip()
        if not username:
            error = "Please enter your Snowflake username."
        else:
            try:
                conn = _open_connection(username)
                sid = secrets.token_hex(16)
                _connections[sid] = conn
                session["sid"]      = sid
                session["username"] = username
                return redirect(url_for("index"))
            except Exception as e:
                error = str(e)

    return render_template("login.html", error=error, token_mode=TOKEN_MODE or SAMPLE_MODE)


@app.route("/logout")
def logout():
    if TOKEN_MODE or SAMPLE_MODE:
        return redirect(url_for("index"))

    sid = session.pop("sid", None)
    if sid and sid in _connections:
        try:
            _connections[sid].close()
        except Exception:
            pass
        del _connections[sid]
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def build_filter_clause(filter_type: str | None, filter_value: str | None) -> str:
    if not filter_type or not filter_value:
        return ""
    safe = filter_value.replace("'", "''")
    if filter_type == "license_key":
        return f"AND fba.LICENSE_ID = '{safe}'"
    if filter_type == "email":
        return (
            f"AND fba.LICENSE_ID IN ("
            f"SELECT LICENSE_ID FROM IB_DWH.IB10_DWH.DIM_FREE_TRIAL WHERE EMAIL = '{safe}'"
            f")"
        )
    return ""


UNREAL_FILTER_CLAUSE = "AND fba.BUILD_TITLE NOT ILIKE '%Unreal%' AND fba.BUILD_TITLE != 'XGETasks'"


def build_new_agents_only_clause(start: str, end: str) -> str:
    return (
        f"AND fba.LICENSE_ID IN ("
        f"SELECT fba2.LICENSE_ID"
        f" FROM IB_DWH.IB10_DWH.FACT_BUILD_ACTIVITY fba2"
        f" INNER JOIN IB_DWH.IB10_DWH.DIM_LICENSE l2"
        f"         ON fba2.LICENSE_ID = l2.LICENSE_ID AND l2.LICENSE_TIER = 'Free Trial'"
        f" WHERE fba2.BUILD_TITLE NOT LIKE 'Incredibuild sample project%%'"
        f" GROUP BY fba2.LICENSE_ID"
        f" HAVING MIN(fba2.START_TIMESTAMP) >= '{start}'"
        f"    AND MIN(fba2.START_TIMESTAMP) <  DATEADD(day, 1, '{end}'::DATE)"
        f")"
    )


BUILDS_QUERY = """
    WITH free_trial_builds AS (
        SELECT DISTINCT fba.BUILD_ID, fba.COORDINATOR_ID, fba.LICENSE_ID,
               fba.START_TIMESTAMP, fba.NUMBER_OF_REMOTE_TASKS, fba.NUMBER_OF_CLOUD_TASKS
        FROM IB_DWH.IB10_DWH.FACT_BUILD_ACTIVITY fba
        INNER JOIN IB_DWH.IB10_DWH.DIM_LICENSE l
                ON fba.LICENSE_ID        = l.LICENSE_ID
               AND l.START_TIMESTAMP     < fba.START_TIMESTAMP
               AND l.END_TIMESTAMP       > fba.END_TIMESTAMP
               AND l.LICENSE_TIER        = 'Free Trial'
        WHERE fba.START_TIMESTAMP >= '{start}'
          AND fba.START_TIMESTAMP <  DATEADD(day, 1, '{end}'::DATE)
          AND fba.BUILD_TITLE     NOT LIKE 'Incredibuild sample project%%'
          {filter_clause}
    ),
    cache_hits_per_build AS (
        SELECT fca.COORDINATOR_ID, fca.BUILD_ID,
               CASE WHEN SUM(fca.CACHE_HITS) > 0 THEN 'yes' ELSE 'no_hits' END AS cache_status
        FROM IB_DWH.IB10_DWH.FACT_BUILD_CACHE_ACTIVITY fca
        INNER JOIN free_trial_builds fb
                ON fca.COORDINATOR_ID = fb.COORDINATOR_ID
               AND fca.BUILD_ID       = fb.BUILD_ID
        GROUP BY fca.COORDINATOR_ID, fca.BUILD_ID
    ),
    build_data AS (
        SELECT
            DATE_TRUNC('day', fb.START_TIMESTAMP)::DATE          AS build_date,
            COUNT(*)                                              AS num_builds,
            COUNT_IF(fb.NUMBER_OF_REMOTE_TASKS > 0
                     OR fb.NUMBER_OF_CLOUD_TASKS > 0)            AS with_distribution,
            COUNT_IF(COALESCE(ch.cache_status, 'no') = 'yes')    AS with_cache_hits,
            COUNT_IF(COALESCE(ch.cache_status, 'no') = 'no_hits') AS with_cache_no_hits
        FROM free_trial_builds fb
        LEFT JOIN cache_hits_per_build ch
               ON fb.COORDINATOR_ID = ch.COORDINATOR_ID
              AND fb.BUILD_ID        = ch.BUILD_ID
        GROUP BY 1
    ),
    new_agents AS (
        SELECT DATE_TRUNC('day', gf.global_first)::DATE AS build_date,
               COUNT(*) AS new_agent_count
        FROM (
            SELECT fb.LICENSE_ID
            FROM free_trial_builds fb
            GROUP BY fb.LICENSE_ID
        ) fb_licenses
        JOIN (
            SELECT fba2.LICENSE_ID, MIN(fba2.START_TIMESTAMP) AS global_first
            FROM IB_DWH.IB10_DWH.FACT_BUILD_ACTIVITY fba2
            INNER JOIN IB_DWH.IB10_DWH.DIM_LICENSE l2
                    ON fba2.LICENSE_ID = l2.LICENSE_ID AND l2.LICENSE_TIER = 'Free Trial'
            WHERE fba2.BUILD_TITLE NOT LIKE 'Incredibuild sample project%%'
            GROUP BY fba2.LICENSE_ID
        ) gf ON gf.LICENSE_ID = fb_licenses.LICENSE_ID
        WHERE gf.global_first >= '{start}'
          AND gf.global_first <  DATEADD(day, 1, '{end}'::DATE)
        GROUP BY 1
    )
    SELECT
        bd.build_date,
        bd.num_builds,
        bd.with_distribution,
        bd.with_cache_hits,
        bd.with_cache_no_hits,
        COALESCE(na.new_agent_count, 0) AS new_agents
    FROM build_data bd
    LEFT JOIN new_agents na ON bd.build_date = na.build_date
    ORDER BY 1
"""


GRID_QUERY = """
    WITH free_trial_builds AS (
        SELECT DISTINCT
               fba.BUILD_ID, fba.COORDINATOR_ID, fba.LICENSE_ID,
               fba.BUILD_TITLE,
               fba.START_TIMESTAMP, fba.END_TIMESTAMP,
               fba.NUMBER_OF_REMOTE_TASKS, fba.NUMBER_OF_CLOUD_TASKS,
               fba.TOTAL_CPU_TIME,
               fba.REMOTE_CPU_TIME, fba.CLOUD_CPU_TIME,
               fba.NUMBER_OF_CORES_ON_INITIATOR_MACHINE,
               fba.TOTAL_CACHEABLE_TASKS,
               fba.AVG_ACTUAL_NEEDED_HELPERS,
               fba.BUILDSET_VERSION
        FROM IB_DWH.IB10_DWH.FACT_BUILD_ACTIVITY fba
        INNER JOIN IB_DWH.IB10_DWH.DIM_LICENSE l
                ON fba.LICENSE_ID        = l.LICENSE_ID
               AND l.START_TIMESTAMP     < fba.START_TIMESTAMP
               AND l.END_TIMESTAMP       > fba.END_TIMESTAMP
               AND l.LICENSE_TIER        = 'Free Trial'
        WHERE fba.START_TIMESTAMP >= '{start}'
          AND fba.START_TIMESTAMP <  DATEADD(day, 1, '{end}'::DATE)
          AND fba.BUILD_TITLE     NOT LIKE 'Incredibuild sample project%%'
          {filter_clause}
    ),
    cache_per_build AS (
        SELECT COORDINATOR_ID, BUILD_ID,
               MAX(CACHED_TASKS)                        AS max_cached_tasks,
               MAX(CACHED_TASKS_TOTAL_PROCESSING_TIME)  AS cached_proc_time_us
        FROM IB_DATALAKE.IB10.BUILD_CACHE_STAT
        GROUP BY COORDINATOR_ID, BUILD_ID
    ),
    cache_hits_per_build AS (
        SELECT COORDINATOR_ID, BUILD_ID,
               CASE WHEN SUM(CACHE_HITS) > 0 THEN 'yes' ELSE 'no_hits' END AS cache_status
        FROM IB_DWH.IB10_DWH.FACT_BUILD_CACHE_ACTIVITY
        GROUP BY COORDINATOR_ID, BUILD_ID
    )
    SELECT
        fb.BUILD_ID                                                            AS build_id,
        fb.BUILD_TITLE                                                         AS build_title,
        fb.LICENSE_ID                                                          AS license_key,
        COALESCE(mktg.EMAIL_NORM, ft.EMAIL, ls_order.CUSTOMER_EMAIL, 'N/A')   AS email,
        COALESCE(ls_lic.CREATED_AT, ft.LICENSE_CREATED_TIMESTAMP)             AS license_created,
        fb.START_TIMESTAMP                                                     AS build_start,
        fb.END_TIMESTAMP                                                       AS build_end,
        GREATEST(0, DATEDIFF('second', fb.START_TIMESTAMP, fb.END_TIMESTAMP)) AS duration_sec,
        CASE WHEN fb.NUMBER_OF_REMOTE_TASKS > 0
                  OR fb.NUMBER_OF_CLOUD_TASKS > 0
             THEN TRUE ELSE FALSE END                                          AS with_distribution,
        COALESCE(ch.cache_status, 'no')                                        AS with_cache,
        CASE
            WHEN (fb.NUMBER_OF_REMOTE_TASKS > 0 OR fb.NUMBER_OF_CLOUD_TASKS > 0
                  OR bcs.max_cached_tasks > 0)
                 AND DATEDIFF('second', fb.START_TIMESTAMP, fb.END_TIMESTAMP) > 0
            THEN ROUND(
                (fb.TOTAL_CPU_TIME + COALESCE(bcs.cached_proc_time_us, 0) / 1000000.0)
                / DATEDIFF('second', fb.START_TIMESTAMP, fb.END_TIMESTAMP),
                1
            )
            ELSE NULL
        END                                                                    AS acceleration,
        CASE
            WHEN COALESCE(fb.NUMBER_OF_CORES_ON_INITIATOR_MACHINE, 0) > 0
                 AND (fb.REMOTE_CPU_TIME > 0 OR fb.CLOUD_CPU_TIME > 0
                      OR bcs.max_cached_tasks > 0)
            THEN ROUND(
                (COALESCE(fb.REMOTE_CPU_TIME, 0)
                 + COALESCE(fb.CLOUD_CPU_TIME, 0)
                 + COALESCE(bcs.cached_proc_time_us, 0) / 1000000.0)
                / fb.NUMBER_OF_CORES_ON_INITIATOR_MACHINE,
                1
            )
            ELSE NULL
        END                                                                    AS acceleration_dori,
        fb.TOTAL_CACHEABLE_TASKS                                               AS total_cacheable_tasks,
        fb.AVG_ACTUAL_NEEDED_HELPERS                                           AS avg_actual_needed_helpers,
        fb.BUILDSET_VERSION                                                    AS buildset_version,
        fb.COORDINATOR_ID                                                      AS coordinator_id
    FROM free_trial_builds fb
    LEFT JOIN IB_DWH.IB10_DWH.DIM_FREE_TRIAL ft ON ft.LICENSE_ID = fb.LICENSE_ID
    LEFT JOIN IB_DATALAKE.LICENSE_SPRING.LICENSES_LICENSE ls_lic ON ls_lic.LICENSE_KEY = fb.LICENSE_ID
    LEFT JOIN IB_DATALAKE.LICENSE_SPRING.CORE_ORDER ls_order ON ls_order.ID = ls_lic.ORDER_ID
    LEFT JOIN (
        SELECT LICENSE_ID, MAX(EMAIL_NORM) AS EMAIL_NORM
        FROM IB_DWH.DWH.FACT_MKTG_FUNNEL_ENTITY_DAILY
        WHERE EMAIL_NORM IS NOT NULL
        GROUP BY LICENSE_ID
    ) mktg ON mktg.LICENSE_ID = fb.LICENSE_ID
    LEFT JOIN cache_per_build bcs
           ON fb.COORDINATOR_ID = bcs.COORDINATOR_ID
          AND fb.BUILD_ID       = bcs.BUILD_ID
    LEFT JOIN cache_hits_per_build ch
           ON fb.COORDINATOR_ID = ch.COORDINATOR_ID
          AND fb.BUILD_ID       = ch.BUILD_ID
    ORDER BY fb.START_TIMESTAMP DESC
"""


# ---------------------------------------------------------------------------
# App routes
# ---------------------------------------------------------------------------

def fetch_dashboard_data(conn, start: str, end: str, filter_clause: str = "") -> dict:
    cur = conn.cursor()
    cur.execute(BUILDS_QUERY.format(start=start, end=end, filter_clause=filter_clause))
    rows = cur.fetchall()

    df = pd.DataFrame(rows, columns=["build_date", "num_builds", "with_distribution",
                                      "with_cache_hits", "with_cache_no_hits", "new_agents"])
    df["build_date"] = pd.to_datetime(df["build_date"])

    if df.empty:
        all_days = pd.date_range(start=start, end=end, freq="D")
        df = pd.DataFrame({"build_date": all_days, "num_builds": 0, "with_distribution": 0,
                           "with_cache_hits": 0, "with_cache_no_hits": 0, "new_agents": 0})
    else:
        all_days = pd.date_range(start=df["build_date"].min(), end=df["build_date"].max(), freq="D")
        df = df.set_index("build_date").reindex(all_days, fill_value=0).reset_index()
        df.rename(columns={"index": "build_date"}, inplace=True)

    df["rolling_7d"]                  = df["num_builds"].rolling(7, min_periods=1).mean().round(1)
    df["cumulative_builds"]           = df["num_builds"].cumsum()
    df["cumulative_agents"]           = df["new_agents"].cumsum()
    df["cumulative_distribution"]     = df["with_distribution"].cumsum()
    df["cumulative_cache_hits"]       = df["with_cache_hits"].cumsum()
    df["cumulative_cache_no_hits"]    = df["with_cache_no_hits"].cumsum()

    dates = df["build_date"].dt.strftime("%Y-%m-%d").tolist()

    return {
        "dates":                      dates,
        "num_builds":                 df["num_builds"].tolist(),
        "with_distribution":          df["with_distribution"].tolist(),
        "with_cache_hits":            df["with_cache_hits"].tolist(),
        "with_cache_no_hits":         df["with_cache_no_hits"].tolist(),
        "new_agents":                 df["new_agents"].tolist(),
        "rolling_7d":                 df["rolling_7d"].tolist(),
        "cumulative_builds":          df["cumulative_builds"].tolist(),
        "cumulative_agents":          df["cumulative_agents"].tolist(),
        "cumulative_distribution":    df["cumulative_distribution"].tolist(),
        "cumulative_cache_hits":      df["cumulative_cache_hits"].tolist(),
        "cumulative_cache_no_hits":   df["cumulative_cache_no_hits"].tolist(),
        "summary": {
            "total_builds":       int(df["num_builds"].sum()),
            "total_agents":       int(df["new_agents"].sum()),
            "active_days":        int((df["num_builds"] > 0).sum()),
            "peak_builds":        int(df["num_builds"].max()),
            "peak_date":          df.loc[df["num_builds"].idxmax(), "build_date"].strftime("%b %d, %Y")
                                  if df["num_builds"].max() > 0 else "N/A",
            "total_distribution": int(df["with_distribution"].sum()),
            "total_cache":        int(df["with_cache_hits"].sum()),
        },
    }


@app.route("/")
@login_required
def index():
    default_start, default_end = default_date_range()
    return render_template("index.html", default_start=default_start, default_end=default_end,
                           token_mode=TOKEN_MODE or SAMPLE_MODE, sample_mode=SAMPLE_MODE)


@app.route("/api/data")
@login_required
def api_data():
    conn             = get_user_connection()
    default_start, default_end = default_date_range()
    start            = request.args.get("start",            default_start)
    end              = request.args.get("end",              default_end)
    filter_type      = request.args.get("filter_type",      "")
    filter_value     = request.args.get("filter_value",     "")
    new_agents_only  = request.args.get("new_agents_only",  "")
    hide_unreal      = request.args.get("hide_unreal",      "")
    fc = build_filter_clause(filter_type, filter_value)
    if new_agents_only == "1":
        fc += " " + build_new_agents_only_clause(start, end)
    if hide_unreal == "1":
        fc += " " + UNREAL_FILTER_CLAUSE
    try:
        return jsonify(fetch_dashboard_data(conn, start, end, fc))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/grid")
@login_required
def api_grid():
    conn             = get_user_connection()
    default_start, default_end = default_date_range()
    start            = request.args.get("start",            default_start)
    end              = request.args.get("end",              default_end)
    filter_type      = request.args.get("filter_type",      "")
    filter_value     = request.args.get("filter_value",     "")
    new_agents_only  = request.args.get("new_agents_only",  "")
    hide_unreal      = request.args.get("hide_unreal",      "")
    fc = build_filter_clause(filter_type, filter_value)
    if new_agents_only == "1":
        fc += " " + build_new_agents_only_clause(start, end)
    if hide_unreal == "1":
        fc += " " + UNREAL_FILTER_CLAUSE
    try:
        cur = conn.cursor()
        cur.execute(GRID_QUERY.format(start=start, end=end, filter_clause=fc))
        rows = cur.fetchall()

        def fmt_ts(ts):
            return ts.strftime("%Y-%m-%d %H:%M:%S") if ts else ""

        data = [
            {
                "build_id":                  str(r[0]),
                "build_title":               r[1],
                "license_key":               r[2],
                "email":                     r[3],
                "license_created":           fmt_ts(r[4]),
                "build_start":               fmt_ts(r[5]),
                "build_end":                 fmt_ts(r[6]),
                "duration_sec":              int(r[7])   if r[7]  is not None else 0,
                "with_distribution":         bool(r[8]),
                "with_cache":                str(r[9]) if r[9] else "no",
                "acceleration":              float(r[10]) if r[10] is not None else None,
                "acceleration_dori":         float(r[11]) if r[11] is not None else None,
                "total_cacheable_tasks":     int(r[12])  if r[12] is not None else None,
                "avg_actual_needed_helpers": float(r[13]) if r[13] is not None else None,
                "buildset_version":          r[14],
                "coordinator_id":            str(r[15]) if r[15] is not None else "",
            }
            for r in rows
        ]
        return jsonify({"rows": data, "total": len(data)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/export")
@login_required
def api_export():
    conn             = get_user_connection()
    default_start, default_end = default_date_range()
    start            = request.args.get("start",            default_start)
    end              = request.args.get("end",              default_end)
    filter_type      = request.args.get("filter_type",      "")
    filter_value     = request.args.get("filter_value",     "")
    new_agents_only  = request.args.get("new_agents_only",  "")
    hide_unreal      = request.args.get("hide_unreal",      "")
    fc = build_filter_clause(filter_type, filter_value)
    if new_agents_only == "1":
        fc += " " + build_new_agents_only_clause(start, end)
    if hide_unreal == "1":
        fc += " " + UNREAL_FILTER_CLAUSE
    try:
        cur = conn.cursor()
        cur.execute(GRID_QUERY.format(start=start, end=end, filter_clause=fc))
        rows = cur.fetchall()

        def fmt_ts(ts):
            return ts.strftime("%Y-%m-%d %H:%M:%S") if ts else ""

        df = pd.DataFrame([{
            "Build ID":                  str(r[0]),
            "Build Title":               r[1],
            "License Key":               r[2],
            "Email":                     r[3],
            "License Created":           fmt_ts(r[4]),
            "Build Start":               fmt_ts(r[5]),
            "Build End":                 fmt_ts(r[6]),
            "Duration (sec)":            int(r[7])    if r[7]  is not None else 0,
            "With Distribution":         bool(r[8]),
            "With Cache":                bool(r[9]),
            "Acceleration":              float(r[10]) if r[10] is not None else None,
            "Acceleration (Dori)":       float(r[11]) if r[11] is not None else None,
            "Total Cacheable Tasks":     int(r[12])   if r[12] is not None else None,
            "Avg Needed Helpers":        float(r[13]) if r[13] is not None else None,
            "Buildset Version":          r[14],
        } for r in rows])

        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Builds")
        buf.seek(0)

        filename = f"free_trial_builds_{start}_to_{end}.xlsx"
        return send_file(buf, as_attachment=True, download_name=filename,
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/build")
@login_required
def api_build():
    build_id      = request.args.get("build_id", "")
    coordinator_id = request.args.get("coordinator_id", "")
    if not build_id:
        return jsonify({"error": "build_id required"}), 400
    try:
        conn = get_user_connection()
        cur  = conn.cursor()
        cur.execute("""
            SELECT * FROM IB_DWH.IB10_DWH.FACT_BUILD_ACTIVITY
            WHERE BUILD_ID = %s AND COORDINATOR_ID = %s
        """, (build_id, coordinator_id))
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        def serial(v):
            if hasattr(v, "strftime"): return v.strftime("%Y-%m-%d %H:%M:%S")
            return v
        data = [{cols[i]: serial(v) for i, v in enumerate(row)} for row in rows]
        return jsonify({"columns": cols, "rows": data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/build/cache")
@login_required
def api_build_cache():
    build_id       = request.args.get("build_id", "")
    coordinator_id = request.args.get("coordinator_id", "")
    if not build_id:
        return jsonify({"error": "build_id required"}), 400
    try:
        conn = get_user_connection()
        cur  = conn.cursor()

        def serial(v):
            if hasattr(v, "strftime"): return v.strftime("%Y-%m-%d %H:%M:%S")
            return v

        cur.execute("""
            SELECT * FROM IB_DWH.IB10_DWH.FACT_BUILD_CACHE_ACTIVITY
            WHERE BUILD_ID = %s AND COORDINATOR_ID = %s
        """, (build_id, coordinator_id))
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        data = [{cols[i]: serial(v) for i, v in enumerate(row)} for row in rows]

        if data:
            return jsonify({"columns": cols, "rows": data})

        # No cache data — diagnose why
        cur.execute("""
            SELECT IS_BUILD_CACHE_QUOTA_AVAILABLE, COMMAND, TOTAL_CACHEABLE_TASKS
            FROM IB_DWH.IB10_DWH.FACT_BUILD_ACTIVITY
            WHERE BUILD_ID = %s AND COORDINATOR_ID = %s
            LIMIT 1
        """, (build_id, coordinator_id))
        build_row = cur.fetchone()

        reasons = []
        if build_row:
            cache_quota, command, cacheable_tasks = build_row
            if cache_quota is False or cache_quota == 0:
                reasons.append("Cache is not included in this license.")
            if command and re.search(r'(/|--)(no.?cache|disablecache)', str(command), re.IGNORECASE):
                reasons.append("The build was run with a no-cache flag in the command.")
            if cacheable_tasks is not None and cacheable_tasks == 0:
                reasons.append("The build had no cacheable tasks.")

        if not reasons:
            reasons.append("Cache was not used in this build.")

        return jsonify({"columns": [], "rows": [], "reasons": reasons})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Free trial tracking dashboard")
    parser.add_argument("--sample-data", action="store_true",
                        help="use the bundled sample data instead of Snowflake")
    if parser.parse_args().sample_data:
        SAMPLE_MODE = True

    app.run(debug=True, port=5050)
