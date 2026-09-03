"""Generate professional demo GIF for Ledger Sentinel."""
from PIL import Image, ImageDraw, ImageFont
import os

W, H = 960, 540
FPS = 10
BG = (15, 23, 42)  # slate-900
BG2 = (30, 41, 59)  # slate-800
ACCENT = (37, 99, 235)  # blue-600
GREEN = (5, 150, 105)
AMBER = (217, 119, 6)
WHITE = (255, 255, 255)
MUTED = (148, 163, 184)
LIGHT_BG = (248, 250, 252)

def get_font(size, bold=False):
    # Try to find a nice font, fallback to default
    candidates = [
        "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except:
                pass
    return ImageFont.load_default()

fonts = {
    "title": get_font(32, True),
    "subtitle": get_font(16, False),
    "mono": get_font(13, False),
    "mono_b": get_font(13, True),
    "small": get_font(11, False),
    "kpi_val": get_font(28, True),
    "kpi_label": get_font(9, True),
}

def rounded_rect(draw, xy, radius, fill, outline=None):
    x0, y0, x1, y1 = xy
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline)

def draw_header(img):
    draw = ImageDraw.Draw(img)
    # top bar
    draw.rectangle([0, 0, W, 48], fill=ACCENT)
    draw.text((20, 14), "◈  LEDGER SENTINEL", font=get_font(15, True), fill=WHITE)
    draw.text((W-200, 16), "Razorpay Buildathon 2026", font=get_font(11, False), fill=(219,234,254))
    return draw

frames = []

def add_frame(img, duration=100):
    frames.append((img.copy(), duration))

# Helpers to create base
def base_image():
    img = Image.new("RGB", (W, H), BG)
    return img

