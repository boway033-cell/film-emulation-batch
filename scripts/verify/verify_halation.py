# -*- coding: utf-8 -*-
"""高光溢出（halation）强度视觉验证：同一裁切区，不同 amount 对比。"""
import os, sys, time
import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import film_emulate as fe

SRC = sys.argv[1] if len(sys.argv) > 1 else "sample.jpg"
OUT = sys.argv[2] if len(sys.argv) > 2 else "verify_out"
os.makedirs(OUT, exist_ok=True)

img = fe._load_rgb(SRC)
W, H = img.size
arr = np.asarray(img, np.uint8)
box = (int(W * 0.18), int(H * 0.20), int(W * 0.18) + 1200, int(H * 0.20) + 800)

CASES = [
    ("halation 0.00", {"halation": 0.0}),
    ("halation 0.30", {"halation": 0.30}),
    ("halation 0.90", {"halation": 0.90}),
    ("cinestill 0.60 + 大红晕", {"halation": 0.60, "tint": [1.0, 0.30, 0.16]}),
]

tiles = [("ORIGINAL", img.crop(box))]
for lab, ov in CASES:
    t0 = time.time()
    h = dict(fe.PRESETS["portra400"]["halation"])
    h.update(ov)
    p = fe.apply_overrides(fe.PRESETS["portra400"], grain=None, halation=ov["halation"], vignette=None,
                           sat=None, contrast=None, exposure=None)
    p["halation"] = h
    r = fe.render(arr, p, seed=20260926)
    tiles.append((lab, Image.fromarray(r, "RGB").crop(box)))
    print(f"  {lab:<26} {time.time()-t0:.1f}s")

cols = 2
tw, th = tiles[0][1].size
cap, pad = 26, 8
rows = (len(tiles) + cols - 1) // cols
sheet = Image.new("RGB", (cols * tw + (cols + 1) * pad, rows * (th + cap) + (rows + 1) * pad), (20, 20, 22))
try:
    font = ImageFont.load_default(size=18)
except Exception:
    font = ImageFont.load_default()
d = ImageDraw.Draw(sheet)
for i, (lab, im) in enumerate(tiles):
    r, c = divmod(i, cols)
    x = pad + c * (tw + pad)
    y = pad + r * (th + cap + pad)
    sheet.paste(im, (x, y))
    d.text((x + 2, y + th + 4), f"{lab}  1:1", fill=(240, 240, 245), font=font)
p = os.path.join(OUT, "crop_halation_1to1.jpg")
sheet.save(p, quality=95, subsampling=0)
print("→", p)
