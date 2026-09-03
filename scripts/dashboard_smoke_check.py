"""Headless smoke check for the Streamlit dashboard.

    python scripts/dashboard_smoke_check.py

Streamlit serves a JS shell, so HTTP alone cannot execute the app script.
This check therefore covers both layers:
  1. the server boots headless and answers 200 + /healthz
  2. the dashboard's data path (same queries + metric math) runs against the
     populated database and produces the expected reconciliation numbers

Exits non-zero on failure.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.getenv("LEDGER_DASHBOARD_PORT", "8643"))
URL = f"http://127.0.0.1:{PORT}"

sys.path.insert(0, str(REPO_ROOT))


def wait_until_up(proc: subprocess.Popen, timeout_s: float = 60.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            if requests.get(URL, timeout=2).status_code == 200:
                return True
        except requests.ConnectionError:
            time.sleep(0.5)
    return False


def check_server(proc: subprocess.Popen) -> bool:
    if not wait_until_up(proc):
        print("[FAIL] dashboard did not serve HTTP 200 in time")
        return False
    health = requests.get(f"{URL}/healthz", timeout=5)
    ok = health.status_code == 200 and "ok" in health.text
    print(f"[{'PASS' if ok else 'FAIL'}] server up: / -> 200, /healthz -> '{health.text.strip()}'")
    return ok


def check_data_path() -> bool:
    """Run the exact queries + metric math the dashboard performs."""
    from app import db
    from app.config import db_path

    conn = db.get_connection(db_path())
    try:
        audit = db.load_audit_df(conn)
    finally:
        conn.close()

    expected_columns = {
        "order_id", "outcome", "reason", "classification", "audit_note", "created_at",
    }
    missing = expected_columns - set(audit.columns)
    if missing:
        print(f"[FAIL] audit_log missing columns: {sorted(missing)}")
        return False

    recon = audit[audit["outcome"].isin(["matched", "exception"])]
    matched = (recon["outcome"] == "matched").sum()
    exceptions = (recon["outcome"] == "exception").sum()
    match_rate = matched / len(recon) * 100 if len(recon) else 0.0

    ok = len(recon) == 61 and matched == 49 and exceptions == 12
    print(
        f"[{'PASS' if ok else 'FAIL'}] data path: {len(recon)} reconciliation rows, "
        f"{matched} matched, {exceptions} exceptions, match rate {match_rate:.1f}%"
    )
    return ok


def main() -> int:
    if not (REPO_ROOT / "ledger_sentinel.db").exists():
        print("ledger_sentinel.db not found - run `python -m app.main` first.")
        return 1

    if not check_data_path():
        return 1

    env = os.environ.copy()
    env["STREAMLIT_BROWSER_GATHER_USAGE_STATS"] = "false"

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "streamlit", "run", "dashboard/app.py",
            "--server.headless", "true",
            "--server.port", str(PORT),
        ],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        return 0 if check_server(proc) else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
