"""Local sample-data backend for `app.py --sample-data`.

Loads the CSVs in sample_data/ into an in-memory DuckDB database that mirrors
the Snowflake table names (IB_DWH.IB10_DWH.FACT_BUILD_ACTIVITY, ...), so the
app's existing SQL runs unchanged. The few Snowflake-only constructs it uses
are translated on the way in. Regenerate the CSVs with
scripts/generate_sample_data.py.
"""

import re
import threading
from datetime import date
from pathlib import Path

import duckdb

SAMPLE_DIR = Path(__file__).resolve().parent / "sample_data"

# Must match ANCHOR_DATE in scripts/generate_sample_data.py. All timestamps are
# shifted forward by (today - ANCHOR_DATE) so the data always ends today.
ANCHOR_DATE = date(2026, 11, 24)

_DATEADD_DAY = re.compile(r"DATEADD\(\s*day\s*,\s*(-?\d+)\s*,\s*([^)]+?)\s*\)", re.IGNORECASE)

_db = None
_lock = threading.Lock()


def _translate(sql: str) -> str:
    """Rewrite the Snowflake-only SQL the app uses into DuckDB syntax."""
    sql = _DATEADD_DAY.sub(r"(\2 + INTERVAL (\1) DAY)", sql)
    sql = sql.replace("%%", "%")      # pyformat escape for LIKE wildcards
    return sql.replace("%s", "?")     # pyformat placeholders -> DuckDB placeholders


def _load() -> duckdb.DuckDBPyConnection:
    db = duckdb.connect()
    shift_days = (date.today() - ANCHOR_DATE).days
    databases, schemas = set(), set()

    for csv_path in sorted(SAMPLE_DIR.glob("*.csv")):
        database, schema, table = csv_path.stem.split(".")
        if database not in databases:
            db.execute(f"ATTACH ':memory:' AS {database}")
            databases.add(database)
        if (database, schema) not in schemas:
            db.execute(f"CREATE SCHEMA {database}.{schema}")
            schemas.add((database, schema))

        full_name = f"{database}.{schema}.{table}"
        db.execute(f"CREATE TABLE {full_name} AS SELECT * FROM read_csv(?, header = true)",
                   [str(csv_path)])

        if shift_days:
            columns = db.execute(f"DESCRIBE {full_name}").fetchall()
            for name, col_type, *_ in columns:
                if col_type in ("TIMESTAMP", "DATE"):
                    db.execute(f"UPDATE {full_name} SET {name} = {name} + INTERVAL ({shift_days}) DAY")
    return db


def _get_db() -> duckdb.DuckDBPyConnection:
    global _db
    with _lock:
        if _db is None:
            _db = _load()
        return _db


class SampleCursor:
    """The subset of the Snowflake cursor API that app.py uses."""

    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, sql, params=None):
        sql = _translate(sql)
        if params is None:
            self._cursor.execute(sql)
        else:
            self._cursor.execute(sql, list(params))
        return self

    def fetchall(self):
        return self._cursor.fetchall()

    def fetchone(self):
        return self._cursor.fetchone()

    @property
    def description(self):
        return self._cursor.description


class SampleConnection:
    def cursor(self):
        # Each DuckDB cursor is its own connection to the shared database,
        # so concurrent requests don't step on each other.
        return SampleCursor(_get_db().cursor())

    def close(self):
        pass


def get_connection() -> SampleConnection:
    _get_db()   # load eagerly so a bad sample file fails on first use, not mid-query
    return SampleConnection()
