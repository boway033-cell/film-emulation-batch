# -*- coding: utf-8 -*-
"""批量输出校验：数量、尺寸（EXIF 方向是否已摆正）、大小、像素差统计"""
import glob, os, re, sys
from PIL import Image, ImageOps
import numpy as np

SRC = sys.argv[1] if len(sys.argv) > 1 else "."
OUT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(SRC, "film_portra400")
rx = re.compile(os.environ.get("FILM_PATTERN", r"^(DSC|_DSC)\d+\.JPG$"))
srcs = sorted(f for f in glob.glob(os.path.join(SRC, "*.JPG")) if rx.match(os.path.basename(f)))
SFX = "_" + os.path.basename(OUT).replace("film_", "")
outs = sorted(glob.glob(os.path.join(OUT, "*.jpg")))
print(f"源 {len(srcs)} 张 / 输出 {len(outs)} 张")

bad = []
dims_changed = 0
for s in srcs:
    stem = os.path.splitext(os.path.basename(s))[0]
    o = os.path.join(OUT, stem + SFX + ".jpg")
    if not os.path.exists(o):
        bad.append(stem); continue
    with Image.open(s) as im:
        exp = ImageOps.exif_transpose(im).size
    with Image.open(o) as im:
        got = im.size
        ori = im.getexif().get(274)
    if got != exp:
        bad.append(f"{stem} 期望{exp} 实际{got}")
    if ori not in (None, 1):
        bad.append(f"{stem} orientation={ori} 未清零")
    if got[1] > got[0]:
        dims_changed += 1
print("尺寸/方向异常:", bad if bad else "无")
print("竖构图输出张数:", dims_changed)

# 抽样：与源图的平均差与直方图位移
for stem in [os.path.splitext(os.path.basename(x))[0] for x in srcs[:5]]:
    s = os.path.join(SRC, stem + ".JPG")
    o = os.path.join(OUT, stem + SFX + ".jpg")
    if not os.path.exists(o):
        continue
    a = np.asarray(ImageOps.exif_transpose(Image.open(s)).convert("RGB"), np.float32)
    b = np.asarray(Image.open(o).convert("RGB"), np.float32)
    print(f"  {stem}: 尺寸{b.shape[1]}x{b.shape[0]} "
          f"平均|Δ|={np.abs(a-b).mean():.2f}/255  "
          f"亮度 {a.mean():.1f}→{b.mean():.1f}  "
          f"体积 {os.path.getsize(s)/1e6:.1f}MB→{os.path.getsize(o)/1e6:.1f}MB")
