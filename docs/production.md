# Production deployment

## Docker (recommended)

```bash
cp .env.example .env   # set WEBHOOK_SECRET, ANTHROPIC_API_KEY etc.
docker compose up --build
# api: http://localhost:8000  dashboard: http://localhost:8501
curl http://localhost:8000/healthz
curl http://localhost:8000/readyz
curl http://localhost:8000/metrics
```

API and dashboard share a named volume `ledger_data` at `/data/ledger_sentinel.db`.
Override with `LEDGER_DB_PATH`.

## Bare metal

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python data/generate_synthetic_data.py
python -m app.main
uvicorn app.main:app --host 0.0.0.0 --port 8000
streamlit run dashboard/app.py --server.port 8501
```

## Health & observability

- `GET /health` — legacy, always 200
- `GET /healthz` — liveness, no DB
- `GET /readyz` — readiness, checks DB migrations
- `GET /metrics` — Prometheus text format (`ledger_sentinel_matched`, `exceptions`, `audit_rows`)
- Every response carries `x-request-id` (echo or generated) and security headers (`nosniff`, `DENY`).
- Enable JSON logs: `LEDGER_JSON_LOGS=1`

## Security

- HMAC webhook verification, idempotent state machine, rate limit (`LEDGER_RATE_LIMIT_PER_MIN=120` on `/webhook`, `/analyst/query`, `/detect`).
- NL analyst is read-only — `_validate_sql` blocks `INSERT/UPDATE/DELETE/DROP/ALTER/PRAGMA` and unknown tables.
- `LEDGER_STRICT=1` refuses dev-default `WEBHOOK_SECRET`.

## Config

All via env — see `.env.example`. No secrets in repo.
