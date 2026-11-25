"""Generate the sample data used by `app.py --sample-data`.

Writes one CSV per Snowflake table the dashboard queries into sample_data/,
named <DATABASE>.<SCHEMA>.<TABLE>.csv. The output is deterministic (fixed
seed), so re-running the script reproduces the committed files.

All people, companies, emails and license keys are made up. Dates end on
ANCHOR_DATE; the app shifts them at load time so the data always ends today.

Usage: python scripts/generate_sample_data.py
"""

import csv
import random
from datetime import date, datetime, timedelta
from pathlib import Path

SEED = 20261125
ANCHOR_DATE = date(2026, 11, 24)       # last day of activity in the files
DATA_START = date(2026, 1, 5)          # first possible signup
NUM_FREE_TRIAL = 100
NUM_PAID = 10                          # non-free-trial licenses; the dashboard must exclude them
TRIAL_DAYS = 30

OUT_DIR = Path(__file__).resolve().parent.parent / "sample_data"

FIRST_NAMES = [
    "Dana", "Avi", "Noa", "Eitan", "Maya", "Yoni", "Tamar", "Omer", "Shira", "Lior",
    "Alex", "Jordan", "Sam", "Taylor", "Morgan", "Casey", "Riley", "Jamie", "Robin", "Quinn",
    "Priya", "Arjun", "Mei", "Kenji", "Lena", "Mateo", "Sofia", "Lucas", "Emma", "Noah",
]
LAST_NAMES = [
    "Levi", "Cohen", "Mizrahi", "Peretz", "Friedman", "Shapiro", "Katz", "Biton",
    "Smith", "Garcia", "Chen", "Patel", "Novak", "Rossi", "Muller", "Silva", "Kim", "Okafor",
]
COMPANIES = [
    "northwind", "contoso", "fabrikam", "tailspin", "adatum", "litware", "proseware",
    "wingtip", "fourthcoffee", "woodgrove", "lucerne", "margie", "alpine", "coho",
    "blueyonder", "graphicdesign", "humongous", "treyresearch", "wideworld", "relecloud",
]
PROJECTS = [
    "core-engine", "render-service", "physics-lib", "audio-mixer", "netcode", "toolchain",
    "sdk", "firmware", "simulator", "codec", "compiler-tests", "ui-shell", "backend-api",
]
CONFIGS = ["Release x64", "Debug x64", "Release Win32", "RelWithDebInfo", "Debug"]
UNREAL_TITLES = ["{game} - UnrealBuildTool", "{game}Editor Win64 Development", "XGETasks"]
GAMES = ["Starfall", "IronKeep", "NeonDrift", "Hollowmere", "SkyForge"]
BUILDSET_VERSIONS = ["10.33.0", "10.34.1", "10.35.0", "10.36.2", "10.37.0", "10.38.1"]

# Engagement segments: (share of users, min builds, max builds)
SEGMENTS = [
    (0.20, 1, 3),       # tried it once or twice
    (0.50, 5, 30),      # light
    (0.25, 30, 120),    # regular
    (0.05, 150, 400),   # heavy
]


def license_key(rng):
    return "-".join("".join(rng.choice("0123456789ABCDEF") for _ in range(4)) for _ in range(4))


def coordinator_id(rng):
    return "{:08X}-{:04X}-{:04X}-{:04X}-{:012X}".format(
        rng.getrandbits(32), rng.getrandbits(16), rng.getrandbits(16),
        rng.getrandbits(16), rng.getrandbits(48))


