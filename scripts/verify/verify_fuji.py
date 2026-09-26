# -*- coding: utf-8 -*-
"""富士 vs 柯达 1:1 校样：自动定位绿色最密集的窗口，逐预设全分辨率渲染后裁切拼接。"""
import os, sys, time
import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import film_emulate as fe

SRC = sys.argv[1] if len(sys.argv) > 1 else "sample.jpg"
OUT = sys.argv[2] if len(sys.argv) > 2 else "verify_out"
NAMES = (sys.argv[3].split(",") if len(sys.argv) > 3 else ["portra400","gold200","superia400","classic_neg","pro400h","velvia50","classic_chrome"])
CW, CH = 1100, 740
os.makedirs(OUT, exist_ok=True)

img = fe._load_rgb(SRC)
W, H = img.size
arr = np.asarray(img, np.uint8)

# 自动找绿色最密集的窗口
small = np.asarray(img.resize((W // 8, H // 8), Image.BOX), np.float32)
green = small[..., 1] - np.maximum(small[..., 0], small[..., 2])
k = 12
step = max(1, (small.shape[0] - k) // 40), max(1, (small.shape[1] - k) // 40)
best, by, bx = -1e9, 0, 0
for y in range(0, small.shape[0] - k, step[0]):
    for x in range(0, small.shape[1] - k, step[1]):
        v = float(green[y:y + k, x:x + k].mean())
        if v > best:
            best, by, bx = v, y, x
cy, cx = int(by * 8), int(bx * 8)
box = (min(max(0, cx), W - CW), min(max(0, cy), H - CH),
       min(max(0, cx), W - CW) + CW, min(max(0, cy), H - CH) + CH)
print(f"源 {W}x{H} | 绿度 {best:.1f} | 裁切 {box}")

tiles = [("ORIGINAL", img.crop(box))]
for n in NAMES:
    t0 = time.time()
    r = fe.render(arr, fe.apply_overrides(fe.PRESETS[n], grain=None, halation=None, vignette=None,
                                          sat=None, contrast=None, exposure=None), seed=20260926)
    tiles.append((fe.PRESETS[n]["label"], Image.fromarray(r, "RGB").crop(box)))
    print(f"  {n:<15} {time.time()-t0:.1f}s")

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
p = os.path.join(OUT, "crop_fuji_vs_kodak_1to1.jpg")
sheet.save(p, quality=95, subsampling=0)
print("→", p)
