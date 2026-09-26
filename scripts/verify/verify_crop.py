# -*- coding: utf-8 -*-
"""1:1 像素级校验：全分辨率渲染若干预设，裁切同一区域拼成长条，用于检查颗粒与光晕。"""
import os, sys, time
import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import film_emulate as fe

SRC = sys.argv[1] if len(sys.argv) > 1 else "sample.jpg"
OUT = sys.argv[2] if len(sys.argv) > 2 else "verify_out"
PRESETS = ["portra400", "gold200", "superia400", "cinestill800t", "ektar100", "pro400h", "kodachrome", "hp5", "tri_x"]

os.makedirs(OUT, exist_ok=True)
img = fe._load_rgb(SRC)
W, H = img.size
arr = np.asarray(img, np.uint8)
print("source", W, "x", H)

# 两个 1:1 裁切区：A 高光/白墙+窗，B 阴影/暗部树叶
CROPS = [
    ("A_high", (int(W * 0.20), int(H * 0.25), int(W * 0.20) + 1100, int(H * 0.25) + 740)),
    ("B_shadow", (int(W * 0.55), int(H * 0.42), int(W * 0.55) + 1100, int(H * 0.42) + 740)),
]

for tag, box in CROPS:
    tiles = []
    for pname in PRESETS:
        t0 = time.time()
        r = fe.render(arr, fe.apply_overrides(fe.PRESETS[pname], grain=None, halation=None, vignette=None,
                                              sat=None, contrast=None, exposure=None), seed=20260926)
        crop = Image.fromarray(r, "RGB").crop(box)
        tiles.append((fe.PRESETS[pname]["label"], crop))
        print(f"  {tag} {pname:<14} {time.time()-t0:.1f}s")
    orig = img.crop(box)
    tiles.insert(0, ("ORIGINAL", orig))

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
    p = os.path.join(OUT, f"crop_{tag}_1to1.jpg")
    sheet.save(p, quality=95, subsampling=0)
    print("→", p)
