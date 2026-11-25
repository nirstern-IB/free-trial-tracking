"""Process bookkeeping around the documented repository start command."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[4]
STATE = Path(tempfile.gettempdir()) / "islo-nirs-tenant-machine-1-free-trial-tracking"
STATE.mkdir(exist_ok=True, mode=0o700)
PID = STATE / "pid"
URL = "http://127.0.0.1:5050"


def running():
    if not PID.exists():
        return False
    try:
        pid = int(PID.read_text())
        os.kill(pid, 0)
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except (ValueError, ProcessLookupError, FileNotFoundError):
        return False


def healthy():
    with urllib.request.urlopen(URL + "/", timeout=5) as response:
        assert response.status == 200 and b"Sample data" in response.read()
    with urllib.request.urlopen(URL + "/api/data", timeout=5) as response:
        assert json.load(response)["summary"]["total_builds"] > 0


action = sys.argv[1]
if action in ("start", "start-verification"):
    if not running():
        with (STATE / "server.log").open("ab") as log:
            process = subprocess.Popen([str(ROOT / ".venv/bin/python"), "app.py"],
                                       cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log,
                                       stderr=log, start_new_session=True)
        PID.write_text(str(process.pid))
    for attempt in range(50):
        try:
            assert running()
            healthy()
            break
        except Exception:
            time.sleep(0.5)
    else:
        sys.exit("Dashboard did not become healthy; inspect the local runtime log.")
    print("Dashboard listening at " + URL, file=sys.stderr)
    if action == "start-verification":
        print(json.dumps({"schema": 1, "targets": [{"name": "free-trial-tracking", "url": URL}]}))
elif action == "health":
    assert running()
    healthy()
    print(json.dumps({"schema": 1, "status": "healthy"}))
elif action == "status":
    print(json.dumps({"schema": 1, "state": "running" if running() else "stopped"}))
elif action == "stop":
    if running():
        os.killpg(int(PID.read_text()), signal.SIGTERM)
        for attempt in range(50):
            if not running():
                break
            time.sleep(0.1)
        else:
            sys.exit("Dashboard did not stop")
    PID.unlink(missing_ok=True)
