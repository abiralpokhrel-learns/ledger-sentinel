"""Render docs/architecture.png — now delegates to generate_architecture_clean.py for crisp output.

    pip install Pillow
    python scripts/generate_architecture_clean.py  # preferred, crisp
    python scripts/generate_architecture.py        # legacy matplotlib
 — the diagram used in the README.

    pip install matplotlib   # docs-only dependency, not in requirements.txt
    python scripts/generate_architecture.py

Layout: deterministic spine down the left (webhook gates -> SQLite ->
reconciliation), AI branch on the right for exceptions only, dashboard at
the bottom. Arrows start/end at box borders; no line crosses a box.
"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "docs" / "architecture.png"

BLUE = "#2563eb"      # razorpay inputs
SLATE = "#334155"     # deterministic core
AMBER = "#d97706"     # ai layer
GREEN = "#059669"     # outputs / dashboard
GRAY = "#94a3b8"      # neutral arrows
BG = "#f8fafc"

BOX = dict(boxstyle="round,pad=0.35", linewidth=1.6)


def box(ax, x, y, w, h, title, lines, color, title_size=10.5):
    ax.add_patch(FancyBboxPatch((x - w / 2, y - h / 2), w, h,
                                facecolor="white", edgecolor=color, **BOX))
    ax.text(x, y + h / 2 - 0.17, title, ha="center", va="center",
            fontsize=title_size, fontweight="bold", color=color)
    body = "\n".join(lines)
    ax.text(x, y - 0.02, body, ha="center", va="center", fontsize=8.6,
            color="#1e293b", linespacing=1.5)


def arrow(ax, x0, y0, x1, y1, color=GRAY):
    ax.add_patch(FancyArrowPatch((x0, y0), (x1, y1), arrowstyle="-|>",
                                 mutation_scale=14, linewidth=1.5, color=color))


def label(ax, x, y, text, color="#475569", ha="center", size=7.8, style="italic"):
    ax.text(x, y, text, fontsize=size, color=color, ha=ha, va="center",
            style=style, linespacing=1.4)


def main():
    fig, ax = plt.subplots(figsize=(11.5, 14.5))
    ax.set_xlim(0, 10)
    ax.set_ylim(0.8, 16.0)
    ax.axis("off")
    fig.patch.set_facecolor(BG)

    LX, CX = 3.4, 5.2

    # --- title (plain text, no box) -------------------------------------------
    ax.text(CX, 15.55, "Ledger Sentinel", ha="center", va="center",
            fontsize=16, fontweight="bold", color=SLATE)
    ax.text(CX, 15.12, "Razorpay AI Buildathon · Track 04 · AI Finance Controller",
            ha="center", va="center", fontsize=9, color="#64748b")

    # --- deterministic spine ----------------------------------------------------
    box(ax, LX, 13.9, 4.0, 1.6, "Webhook Handler  (FastAPI)",
        ["1. verify HMAC-SHA256 over raw bytes",
         "2. idempotency via event_id PK",
         "3. forward-only state machine"], SLATE)
    label(ax, 0.72, 14.05, "Razorpay\ntest-mode\nevents", BLUE)
    arrow(ax, 1.02, 14.0, LX - 2.02, 14.0, BLUE)

    box(ax, CX, 12.0, 5.0, 1.1, "SQLite",
        ["orders · webhook_events · settlement", "audit_log (every row, timestamped)"],
        SLATE)
    arrow(ax, LX, 13.06, LX, 12.76)
    label(ax, LX + 0.18, 12.88, "verified events only", ha="left", size=7.4)

    box(ax, LX, 10.0, 4.4, 1.7, "Reconciliation Engine  (pandas)",
        ["expected net = amount − MDR − GST",
         "tolerance match within ₹0.01",
         "status consistency check"], SLATE)
    arrow(ax, LX, 11.41, LX, 11.08)
    label(ax, 7.15, 10.75, "Settlement\nfile (CSV)", BLUE)
    arrow(ax, 6.55, 10.5, LX + 2.26, 10.35, BLUE)

    # --- clean match branch (green, left) -----------------------------------------
    box(ax, 2.4, 7.9, 2.7, 1.15, "audit_log",
        ["outcome: matched", "reason: reconciled", "within tolerance"], GREEN,
        title_size=10)
    arrow(ax, 2.85, 8.97, 2.55, 8.68, GREEN)
    label(ax, 1.98, 8.68, "clean match", GREEN, ha="right")

    # --- exception branch (amber, right) --------------------------------------------
    box(ax, 7.6, 7.4, 3.9, 2.1, "AI Classifier  (Anthropic)",
        ['"expected_tds_withholding"',
         '"late_authorization_flip"',
         '"unresolved"',
         "+ one-sentence audit note each",
         "only isolated rows, never the batch"], AMBER)
    arrow(ax, 4.35, 8.97, 7.6, 8.74, AMBER)
    label(ax, 5.3, 9.32, "exception", AMBER)

    box(ax, 3.6, 5.3, 3.0, 1.15, "audit_log",
        ["outcome: exception", "classification +", "plain-english note"], AMBER,
        title_size=10)
    arrow(ax, 6.9, 6.16, 4.42, 6.08, AMBER)
    label(ax, 6.4, 5.82, "classification + note", AMBER, ha="left")

    # --- dashboard -------------------------------------------------------------------
    box(ax, CX, 2.9, 7.6, 1.5, "Streamlit Dashboard",
        ["match rate (graded over reconciliation rows)",
         "exception count + exception table with audit notes",
         "complete audit trail expander"], GREEN)
    arrow(ax, 1.75, 7.31, 1.75, 3.84, GREEN)
    arrow(ax, 3.6, 4.69, 4.6, 3.84, GREEN)

    ax.text(CX, 1.45, "deterministic spine (left) is exact and replayable — "
                      "AI touches only the residue it cannot decide",
            fontsize=8.8, color="#475569", ha="center", style="italic")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT, dpi=160, bbox_inches="tight", facecolor=BG)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
