"""Generate clean architecture diagram - crisp Pillow render, no messy text."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import textwrap

OUT = Path(__file__).resolve().parent.parent / "docs" / "architecture.png"
W, H = 1200, 1600
BG = (248, 250, 252)
WHITE = (255, 255, 255)
SLATE = (51, 65, 85)
SLATE_DARK = (15, 23, 42)
BLUE = (37, 99, 235)
AMBER = (217, 119, 6)
GREEN = (5, 150, 105)
GRAY = (148, 163, 184)
LIGHT_BORDER = (226, 232, 240)

# Try to load a clean font, fallback to default
try:
    # Try common Windows fonts
    import os
    font_bold_large = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", 28)
    font_bold = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", 16)
    font_bold_small = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", 13)
    font_reg = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 12)
    font_reg_small = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 11)
    font_mono = ImageFont.truetype("C:/Windows/Fonts/consola.ttf", 10)
except:
    font_bold_large = ImageFont.load_default()
    font_bold = ImageFont.load_default()
    font_bold_small = ImageFont.load_default()
    font_reg = ImageFont.load_default()
    font_reg_small = ImageFont.load_default()
    font_mono = ImageFont.load_default()

def rounded_rect(draw, xy, radius, fill, outline, width=2):
    x0, y0, x1, y1 = xy
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)

def draw_box(draw, x, y, w, h, title, subtitle, bullets, color, icon=""):
    # card
    x0, y0 = x - w//2, y - h//2
    x1, y1 = x + w//2, y + h//2
    rounded_rect(draw, (x0, y0, x1, y1), 16, WHITE, color, width=2)
    # left accent bar
    draw.rounded_rectangle((x0, y0, x0+6, y1), radius=6, fill=color)
    # icon circle
    if icon:
        draw.ellipse((x0+18, y0+14, x0+44, y0+40), fill=color)
        draw.text((x0+31, y0+27), icon, fill=WHITE, font=font_bold_small, anchor="mm")
        tx = x0 + 54
    else:
        tx = x0 + 18
    # title
    draw.text((tx, y0+16), title, fill=SLATE_DARK, font=font_bold, anchor="lm")
    if subtitle:
        draw.text((tx, y0+38), subtitle, fill=GRAY, font=font_reg_small, anchor="lm")
    # bullets
    by = y0 + 62
    for b in bullets:
        draw.ellipse((x0+20, by+3, x0+28, by+11), fill=color)
        draw.text((x0+36, by+3), b, fill=SLATE, font=font_reg, anchor="lm")
        by += 20

def arrow(draw, x, y0, y1, color=GRAY, width=3, label=""):
    # vertical arrow
    draw.line((x, y0, x, y1-14), fill=color, width=width)
    # arrowhead
    draw.polygon([(x-8, y1-14), (x+8, y1-14), (x, y1)], fill=color)
    if label:
        draw.text((x+14, (y0+y1)//2), label, fill=color, font=font_reg_small, anchor="lm")

def arrow_h(draw, x0, x1, y, color=GRAY, label="", label_color=GRAY):
    draw.line((x0, y, x1-14, y), fill=color, width=3)
    draw.polygon([(x1-14, y-6), (x1-14, y+6), (x1, y)], fill=color)
    if label:
        draw.text(((x0+x1)//2, y-14), label, fill=label_color, font=font_reg_small, anchor="mm")

img = Image.new("RGB", (W, H), BG)
draw = ImageDraw.Draw(img)

# Title
# top banner
rounded_rect(draw, (40, 20, W-40, 110), 16, WHITE, LIGHT_BORDER, width=1)
draw.text((W//2, 48), "LEDGER SENTINEL", fill=SLATE_DARK, font=font_bold_large, anchor="mm")
draw.text((W//2, 78), "Razorpay  AI  Buildathon  2026  \u00b7  Track 04  AI Finance Controller  +  Track 02  AI Defense", fill=GRAY, font=font_reg_small, anchor="mm")
draw.text((W//2, 98), "Deterministic first, AI last  \u00b7  Defense-only  \u00b7  Every rupee accounted for", fill=SLATE, font=font_reg_small, anchor="mm")

# Flow - vertical spine
# Positions
CX = W//2
LX = CX

# 1. Webhook Handler

draw_box(draw, CX, 200, 520, 110, "1  \u00b7  Webhook Handler", "FastAPI  +  HMAC  +  State Machine", [
    "Verify HMAC-SHA256 on raw bytes (constant-time)",
    "Idempotency by event_id (PK)  \u00b7  duplicate safe",
    "Forward-only state: created \u2192 captured  \u00b7  drops backward"
], SLATE, icon="1")
# Razorpay label left
draw.text((CX-360, 200), "Razorpay\nwebhook", fill=BLUE, font=font_bold_small, anchor="mm", align="center")
arrow_h(draw, CX-300, CX-260, 200, BLUE, label="raw bytes", label_color=BLUE)

arrow(draw, CX, 255, 295, SLATE, label="verified only")

# 2. SQLite Storage
draw_box(draw, CX, 370, 640, 100, "2  \u00b7  Storage  (SQLite  WAL)", "Single source of truth", [
    "orders  \u00b7  webhook_events  \u00b7  settlement  \u00b7  audit_log",
    "machine_decisions  vs  human_resolutions  (separate tables)"
], SLATE, icon="2")
arrow(draw, CX, 420, 460, SLATE)

# Settlement input from right
draw.text((CX+380, 430), "Settlement", fill=BLUE, font=font_bold_small, anchor="mm")
draw.text((CX+380, 448), "CSV  or  live MCP", fill=GRAY, font=font_reg_small, anchor="mm")
arrow_h(draw, CX+310, CX+290, 440, BLUE)

# 3. Reconciliation
draw_box(draw, CX, 540, 560, 110, "3  \u00b7  Reconciliation Engine", "pandas  \u00b7  tolerance  Rs 0.01", [
    "net_expected  =  amount \u2212 MDR \u2212 GST",
    "Match if  |net_expected \u2212 amount_settled|  \u2264  0.01",
    "+ status consistency check"
], SLATE, icon="3")
arrow(draw, CX, 595, 635, SLATE, label="exceptions only \u2192 AI")

# Split: left matched, right exception
# Matched branch - left
draw_box(draw, CX-280, 750, 380, 100, "Matched", "80.3%  on demo  (49/61)", [
    "Within tolerance",
    "Logged to audit_log"
], GREEN, icon="\u2713")
# Exception branch - right
draw_box(draw, CX+280, 750, 380, 100, "Exception", "12 / 61  isolated for AI", [
    "TDS  \u00b7  late flip  \u00b7  unexplained",
    "Needs explanation"
], AMBER, icon="!")
# Arrows from reconciliation to both
arrow_h(draw, CX-110, CX-110-90, 680, GREEN, label="clean", label_color=GREEN)
# Actually need two arrows: left and right diagonal
# Use vertical + horizontal
# Left fork
draw.line((CX-40, 635, CX-40, 680), fill=GREEN, width=3)
draw.line((CX-40, 680, CX-280, 680), fill=GREEN, width=3)
draw.line((CX-280, 680, CX-280, 690), fill=GREEN, width=3)
draw.polygon([(CX-288, 690), (CX-272, 690), (CX-280, 700)], fill=GREEN)
# Right fork
draw.line((CX+40, 635, CX+40, 680), fill=AMBER, width=3)
draw.line((CX+40, 680, CX+280, 680), fill=AMBER, width=3)
draw.line((CX+280, 680, CX+280, 690), fill=AMBER, width=3)
draw.polygon([(CX+272, 690), (CX+288, 690), (CX+280, 700)], fill=AMBER)

# 4. AI Classifier (below exception)
draw_box(draw, CX+280, 900, 440, 120, "4  \u00b7  AI Classifier", "Claude  Sonnet  /  heuristic fallback", [
    "expected_tds_withholding",
    "late_authorization_flip",
    "unresolved  +  one-line audit note",
    "Batched + cached (hash)  \u00b7  never blocks pipeline"
], AMBER, icon="4")
arrow(draw, CX+280, 810, 830, AMBER)
# 5. Cost-sensitive + Policy (center below)
draw_box(draw, CX, 1070, 700, 130, "5  \u00b7  Defense Layer", "Cost-Sensitive  \u00b7  Policy  \u00b7  Chargeback", [
    "Rolling windows (1h/6h)  \u00b7  flag only when rate spikes > mean+2\u03c3",
    "Cost = 25\u00d7FN + 1\u00d7FP  \u00b7  threshold on money, not accuracy",
    "Policy: signals \u2192 approve | step_up | review | block (deterministic)",
    "Chargeback: read-only evidence pack (draft, human must file)"
], SLATE_DARK, icon="5")
# arrows from AI and matched to defense
# From AI to defense (diagonal)
draw.line((CX+280, 960, CX+280, 990), fill=AMBER, width=3)
draw.line((CX+280, 990, CX+80, 990), fill=AMBER, width=3)
draw.line((CX+80, 990, CX+80, 1000), fill=AMBER, width=3)
draw.polygon([(CX+72, 1000), (CX+88, 1000), (CX+80, 1010)], fill=AMBER)
draw.text((CX+180, 982), "signals", fill=AMBER, font=font_reg_small, anchor="mm")
# From matched to defense (direct)
draw.line((CX-280, 810, CX-280, 990), fill=GREEN, width=2)
draw.line((CX-280, 990, CX-80, 990), fill=GREEN, width=2)
draw.line((CX-80, 990, CX-80, 1000), fill=GREEN, width=2)
draw.polygon([(CX-88, 1000), (CX-72, 1000), (CX-80, 1010)], fill=GREEN)

arrow(draw, CX, 1135, 1175, SLATE_DARK)

# 6. Outputs
draw_box(draw, CX-220, 1270, 380, 110, "Dashboard", "Streamlit  Pro", [
    "KPI  \u00b7  charts  \u00b7  priority inbox",
    "Honest metrics (held-out, FP \u20b9500)",
    "Policy playground + chargeback compiler"
], GREEN, icon="\u25C6")
draw_box(draw, CX+220, 1270, 380, 110, "Audit & API", "PDF  +  CSV  +  REST", [
    "GET /report.pdf  \u00b7  /export.csv",
    "POST /detect  /policy/decide",
    "GET /chargeback /metrics/honest"
], GREEN, icon="\u2261")
# arrows from defense to both outputs
draw.line((CX-60, 1175, CX-60, 1200), fill=GREEN, width=3)
draw.line((CX-60, 1200, CX-220, 1200), fill=GREEN, width=3)
draw.line((CX-220, 1200, CX-220, 1210), fill=GREEN, width=3)
draw.polygon([(CX-228, 1210), (CX-212, 1210), (CX-220, 1220)], fill=GREEN)
draw.line((CX+60, 1175, CX+60, 1200), fill=GREEN, width=3)
draw.line((CX+60, 1200, CX+220, 1200), fill=GREEN, width=3)
draw.line((CX+220, 1200, CX+220, 1210), fill=GREEN, width=3)
draw.polygon([(CX+212, 1210), (CX+228, 1210), (CX+220, 1220)], fill=GREEN)

# Footer note
rounded_rect(draw, (40, 1380, W-40, 1440), 12, WHITE, LIGHT_BORDER, width=1)
draw.text((W//2, 1402), "Deterministic spine (left) is exact and replayable", fill=SLATE_DARK, font=font_bold_small, anchor="mm")
draw.text((W//2, 1422), "AI touches only the residue  \u00b7  machine_decisions  vs  human_resolutions  \u00b7  defense-only, never moves money", fill=GRAY, font=font_reg_small, anchor="mm")

# Bottom badge
rounded_rect(draw, (W//2-180, 1460, W//2+180, 1495), 20, SLATE_DARK, SLATE_DARK, width=1)
draw.text((W//2, 1478), "\u25C6  Ledger Sentinel  \u00b7  v1.1 Pro  \u00b7  30 tests", fill=WHITE, font=font_bold_small, anchor="mm")

# Add subtle watermark / source
# draw.text((W-50, H-20), "docs/architecture.png", fill=GRAY, font=font_reg_small, anchor="rm")

img.save(OUT, "PNG", dpi=(200,200))
print(f"wrote {OUT} {img.size}")