def ts(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""


def work_time(rng, day):
    """A plausible build start time on `day`, weighted to working hours."""
    hour = min(23, max(0, int(rng.gauss(13, 3.5))))
    return datetime(day.year, day.month, day.day, hour, rng.randrange(60), rng.randrange(60))


def pick_segment(rng):
    r, acc = rng.random(), 0.0
    for share, lo, hi in SEGMENTS:
        acc += share
        if r <= acc:
            return lo, hi
    return SEGMENTS[-1][1:]


def make_user(rng, idx, tier):
    first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
    company = rng.choice(COMPANIES)
    span = (ANCHOR_DATE - DATA_START).days - 3
    # Later signups slightly more likely (growing funnel)
    signup = DATA_START + timedelta(days=int(span * (rng.random() ** 0.8)))
    unreal = rng.random() < 0.15
    return {
        "idx": idx,
        "tier": tier,
        "license_id": license_key(rng),
        "coordinator_id": coordinator_id(rng),
        "email": f"{first}.{last}{idx}@{company}.example.com".lower(),
        "signup": signup,
        "unreal": unreal,
        "game": rng.choice(GAMES),
        "projects": rng.sample(PROJECTS, rng.randint(1, 3)),
        "cores": rng.choice([8, 12, 16, 24, 32]),
        "p_distributed": rng.choice([0.0, 0.3, 0.6, 0.8, 0.95]),
        "p_cloud": rng.choice([0.0, 0.0, 0.2, 0.5]),
        "cache_quota": rng.random() < 0.35,
        "p_nocache": rng.choice([0.0, 0.0, 0.1]),
        "version": rng.choice(BUILDSET_VERSIONS),
        # Which email sources know about this user (exercises the app's fallbacks)
        "in_free_trial_table": tier == "Free Trial" and rng.random() < 0.85,
        "in_mktg": rng.random() < 0.6,
        "in_license_spring": rng.random() < 0.7,
    }


def make_builds(rng, user, next_build_id):
    builds = []
    lo, hi = pick_segment(rng)
    count = rng.randint(lo, hi)
    trial_end = user["signup"] + timedelta(days=TRIAL_DAYS)
    last_day = min(trial_end, ANCHOR_DATE)
    active_days = max(1, (last_day - user["signup"]).days)

    # Most users start with the bundled sample project, which the dashboard filters out
    days = [user["signup"]] * rng.choice([0, 1, 1, 2])
    days += sorted(user["signup"] + timedelta(days=int(active_days * rng.random() ** 1.3))
                   for _ in range(count))
    # A few builds after the trial ended (outside the license window, so excluded)
    if trial_end < ANCHOR_DATE and rng.random() < 0.2:
        days += [min(ANCHOR_DATE, trial_end + timedelta(days=rng.randint(1, 10)))]

    sample_left = len(days) - count - (1 if days and days[-1] > trial_end else 0)
    for day in days:
        start = work_time(rng, day)
        if day == user["signup"]:
            start = max(start, datetime.combine(day, datetime.min.time()) + timedelta(hours=1))
        is_sample = sample_left > 0
        sample_left -= 1 if is_sample else 0

        if is_sample:
            title = "Incredibuild sample project - " + rng.choice(["Hello World", "Benchmark"])
        elif user["unreal"] and rng.random() < 0.7:
            title = rng.choice(UNREAL_TITLES).format(game=user["game"])
        else:
            title = f"{rng.choice(user['projects'])} {rng.choice(CONFIGS)}"

        duration = int(max(20, rng.lognormvariate(6.3, 0.9)))      # seconds, median ~9 min
        end = start + timedelta(seconds=duration)
        distributed = rng.random() < user["p_distributed"]
        cloud = distributed and rng.random() < user["p_cloud"]
        total_tasks = rng.randint(50, 6000)
        remote_tasks = int(total_tasks * rng.uniform(0.3, 0.9)) if distributed else 0
        cloud_tasks = int(remote_tasks * rng.uniform(0.1, 0.5)) if cloud else 0
        local_cpu = duration * user["cores"] * rng.uniform(0.4, 0.9)
        remote_cpu = duration * rng.uniform(2, 20) * user["cores"] * 0.5 if distributed else 0.0
        cloud_cpu = remote_cpu * rng.uniform(0.2, 0.6) if cloud else 0.0
        nocache = rng.random() < user["p_nocache"]
        cacheable = int(total_tasks * rng.uniform(0.2, 0.8)) if user["cache_quota"] else 0

        builds.append({
            "BUILD_ID": next_build_id,
            "COORDINATOR_ID": user["coordinator_id"],
            "LICENSE_ID": user["license_id"],
            "BUILD_TITLE": title,
            "START_TIMESTAMP": ts(start),
            "END_TIMESTAMP": ts(end),
            "NUMBER_OF_TASKS": total_tasks,
            "NUMBER_OF_REMOTE_TASKS": remote_tasks,
            "NUMBER_OF_CLOUD_TASKS": cloud_tasks,
            "TOTAL_CPU_TIME": round(local_cpu + remote_cpu, 1),
            "REMOTE_CPU_TIME": round(remote_cpu, 1),
            "CLOUD_CPU_TIME": round(cloud_cpu, 1),
            "NUMBER_OF_CORES_ON_INITIATOR_MACHINE": user["cores"],
            "TOTAL_CACHEABLE_TASKS": cacheable,
            "AVG_ACTUAL_NEEDED_HELPERS": round(rng.uniform(2, 40), 1) if distributed else 0.0,
            "BUILDSET_VERSION": user["version"],
            "IS_BUILD_CACHE_QUOTA_AVAILABLE": "true" if user["cache_quota"] else "false",
            "COMMAND": ("BuildConsole.exe Solution.sln /build /cfg=\"Release|x64\""
                        + (" /nocache" if nocache else "")),
            "_cache_enabled": user["cache_quota"] and not nocache and cacheable > 0,
            "_cacheable": cacheable,
        })
        next_build_id += 1
    return builds, next_build_id


def write_csv(name, rows, columns):
    path = OUT_DIR / f"{name}.csv"
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"  {path.name}: {len(rows)} rows")


