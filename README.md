# Ledger Sentinel

> **Razorpay AI Buildathon 2026 · Track 04: AI Finance Controller**
> An agent that reconciles a batch of Razorpay orders, webhook events, and a
> settlement file — matching what's owed against what actually got paid out, and
> routing anything that doesn't cleanly match to an LLM for classification
> instead of a human.

---

## 1. Problem statement

A mid-size merchant on Razorpay processes thousands of orders a month. At
month-end, finance ops must reconcile **what Razorpay settled** against **what
the order ledger says was owed**, order by order. Done by hand — pulling two
CSV exports into a spreadsheet and eyeballing the differences — this routinely
lands around a **51% match rate** on the first pass: the rest are rounding
noise, TDS/TCS withholdings, late captures, and genuinely unexplained gaps that
each take a human 2–5 minutes to triage.

At scale that is real money stuck in limbo and real payroll burned on
mechanical matching. Structured tooling that does exact, tolerance-based
matching pushes the clean match rate to **88%+**, leaving only the ambiguous
few for human (or AI) judgment. **Ledger Sentinel** is that tooling: it closes
the reconciliation loop across a batch, reports its match rate honestly, and
isolates every unresolved item with a plain-English reason instead of a silent
failure.

## 2. What it does

1. Ingests a Razorpay **webhook event stream** (FastAPI), verifying each
   delivery with HMAC-SHA256 before anything else.
2. Applies a **deterministic state machine** with idempotency so duplicate and
   out-of-order deliveries are handled without manual intervention.
3. Loads the **settlement file** and reconciles it against orders with a
   money tolerance (never strict `==` on floats).
4. Routes only the **surviving exceptions** to an LLM (Anthropic) for
   classification and a human-readable audit note.
5. Writes **every row** — matched or exception — to an `audit_log`.
6. Shows the **match rate + exception table** on a one-screen Streamlit dashboard.

## 3. Architecture

```
                 ┌─────────────────────┐
Razorpay         │   Webhook Handler    │
test-mode  ──────▶  (FastAPI)           │
events            │  1. verify HMAC      │
                  │  2. check idempotent │
                  │  3. validate state   │
                  │     transition       │
                  └──────────┬───────────┘
                             │
                             ▼
                  ┌──────────────────────┐
                  │   SQLite             │
                  │   orders / events /   │
                  │   audit_log           │
                  └──────────┬───────────┘
                             │
                             ▼
                  ┌──────────────────────┐
Settlement  ─────▶│  Reconciliation      │
file (CSV)        │  Engine (Pandas)     │
                  │  tolerance-matched   │
                  └──────────┬───────────┘
                            │
              clean match ──┤── exception
                            │        │
                            ▼        ▼
                    audit_log   ┌──────────────┐
                                │  AI Classifier │
                                │  (Anthropic)   │
                                │  → audit note  │
                                └──────┬─────────┘
                                       ▼
                                  audit_log
                                       │
                                       ▼
                            ┌────────────────────┐
                            │  Streamlit Dashboard│
                            │  match rate +       │
                            │  exception table    │
                            └────────────────────┘
```

## 4. Why AI, why not

This split is the core design decision and the part the panel will probe.

**Deterministic (no AI — exact, auditable, replayable):**
- **Signature verification** — HMAC over raw request bytes. Cryptography, not judgment.
- **Idempotency** — `event_id` is a primary key; a duplicate delivery is acknowledged and skipped.
- **State machine** — forward-only transitions on `{created, authorized, captured, refunded, failed}`. Backward deliveries are dropped and logged.
- **Tolerance matching** — net payout (`amount − MDR − GST`) vs settled amount within ₹0.01. Money is never compared with `==`.

**AI (genuine judgment, only on the residue):**
- The **exceptions that survive deterministic matching** — TDS/TCS-shaped gaps, late-authorization flips, and unexplained differences — are sent to Claude for a single classification and a one-sentence audit note. The full batch is never sent; only isolated exception rows, so the model stays focused and fast.

The principle: *AI has no business deciding what is exact. It earns its place only where a human would otherwise have to make a judgment call.*

## 5. Setup

Prerequisites: Python 3.11+, and (optionally) Node.js + `npx` and Docker if you
want the live Razorpay MCP path instead of the synthetic data.

```bash
# 1. Clone and enter
cd ledger-sentinel

# 2. Create a venv and install
python -m venv .venv && .venv\Scripts\activate   # Windows
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
#   - set WEBHOOK_SECRET (or leave the dev default to run the demo as-is)
#   - set ANTHROPIC_API_KEY for real AI classification
#     (without it, the classifier falls back to a deterministic heuristic)

# 4. Generate the synthetic 50+ record batch (planted edge cases included)
python data/generate_synthetic_data.py

# 5. Run the full pipeline end to end
python -m app.main

# 6. (optional) Launch the dashboard in another terminal
streamlit run dashboard/app.py

# 7. (optional) Run the sanity tests
python -m pytest tests/ -q

# 8. (optional) Start the webhook server
uvicorn app.main:app --reload
```

> **Note on MCP:** `app/mcp_client.py` supports both the Remote
> (`npx mcp-remote`) and Local Docker Razorpay MCP servers. Because the
> buildathon uses synthetic data, the pipeline runs against `settlement.csv`;
> the MCP client is the live-data alternative and degrades gracefully if no
> test-mode credentials are present.

## 6. Result on the synthetic batch

Running the pipeline against the generated 61-row merged batch:

```
  Total rows reconciled : 61
  Matched               : 49
  Exceptions            : 12
  Match rate            : 80.3%
```

The 12 exceptions break down exactly as the planted edge cases intended:

| Reason | Count | Routed to AI as |
|---|---|---|
| `exception_tds_candidate` (TDS/TCS-shaped gap) | 3 | `expected_tds_withholding` |
| `status_mismatch` (late-auth flips / missed captures) | 3 | `late_authorization_flip` |
| `exception_unexplained` (large random gap) | 2 | `unresolved` |
| `missing_settlement` (failed orders, no payout) | 3 | `unresolved` |
| `missing_order` (settlement with no order) | 1 | `unresolved` |

The 3 "rounding-level" rows (within ₹0.01) were correctly auto-reconciled,
proving the tolerance band works where strict equality would have failed.

## 7. Developer log (real failure, Day 7-style)

A genuine bug surfaced while wiring the webhook state machine to the database —
documented live in `docs/dev-log.md`. Short version: two defects in the order
persistence layer (a swapped parameter in `UPDATE orders SET status = ?` and the
first event overwriting the order's real amount with `0.0`) left every order
stuck at `created` and produced a **0% match rate**. Both were caught because
the audit_log showed the impossible result, traced to `try_record_event`, and
fixed. The lesson — the audit trail is what makes a wrong answer *visible* —
is exactly why the design logs every row.

## 8. Repository layout

```
ledger-sentinel/
├── requirements.txt
├── .env.example
├── data/
│   └── generate_synthetic_data.py
├── app/
│   ├── main.py          # FastAPI entrypoint + end-to-end pipeline
│   ├── webhook.py       # signature / idempotency / state machine
│   ├── reconcile.py     # Pandas tolerance matching
│   ├── classify.py      # AI exception classification (+ heuristic fallback)
│   ├── db.py            # SQLite schema + helpers
│   ├── config.py        # shared constants / secrets
│   └── mcp_client.py    # Razorpay MCP integration (Remote/Local)
├── dashboard/
│   └── app.py           # Streamlit dashboard
├── tests/
│   └── test_reconcile.py
└── docs/
    └── dev-log.md
```
