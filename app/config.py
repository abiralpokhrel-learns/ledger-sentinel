"""Shared configuration / constants for Ledger Sentinel.

Secrets are read from environment (or a local `.env`). A fixed dev default is
used when nothing is configured so the synthetic demo runs end-to-end without
setup. The webhook signer and verifier MUST use the same secret — that is the
entire point of HMAC verification, so we centralise it here.
"""
from __future__ import annotations

import os
from dotenv import load_dotenv

load_dotenv()

# Fixed default so generated data and the webhook handler agree out of the box.
# Override with the WEBHOOK_SECRET environment variable for real test-mode use.
DEFAULT_WEBHOOK_SECRET = "ledger_sentinel_dev_secret"


def webhook_secret() -> str:
    return os.getenv("WEBHOOK_SECRET", DEFAULT_WEBHOOK_SECRET)


def db_path() -> str:
    return os.getenv("LEDGER_DB_PATH", "ledger_sentinel.db")


def anthropic_api_key() -> str | None:
    return os.getenv("ANTHROPIC_API_KEY")


def anthropic_model() -> str:
    return os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")


def mcp_mode() -> str:
    return os.getenv("LEDGER_MCP_MODE", "remote")


# --- reconciliation constants ----------------------------------------------

# Never compare money with strict equality. ₹0.01 covers rounding noise.
TOLERANCE = 0.01

# Expected withholding band for the AI classifier to recognise as TDS/TCS.
# Gap as a fraction of gross order amount. Anything in this band and not a
# clean match is treated as a candidate "expected_tds_withholding".
TDS_RATE = 0.02  # 2% — representative TDS/TCS-shaped deduction
TDS_BAND = 0.005  # +/- 0.5pp slack around the nominal rate