def main():
    rng = random.Random(SEED)
    OUT_DIR.mkdir(exist_ok=True)

    users = [make_user(rng, i, "Free Trial") for i in range(1, NUM_FREE_TRIAL + 1)]
    users += [make_user(rng, NUM_FREE_TRIAL + i, rng.choice(["Pro", "Enterprise"]))
              for i in range(1, NUM_PAID + 1)]

    builds, cache_activity, cache_stat = [], [], []
    licenses, free_trials, mktg, ls_licenses, ls_orders = [], [], [], [], []
    next_build_id = 100001

    for u in users:
        start = datetime.combine(u["signup"], datetime.min.time())
        end = start + timedelta(days=TRIAL_DAYS if u["tier"] == "Free Trial" else 365)
        licenses.append({"LICENSE_ID": u["license_id"], "LICENSE_TIER": u["tier"],
                         "START_TIMESTAMP": ts(start), "END_TIMESTAMP": ts(end)})
        if u["in_free_trial_table"]:
            free_trials.append({"LICENSE_ID": u["license_id"], "EMAIL": u["email"],
                                "LICENSE_CREATED_TIMESTAMP": ts(start)})
        if u["in_mktg"]:
            mktg.append({"LICENSE_ID": u["license_id"], "EMAIL_NORM": u["email"],
                         "EVENT_DATE": u["signup"].isoformat()})
        if u["in_license_spring"]:
            order_id = 50000 + u["idx"]
            ls_licenses.append({"LICENSE_KEY": u["license_id"], "ORDER_ID": order_id,
                                "CREATED_AT": ts(start - timedelta(minutes=rng.randint(1, 30)))})
            ls_orders.append({"ID": order_id, "CUSTOMER_EMAIL": u["email"]})

        user_builds, next_build_id = make_builds(rng, u, next_build_id)
        for b in user_builds:
            if b["_cache_enabled"]:
                hits = rng.choice([0, 0, 0, rng.randint(1, b["_cacheable"])])
                cache_activity.append({
                    "COORDINATOR_ID": b["COORDINATOR_ID"], "BUILD_ID": b["BUILD_ID"],
                    "CACHE_HITS": hits, "CACHE_MISSES": b["_cacheable"] - hits,
                    "CACHE_UPLOADS": rng.randint(0, b["_cacheable"] - hits),
                })
                if hits:
                    cache_stat.append({
                        "COORDINATOR_ID": b["COORDINATOR_ID"], "BUILD_ID": b["BUILD_ID"],
                        "CACHED_TASKS": hits,
                        "CACHED_TASKS_TOTAL_PROCESSING_TIME": hits * rng.randint(200_000, 3_000_000),
                    })
        builds += user_builds

    # Users with no email anywhere show as "N/A" in the builds table
    print(f"Writing sample data to {OUT_DIR}")
    build_columns = [c for c in builds[0] if not c.startswith("_")]
    write_csv("IB_DWH.IB10_DWH.FACT_BUILD_ACTIVITY", builds, build_columns)
    write_csv("IB_DWH.IB10_DWH.FACT_BUILD_CACHE_ACTIVITY", cache_activity,
              ["COORDINATOR_ID", "BUILD_ID", "CACHE_HITS", "CACHE_MISSES", "CACHE_UPLOADS"])
    write_csv("IB_DWH.IB10_DWH.DIM_LICENSE", licenses,
              ["LICENSE_ID", "LICENSE_TIER", "START_TIMESTAMP", "END_TIMESTAMP"])
    write_csv("IB_DWH.IB10_DWH.DIM_FREE_TRIAL", free_trials,
              ["LICENSE_ID", "EMAIL", "LICENSE_CREATED_TIMESTAMP"])
    write_csv("IB_DWH.DWH.FACT_MKTG_FUNNEL_ENTITY_DAILY", mktg,
              ["LICENSE_ID", "EMAIL_NORM", "EVENT_DATE"])
    write_csv("IB_DATALAKE.IB10.BUILD_CACHE_STAT", cache_stat,
              ["COORDINATOR_ID", "BUILD_ID", "CACHED_TASKS", "CACHED_TASKS_TOTAL_PROCESSING_TIME"])
    write_csv("IB_DATALAKE.LICENSE_SPRING.LICENSES_LICENSE", ls_licenses,
              ["LICENSE_KEY", "ORDER_ID", "CREATED_AT"])
    write_csv("IB_DATALAKE.LICENSE_SPRING.CORE_ORDER", ls_orders, ["ID", "CUSTOMER_EMAIL"])


if __name__ == "__main__":
    main()
