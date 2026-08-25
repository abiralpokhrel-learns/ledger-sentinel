"""Live HTTP check of the webhook server — boots uvicorn and fires real requests.

    python scripts/live_webhook_check.py

Verifies, over actual HTTP (not TestClient):
  1. a correctly signed delivery is applied
  2. the same event_id re-delivered is skipped (idempotent)
  3. a backward transition is dropped (state machine)
  4. a bad signature is rejected with HTTP 400

Exits non-zero on any failure. Uses an isolated temp database so it never
touches ledger_sentinel.db.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.getenv("LEDGER_CHECK_PORT", "8642"))
BASE = f"http://127.0.0.1:{PORT}"

sys.path.insert(0, str(REPO_ROOT))
from app.config import DEFAULT_WEBHOOK_SECRET  # noqa: E402


def sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def make_event(order_id: str, event_type: str) -> tuple[dict, bytes]:
    payload = {
        "event_id": f"evt_{uuid.uuid4().hex[:12]}",
        "order_id": order_id,
        "event_type": event_type,
    }
    raw = json.dumps(payload).encode()
    return payload, raw


def post_event(raw: bytes, secret: str) -> requests.Response:
    return requests.post(
        f"{BASE}/webhook",
        data=raw,
        headers={
            "Content-Type": "application/json",
            "x-razorpay-signature": sign(raw, secret),
        },
        timeout=10,
    )


def wait_for_health(proc: subprocess.Popen, timeout_s: float = 30.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"uvicorn exited early with code {proc.returncode}")
        try:
            if requests.get(f"{BASE}/health", timeout=2).status_code == 200:
                return
        except requests.ConnectionError:
            time.sleep(0.4)
    raise RuntimeError("uvicorn did not become healthy in time")


def main() -> int:
    results: list[tuple[str, bool]] = []

    # ignore_cleanup_errors: the terminated server process can release its
    # SQLite handle a moment after terminate() on Windows.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        env = os.environ.copy()
        env["LEDGER_DB_PATH"] = str(Path(tmp) / "check.db")
        env["WEBHOOK_SECRET"] = DEFAULT_WEBHOOK_SECRET

        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT)],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            wait_for_health(proc)
            results.append(("server boots + /health", True))

            # 1. signed event applies
            payload, raw = make_event("order_live_1", "authorized")
            r = post_event(raw, DEFAULT_WEBHOOK_SECRET)
            results.append((
                "signed event accepted (200, applied)",
                r.status_code == 200 and r.json()["verdict"] == "applied",
            ))

            # 2. same event_id re-delivered -> duplicate_skipped
            r = post_event(raw, DEFAULT_WEBHOOK_SECRET)
            results.append((
                "duplicate event_id skipped (idempotent)",
                r.status_code == 200 and r.json()["verdict"] == "duplicate_skipped",
            ))

            # 3. backward transition dropped
            _, raw_back = make_event("order_live_1", "created")
            r = post_event(raw_back, DEFAULT_WEBHOOK_SECRET)
            results.append((
                "backward transition dropped (state machine)",
                r.status_code == 200 and r.json()["verdict"] == "out_of_order_dropped",
            ))

            # 4. bad signature rejected with 400
            _, raw_bad = make_event("order_live_2", "captured")
            r = post_event(raw_bad, "totally_wrong_secret")
            results.append((
                "bad signature rejected (400)",
                r.status_code == 400,
            ))

            # 5. missing signature header rejected with 400
            r = requests.post(
                f"{BASE}/webhook", data=raw_bad,
                headers={"Content-Type": "application/json"}, timeout=10,
            )
            results.append(("missing signature rejected (400)", r.status_code == 400))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    print("\n=== Live webhook server check ===")
    failed = False
    for name, ok in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        failed |= not ok
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
