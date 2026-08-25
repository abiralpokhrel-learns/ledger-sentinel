# Dev Log — Ledger Sentinel

Real failures encountered while building, written as they happened (not
reconstructed from memory). The point of keeping this is the Day 7 deliverable:
one honest "it broke, here's how I found it, here's how I fixed it" story.

---

## Failure: 0% match rate on the first end-to-end run

**When:** first full run of `python -m app.main` after wiring webhook events
into the database.

**Symptom:** every order landed as a `status_mismatch` exception and the
pipeline reported `Match rate: 0.0%`. Reconciliation can't be *that* bad on
clean synthetic data, so the result itself was the red flag.

**How I found it:** the `audit_log` is the canary. I queried the order statuses
directly and saw every order stuck at `created` — the `authorized` and
`captured` events had never taken effect even though `try_record_event` claimed
they were `applied`. A minimal repro (`debug2.py`) confirmed the verdict came
back `applied` but `get_order_status` still returned `None`.

**Root cause — two defects in `app/db.py` + `app/webhook.py`:**
1. `set_order_status` had its bind parameters swapped:
   ```python
   # wrong
   conn.execute("UPDATE orders SET status = ? WHERE order_id = ?", (order_id, status))
   # the SET got the order_id string and the WHERE matched nothing -> 0 rows updated
   ```
2. `try_record_event`, on the *first* event (when `current_state` is `None`),
   called `upsert_order(order_id, amount=0.0, status=event_type)`. That
   overwrote the order's real amount (loaded from `orders.csv`) with `0.0`,
   which would have corrupted the reconciliation amounts even if the status
   bug were fixed.

**How I fixed it:**
- Swapped the bind order in `set_order_status` to `(status, order_id)`.
- Rewrote the first-event branch in `try_record_event` to only *set the status*
  when the order already exists (it always does — it was loaded from
  `orders.csv`), and only insert a stub row when the order is genuinely absent.

**Verification:** re-ran the pipeline → `Match rate: 80.3%` (49/61), with the
12 exceptions breaking down exactly into the planted edge-case buckets
(TDS, late-auth, unexplained, missing settlement, missing order). Tests in
`tests/test_reconcile.py` were also extended to cover each case.

**Takeaway:** the audit trail is what turned an invisible wrong answer into a
findable one. If the pipeline had silently "matched" everything or crashed, I'd
have wasted far longer. Logging every row — matched or exception — is the single
most valuable design choice here.