# Scene 1: Title card (2 sec)
for alpha in range(0, 20):
    img = base_image()
    draw = ImageDraw.Draw(img)
    # gradient-ish bg
    for y in range(H):
        c = int(15 + (30-15) * y / H)
        draw.line([(0, y), (W, y)], fill=(c, c+8, 42))
    draw_header(img)
    # center content
    # pulse effect
    scale = 1.0
    # title
    y0 = 140
    # fake logo box
    rounded_rect(draw, [W//2-60, y0, W//2+60, y0+36], 10, fill=(255,255,255))
    draw.text((W//2, y0+8), "◈  LEDGER", font=get_font(18, True), fill=ACCENT, anchor="mm")
    draw.text((W//2, y0+22), "SENTINEL", font=get_font(9, True), fill=MUTED, anchor="mm")
    y0 = 210
    txt = "Your finance assistant that checks"
    draw.text((W//2, y0), txt, font=get_font(22, True), fill=WHITE, anchor="mm")
    draw.text((W//2, y0+32), "what you expected  vs  what you got", font=get_font(15, False), fill=MUTED, anchor="mm")
    # subtitle pill
    pill = "Razorpay AI Finance Controller  •  Track 04"
    tw = draw.textlength(pill, font=fonts["small"])
    rounded_rect(draw, [W//2-tw//2-14, y0+62, W//2+tw//2+14, y0+82], 20, fill=(30,41,59), outline=(51,65,85))
    draw.text((W//2, y0+72), pill, font=fonts["small"], fill=(226,232,240), anchor="mm")
    # bottom hint
    draw.text((W//2, H-30), "Deterministic first  •  AI only on the residue", font=get_font(10, False), fill=(100,116,139), anchor="mm")
    add_frame(img, 100 if alpha < 19 else 800)

# Scene 2: Terminal pipeline (4 sec)
terminal_lines = [
    ("$ python data/generate_synthetic_data.py", MUTED),
    ("orders.csv:           60 rows", WHITE),
    ("webhook_events.jsonl: 179 events", WHITE),
    ("settlement.csv:       59 rows", WHITE),
    ("", WHITE),
    ("$ python -m app.main", MUTED),
    ("INFO ledger_sentinel: webhook events processed: 176 lines", (110,200,160)),
    ("", WHITE),
    ("=== Reconciliation summary ===", GREEN),
    ("  Total rows reconciled : 61", WHITE),
    ("  Matched               : 49", GREEN),
    ("  Exceptions            : 12", AMBER),
    ("  Match rate            : 80.3%", WHITE),
    ("  Exceptions by reason  : {'exception_tds_candidate': 3, ...}", MUTED),
]

for step in range(len(terminal_lines)+1):
    img = base_image()
    draw = ImageDraw.Draw(img)
    draw_header(img)
    # terminal window
    rounded_rect(draw, [40, 70, W-40, H-40], 12, fill=(2,6,23), outline=(51,65,85))
    # dots
    for i, col in enumerate([(239,68,68),(234,179,8),(34,197,94)]):
        draw.ellipse([58+i*18, 82, 68+i*18, 92], fill=col)
    draw.text((120, 82), "ledger-sentinel — bash", font=get_font(10, False), fill=MUTED)
    y = 110
    for i in range(step):
        text, color = terminal_lines[i]
        draw.text((58, y), text, font=fonts["mono"], fill=color)
        y += 18
        if y > H-50:
            break
    # blinking cursor on last line
    if step < len(terminal_lines):
        draw.text((58 + draw.textlength(terminal_lines[step][0] if step < len(terminal_lines) else "", font=fonts["mono"]), y), "█", font=fonts["mono"], fill=WHITE)
    add_frame(img, 180 if step < len(terminal_lines) else 900)

# Scene 3: Dashboard KPIs (3 sec)
for _ in range(1):
    img = Image.new("RGB", (W, H), LIGHT_BG)
    draw = ImageDraw.Draw(img)
    draw_header(img)
    # KPIs
    kpis = [
        ("MATCH RATE", "80.3%", "49 of 61 rows", GREEN),
        ("MATCHED", "49", "within Rs 0.01", GREEN),
        ("EXCEPTIONS", "12", "need attention", AMBER),
        ("AT RISK", "Rs 1,240", "sum of gaps", ACCENT),
    ]
    x = 30
    for label, val, sub, col in kpis:
        rounded_rect(draw, [x, 70, x+210, 140], 10, fill=WHITE, outline=(226,232,240))
        draw.text((x+14, 82), label, font=fonts["kpi_label"], fill=MUTED)
        draw.text((x+14, 96), val, font=fonts["kpi_val"], fill=col)
        draw.text((x+14, 124), sub, font=fonts["small"], fill=MUTED)
        # small bar
        draw.rounded_rectangle([x+14, 133, x+196, 136], radius=2, fill=(226,232,240))
        pct = 0.803 if label=="MATCH RATE" else 0.8 if label=="MATCHED" else 0.2
        draw.rounded_rectangle([x+14, 133, x+14+int(182*pct), 136], radius=2, fill=col)
        x += 225
    # chart area
    rounded_rect(draw, [30, 160, 500, 340], 10, fill=WHITE, outline=(226,232,240))
    draw.text((45, 172), "Exceptions by reason", font=get_font(11, True), fill=BG)
    # bar chart
    reasons = [("exception_tds",3, AMBER), ("status_mismatch",3, (100,116,139)), ("missing_sett.",3, (14,165,233)), ("unexplained",2, (239,68,68)), ("missing_order",1, (148,163,184))]
    max_c = 3
    bx, by, bw, bh = 60, 200, 400, 110
    # axes
    draw.line([(bx, by+bh), (bx+bw, by+bh)], fill=(203,213,225), width=1)
    for i, (name, cnt, col) in enumerate(reasons):
        bar_w = int(cnt / max_c * 300)
        y = by + i*20
        draw.rounded_rectangle([bx, y, bx+bar_w, y+14], radius=4, fill=col)
        draw.text((bx+bar_w+8, y+1), str(cnt), font=fonts["small"], fill=BG)
        draw.text((bx, y-10 if False else by+bh+6), "", font=fonts["small"], fill=MUTED)
        # label left
        draw.text((bx, y+1), "", font=fonts["small"], fill=MUTED)
    # y labels under
    draw.text((bx, by+bh+10), "exception_tds   status_mis   missing_set  unexplained  missing_ord", font=get_font(7, False), fill=MUTED)
    # gauge
    rounded_rect(draw, [520, 160, W-30, 260], 10, fill=WHITE, outline=(226,232,240))
    draw.text((535, 172), "Match gauge", font=get_font(11, True), fill=BG)
    # circular gauge simulation with arc
    cx, cy, r = 620, 215, 30
    draw.ellipse([cx-r, cy-r, cx+r, cy+r], outline=(226,232,240), width=6)
    # progress arc (approx with pieslice)
    draw.pieslice([cx-r, cy-r, cx+r, cy+r], start=-90, end=-90+ int(360*0.803), fill=GREEN, outline=GREEN)
    draw.ellipse([cx-r+10, cy-r+10, cx+r-10, cy+r-10], fill=WHITE)
    draw.text((cx, cy-2), "80%", font=get_font(12, True), fill=BG, anchor="mm")
    draw.text((W-80, 232), "health: good", font=fonts["small"], fill=GREEN)
    # priority inbox
    rounded_rect(draw, [520, 275, W-30, 500], 10, fill=WHITE, outline=(226,232,240))
    draw.text((535, 285), "Priority inbox — largest gaps", font=get_font(11, True), fill=BG)
    priorities = [
        ("order_0027", "unexplained", "Rs 137.50", (239,68,68)),
        ("order_0010", "TDS candidate", "Rs 218.45", AMBER),
        ("order_0022", "TDS candidate", "Rs 194.30", AMBER),
    ]
    y = 310
    for oid, reason, gap, col in priorities:
        rounded_rect(draw, [535, y, W-45, y+52], 8, fill=(248,250,252), outline=(226,232,240))
        draw.text((545, y+8), oid, font=get_font(11, True), fill=BG)
        draw.text((545, y+24), reason, font=fonts["small"], fill=MUTED)
        draw.text((W-90, y+18), gap, font=get_font(11, True), fill=col)
        y += 60
    add_frame(img, 2200)

# Scene 4: AI Chat (3 sec)
for _ in range(1):
    img = Image.new("RGB", (W, H), LIGHT_BG)
    draw = ImageDraw.Draw(img)
    draw_header(img)
    draw.text((30, 70), "Ask your ledger", font=get_font(16, True), fill=BG)
    draw.text((30, 90), "AI Finance Assistant  •  Claude or heuristic fallback", font=fonts["small"], fill=MUTED)
    # chat container
    rounded_rect(draw, [30, 115, W-30, 470], 12, fill=WHITE, outline=(226,232,240))
    draw.text((45, 128), "◉  AI Finance Assistant", font=get_font(11, True), fill=ACCENT)
    # user bubble
    rounded_rect(draw, [45, 155, W-80, 190], 12, fill=(239,246,255), outline=(191,219,254))
    draw.text((60, 165), "Why is order_0010 flagged?", font=get_font(11, False), fill=BG)
    draw.text((60, 178), "user", font=fonts["small"], fill=MUTED)
    # assistant bubble
    rounded_rect(draw, [45, 205, W-80, 300], 12, fill=(240,253,244), outline=(167,243,208))
    draw.text((60, 215), "order_0010  —  exception_tds_candidate", font=get_font(11, True), fill=BG)
    draw.text((60, 232), "Classification: expected_tds_withholding", font=fonts["small"], fill=GREEN)
    # wrap text
    note = "The shortfall matches a typical TDS/TCS"
    note2= "withholding rate — expected. No action needed."
    draw.text((60, 250), note, font=fonts["small"], fill=BG)
    draw.text((60, 266), note2, font=fonts["small"], fill=BG)
    draw.text((60, 284), "via heuristic  •  try: \"What is the match rate?\"", font=get_font(8, False), fill=MUTED)
    # second example faint
    rounded_rect(draw, [45, 315, W-80, 350], 12, fill=(254,249,195), outline=(253,224,71))
    draw.text((60, 325), "Summary: 49 matched, 12 exceptions — 80.3%  ", font=fonts["small"], fill=BG)
    # input bar
    rounded_rect(draw, [45, 420, W-45, 455], 20, fill=(248,250,252), outline=(226,232,240))
    draw.text((60, 432), "Ask about any order…  e.g.  \"Which are TDS?\"", font=fonts["small"], fill=MUTED)
    draw.ellipse([W-70, 426, W-50, 446], fill=ACCENT)
    draw.text((W-65, 433), "↑", font=get_font(12, True), fill=WHITE, anchor="mm")
    add_frame(img, 2200)

# Scene 5: Features grid + closing (3 sec)
for _ in range(1):
    img = Image.new("RGB", (W, H), LIGHT_BG)
    draw = ImageDraw.Draw(img)
    draw_header(img)
    draw.text((W//2, 80), "Built to stand out", font=get_font(18, True), fill=BG, anchor="mm")
    draw.text((W//2, 102), "Professional, interactive, audit-ready", font=fonts["small"], fill=MUTED, anchor="mm")
    feats = [
        ("◈  Pro Dashboard", "KPIs, charts, priority inbox", GREEN),
        ("◎  AI Assistant", "Chat with your ledger", ACCENT),
        ("⤓  Bring Your Own CSV", "Upload & reconcile live", (14,165,233)),
        ("⬇  PDF Report", "One-click audit deliverable", AMBER),
    ]
    x = 30
    for title, desc, col in feats:
        rounded_rect(draw, [x, 130, x+210, 230], 12, fill=WHITE, outline=(226,232,240))
        # icon circle
        draw.ellipse([x+16, 142, x+44, 170], fill=col)
        draw.text((x+30, 156), title[0], font=get_font(12, True), fill=WHITE, anchor="mm")
        draw.text((x+54, 146), title[2:], font=get_font(11, True), fill=BG)
        draw.text((x+16, 180), desc, font=fonts["small"], fill=MUTED)
        draw.text((x+16, 200), "→  Try it live", font=get_font(9, True), fill=col)
        x += 225
    # how to run
    rounded_rect(draw, [30, 250, W-30, 400], 12, fill=BG, outline=BG)
    draw.text((45, 265), "$  Try it in 30 seconds", font=get_font(11, True), fill=WHITE)
    cmds = [
        "pip install -r requirements.txt",
        "python data/generate_synthetic_data.py",
        "python -m app.main        # 80.3% match rate",
        "streamlit run dashboard/app.py",
    ]
    y = 285
    for c in cmds:
        draw.text((45, y), c, font=fonts["mono"], fill=(147,197,253))
        y += 18
    draw.text((W//2, 420), "GET /health  •  POST /ask  •  GET /report.pdf  •  POST /reconcile-upload", font=get_font(9, False), fill=MUTED, anchor="mm")
    # CTA
    rounded_rect(draw, [W//2-110, 440, W//2+110, 478], 20, fill=ACCENT)
    draw.text((W//2, 459), "Open Dashboard  →", font=get_font(12, True), fill=WHITE, anchor="mm")
    add_frame(img, 2500)

# Save GIF
out_path = "docs/demo.gif"
os.makedirs("docs", exist_ok=True)
# Deduplicate durations: Pillow wants list
images = [f[0] for f in frames]
durations = [f[1] for f in frames]
# Quantize for smaller file
images[0].save(out_path, save_all=True, append_images=images[1:], duration=durations, loop=0, optimize=True)
print(f"Wrote {out_path} ({os.path.getsize(out_path)} bytes, {len(frames)} frames)")
# Also copy to root for easy access
try:
    images[0].save("demo.gif", save_all=True, append_images=images[1:], duration=durations, loop=0, optimize=True)
    print("Also wrote demo.gif")
except:
    pass
