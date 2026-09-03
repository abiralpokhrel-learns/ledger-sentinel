"""Generate the synthetic 50+ record batch for Ledger Sentinel.

Run:  python data/generate_synthetic_data.py
Produces three files in data/:
  - orders.csv            (our ledger's belief about each order)
  - webhook_events.jsonl  (the event stream, with planted anomalies)
  - settlement.csv        (what Razorpay actually paid out)

Every edge case the system must handle is planted on purpose. The comments
below mark each one so the README / dev-log can reference them precisely.
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import json
import os
import random
import sys
from pathlib import Path

from faker import Faker

# Allow running as a plain script: ensure repo root is importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import webhook_secret, TDS_RATE

random.seed(42)
Faker.seed(42)
fake = Faker()

HERE = Path(__file__).resolve().parent
SECRET = webhook_secret()

CATEGORIES = ["professional_services", "software", "retail_goods", "consulting", "food"]
N_ORDERS = 60


def sign(raw_body: str, secret: str) -> str:
    return hmac.new(secret.encode(), raw_body.encode(), hashlib.sha256).hexdigest()


def make_event(order_id, event_type, seq, ts, bad_sig=False):
    """Build one webhook event line.

    `raw_body` is the exact JSON string that gets signed, so the offline
    replay can re-send byte-identical payloads (the whole point of HMAC).
    """
    payload = {
        "event_id": f"evt_{order_id}_{seq}",
        "order_id": order_id,
        "event_type": event_type,
        "timestamp": ts,
        "contains": {"notes": fake.sentence()},
    }
    raw_body = json.dumps(payload, separators=(",", ":"))
    sig_secret = "wrong_secret" if bad_sig else SECRET
    return {
        "event_id": payload["event_id"],
        "order_id": order_id,
        "event_type": event_type,
        "timestamp": ts,
        "raw_body": raw_body,
        "signature": sign(raw_body, sig_secret),
        "bad_sig": bad_sig,
    }


def main():
    orders = []
    events = []
    settlements = []

    # Decide the lifecycle of each order up front.
    # Most captured; a handful failed (which will surface as missing/captured
    # settlement exceptions). One specific failed order becomes the late-auth case.
    plan = []
    for i in range(1, N_ORDERS + 1):
        oid = f"order_{i:04d}"
        amount = round(random.uniform(200, 50000), 2)
        mdr = round(amount * random.uniform(0.015, 0.025), 2)
        gst = round(mdr * 0.18, 2)
        category = random.choice(CATEGORIES)
        plan.append(
            {
                "order_id": oid,
                "amount": amount,
                "mdr": mdr,
                "gst": gst,
                "category": category,
            }
        )

    # Pick anomaly orders deterministically.
    failed_idx = [5, 17, 33, 48]            # orders that end up "failed"
    late_auth_idx = 33                       # failed in our system, captured in settlement
    tds_idx = [10, 22, 41]                   # TDS-shaped gaps
    rounding_idx = [3, 19, 55]               # within tolerance
    unexplained_idx = [27, 44]               # large unexplained gaps
    settlement_only_idx = N_ORDERS + 1       # settlement row with no order
    bad_sig_idx = [7, 29, 52]                # events delivered with bad signatures
    dup_idx = [12, 38]                       # duplicate event deliveries
    out_of_order_idx = 25                    # captured delivered before authorized

    bad_sig_order_ids = {plan[i - 1]["order_id"] for i in bad_sig_idx if i <= N_ORDERS}

    for n, p in enumerate(plan, start=1):
        oid = p["order_id"]
        is_failed = n in failed_idx
        is_late = n == late_auth_idx

        # --- orders.csv: our ledger's final belief about this order ---
        final_status = "failed" if is_failed else "captured"
        orders.append(
            {
                "order_id": oid,
                "amount": p["amount"],
                "mdr": p["mdr"],
                "gst": p["gst"],
                "category": p["category"],
                "status": final_status,
            }
        )

        # --- webhook_events.jsonl: the delivery stream ---
        base_ts = fake.unix_time()
        if is_failed and not is_late:
            seq_events = [("created", 0), ("failed", 2)]
        else:
            seq_events = [("created", 0), ("authorized", 1), ("captured", 2)]

        built = []
        for seq, (etype, rank) in enumerate(seq_events, start=1):
            bad = oid in bad_sig_order_ids and etype == "captured"
            built.append(make_event(oid, etype, seq, base_ts + rank * 10, bad_sig=bad))

        if n == out_of_order_idx:
            # PLANTED: out-of-order delivery. Send captured before authorized;
            # the state machine must drop the backward "authorized" update.
            built.sort(key=lambda e: 0 if e["event_type"] == "captured" else 1)

        if n in dup_idx:
            # PLANTED: duplicate delivery — same event_id arrives twice.
            built.append(dict(built[0]))

        events.extend(built)

        # --- settlement.csv ---
        net = round(p["amount"] - p["mdr"] - p["gst"], 2)
        if n in rounding_idx:
            # PLANTED: rounding-level noise, within tolerance -> should MATCH.
            settled = round(net + 0.005, 2)
            s_status = "captured"
        elif n in tds_idx:
            # PLANTED: TDS/TCS-shaped gap (~2% of gross) -> exception.
            settled = round(net - round(p["amount"] * TDS_RATE, 2), 2)
            s_status = "captured"
        elif is_late:
            # PLANTED: late-authorization flip. Our system says failed,
            # settlement says captured, amounts agree -> exception (status mismatch).
            settled = net
            s_status = "captured"
        elif n in unexplained_idx:
            # PLANTED: genuinely unexplained gap -> manual-review candidate.
            settled = round(net + 137.50, 2)
            s_status = "captured"
        elif is_failed and not is_late:
            # Honest failed order with no settlement row -> order-only exception.
            continue
        else:
            settled = net
            s_status = "captured"

        settlements.append(
            {
                "order_id": oid,
                "amount_settled": settled,
                "settlement_status": s_status,
                "utr": fake.bothify("UTR############"),
                # Deterministic fixed 2026 date so regeneration doesn't diff across days
                "settlement_date": f"2026-{random.randint(1,8):02d}-{random.randint(1,28):02d}",
            }
        )

    # PLANTED: a settlement row that has no corresponding order at all.
    orphan = f"order_{settlement_only_idx:04d}"
    settlements.append(
        {
            "order_id": orphan,
            "amount_settled": round(random.uniform(100, 1000), 2),
            "settlement_status": "captured",
            "utr": fake.bothify("UTR############"),
            "settlement_date": f"2026-{random.randint(1,8):02d}-{random.randint(1,28):02d}",
        }
    )

    # Write outputs
    with open(HERE / "orders.csv", "w", newline="") as f:
        w = csv.DictWriter(
            f, fieldnames=["order_id", "amount", "mdr", "gst", "category", "status"]
        )
        w.writeheader()
        w.writerows(orders)

    with open(HERE / "webhook_events.jsonl", "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")

    with open(HERE / "settlement.csv", "w", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "order_id",
                "amount_settled",
                "settlement_status",
                "utr",
                "settlement_date",
            ],
        )
        w.writeheader()
        w.writerows(settlements)

    # Report the planted inventory so the run is self-documenting.
    print(f"orders.csv:           {len(orders)} rows")
    print(f"webhook_events.jsonl: {len(events)} events "
          f"(planted duplicates on {dup_idx}, out-of-order on {out_of_order_idx}, "
          f"bad-sig on {bad_sig_idx})")
    print(f"settlement.csv:       {len(settlements)} rows "
          f"(rounding={rounding_idx}, tds={tds_idx}, late_auth={late_auth_idx}, "
          f"unexplained={unexplained_idx}, settlement_only={orphan})")


if __name__ == "__main__":
    main()
