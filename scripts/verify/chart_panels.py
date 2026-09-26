# -*- coding: utf-8 -*-
"""生成富士/柯达胶片特性对照图：色调曲线、色彩交叉矢量、饱和度-亮度、色相带偏移。"""
import math, os, sys
import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import film_emulate as fe

OUT = sys.argv[1] if len(sys.argv) > 1 else "fuji-curves-and-crossover.png"
BG, FG, MUTED, GRID, PANEL = (255, 255, 255), (32, 34, 40), (126, 130, 140), (228, 230, 236), (249, 250, 252)

SERIES = [
    ("superia400", "Superia 400", (0, 148, 110)),
    ("classic_neg", "經典 Neg.", (32, 106, 176)),
    ("pro400h", "Pro 400H", (146, 106, 198)),
    ("velvia50", "Velvia 50", (214, 56, 44)),
    ("portra400", "Portra 400", (200, 152, 62)),
    ("gold200", "Gold 200", (146, 88, 30)),
]
F3 = [("superia400", "Superia 400", (0, 148, 110)),
      ("velvia50", "Velvia 50", (214, 56, 44)),
      ("gold200", "Gold 200", (146, 88, 30))]


def font(sz, bold=False):
    for p in (r"C:/Windows/Fonts/msyhbd.ttc" if bold else r"C:/Windows/Fonts/msyh.ttc",
              r"C:/Windows/Fonts/simhei.ttf", r"C:/Windows/Fonts/Deng.ttf"):
        try:
            return ImageFont.truetype(p, sz)
        except Exception:
            continue
    return ImageFont.load_default()


W = 1720
PAD, PW, PH, GAPX, GAPY = 48, 790, 520, 46, 74
H = 138 + PH + GAPY + PH + 46
img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)
d.text((PAD, 32), "胶片色调与色彩交叉对照：富士 vs 柯达", fill=FG, font=font(38, True))
d.text((PAD, 82), "全部曲线由 film-emulate.py 的预设参数直接计算，非示意图",
       fill=MUTED, font=font(20))

P = [(PAD, 138), (PAD + PW + GAPX, 138),
     (PAD, 138 + PH + GAPY), (PAD + PW + GAPX, 138 + PH + GAPY)]


def panel(idx, title, sub):
    x, y = P[idx]
    d.rounded_rectangle([x, y, x + PW, y + PH], 10, fill=PANEL, outline=GRID, width=1)
    d.text((x + 24, y + 16), title, fill=FG, font=font(24, True))
    d.text((x + 24, y + 50), sub, fill=MUTED, font=font(17))


def legend(idx, rows, y_off=84):
    x, y = P[idx]
    for ri, row in enumerate(rows):
        cx = x + 26
        for lab, col in row:
            d.line([cx, y + y_off + ri * 26 + 9, cx + 26, y + y_off + ri * 26 + 9], fill=col, width=4)
            d.text((cx + 32, y + y_off + ri * 26), lab, fill=FG, font=font(16))
            cx += 40 + d.textlength(lab, font=font(16)) + 26
    return y + y_off + len(rows) * 26 + 12


def area(idx, top):
    x, y = P[idx]
    return x + 74, top, x + PW - 30, y + PH - 48


def axes(x0, y0, x1, y1, xr, yr, xlab, ylab, xticks, yticks, xfmt="{:.0f}", yfmt="{:.0f}"):
    for v in xticks:
        px = x0 + (v - xr[0]) / (xr[1] - xr[0]) * (x1 - x0)
        d.line([px, y0, px, y1], fill=GRID)
        tw = d.textlength(xfmt.format(v), font=font(15))
        d.text((px - tw / 2, y1 + 8), xfmt.format(v), fill=MUTED, font=font(15))
    for v in yticks:
        py = y1 - (v - yr[0]) / (yr[1] - yr[0]) * (y1 - y0)
        d.line([x0, py, x1, py], fill=GRID)
        tw = d.textlength(yfmt.format(v), font=font(15))
        d.text((x0 - 10 - tw, py - 9), yfmt.format(v), fill=MUTED, font=font(15))
    d.line([x0, y1, x1, y1], fill=MUTED)
    d.line([x0, y0, x0, y1], fill=MUTED)
    tw = d.textlength(xlab, font=font(17))
    d.text(((x0 + x1) / 2 - tw / 2, max(y1 + 32, P[0][1])), xlab, fill=FG, font=font(17))
    d.text((x0 - 8, y0 - 32), ylab, fill=FG, font=font(17))
    return lambda vx, vy: (x0 + (vx - xr[0]) / (xr[1] - xr[0]) * (x1 - x0),
                           y1 - (vy - yr[0]) / (yr[1] - yr[0]) * (y1 - y0))


xs = np.linspace(0, 1, 512)

# ── ① 色调曲线 ────────────────────────────────────────────
panel(0, "① 色调曲线：暗部趾部与高光肩部", "横轴 = 输入亮度，纵轴 = 输出亮度；曲线越早弯折 = 高光越早滚降")
top = legend(0, rows=[[(l, c) for _, l, c in SERIES[:3]], [(l, c) for _, l, c in SERIES[3:]]])
ax0, ay0, ax1, ay1 = area(0, top + 40)
mp = axes(ax0, ay0, ax1, ay1, (0, 1), (0, 1), "输入亮度", "输出亮度",
          [0, 0.25, 0.5, 0.75, 1.0], [0, 0.25, 0.5, 0.75, 1.0], xfmt="{:.2f}", yfmt="{:.2f}")
d.line([mp(0, 0), mp(1, 1)], fill=(208, 211, 219), width=2)
d.text(mp(0.88, 0.86), "对角线", fill=MUTED, font=font(15))
for k, lab, col in SERIES:
    cu = fe.PRESETS[k]["curve"]
    ys = fe.curve_channel(xs, 0.0, 1.0, cu["contrast"], cu["shoulder"],
                          cu.get("shoulder_k", 3.2), cu.get("shadow_contrast", 0.0), cu.get("toe", 0.0))
    d.line([mp(float(a), float(b)) for a, b in zip(xs, ys)], fill=col, width=3)

# ── ② 色彩交叉矢量 ────────────────────────────────────────
panel(1, "② 色彩交叉：阴影偏色 → 高光偏色的走向", "横轴 R−G，纵轴 B−G（8bit 级）；箭头由阴影指向高光")
top = legend(1, rows=[[(l, c) for _, l, c in SERIES[:3]], [(l, c) for _, l, c in SERIES[3:]]])
ax0, ay0, ax1, ay1 = area(1, top + 20)
pairs = []
for k, lab, col in SERIES:
    c = fe.PRESETS[k]["crossover"]
    s, h = np.array(c["shadow"]) * 255, np.array(c["highlight"]) * 255
    pairs.append((lab, col, (s[0] - s[1], s[2] - s[1]), (h[0] - h[1], h[2] - h[1])))
mx = max(6.0, max(abs(v) for p in pairs for v in p[2] + p[3]) * 1.3)
mp = axes(ax0, ay0, ax1, ay1, (-mx, mx), (-mx, mx), "R − G（正 = 偏红/暖）", "B − G（正 = 偏蓝青）",
          [-mx, -mx / 2, 0, mx / 2, mx], [-mx, -mx / 2, 0, mx / 2, mx], xfmt="{:+.0f}", yfmt="{:+.0f}")
d.line([mp(-mx, 0), mp(mx, 0)], fill=(200, 203, 212), width=2)
d.line([mp(0, -mx), mp(0, mx)], fill=(200, 203, 212), width=2)
for lab, col, s, h in pairs:
    ps, ph = mp(*s), mp(*h)
    d.line([ps, ph], fill=col, width=3)
    d.ellipse([ps[0] - 5, ps[1] - 5, ps[0] + 5, ps[1] + 5], fill=col)
    d.ellipse([ph[0] - 8, ph[1] - 8, ph[0] + 8, ph[1] + 8], fill=col, outline=BG, width=2)
C2 = ["大点 = 高光，小点 = 阴影",
      "富士向左上（青绿阴影 → 品红高光）；柯达向右下（琥珀阴影 → 暖金高光）"]
cw = max(d.textlength(t, font=font(15)) for t in C2)
d.rectangle([ax0 + 4, ay1 - 50, ax0 + 12 + cw, ay1 - 6], fill=PANEL)
d.text((ax0 + 10, ay1 - 44), C2[0], fill=MUTED, font=font(15))
d.text((ax0 + 10, ay1 - 22), C2[1], fill=MUTED, font=font(15))

# ── ③ 饱和度 – 亮度 ──────────────────────────────────────
panel(2, "③ 饱和度随亮度：高光褪色程度", "1.0 = 不改；富士高光褪色更狠（白点更干净），暗部保留更多")
lg = [(f"{lab}  高光 {fe.PRESETS[k].get('sat_hi', 1):.2f}",
       fe.PRESETS[k].get("sat_hi", 1.0), col) for k, lab, col in SERIES]
top = legend(2, rows=[[(l, c) for l, _, c in lg[:3]], [(l, c) for l, _, c in lg[3:]]])
ax0, ay0, ax1, ay1 = area(2, top + 40)
mp = axes(ax0, ay0, ax1, ay1, (0, 1), (0.6, 1.3), "亮度", "饱和度倍率",
          [0, 0.25, 0.5, 0.75, 1.0], [0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3], xfmt="{:.2f}", yfmt="{:.1f}")
for k, lab, col in SERIES:
    p = fe.PRESETS[k]
    sat, sh, hi = p.get("sat", 1.0), p.get("sat_sh", p.get("sat", 1.0)), p.get("sat_hi", p.get("sat", 1.0))
    ys = np.where(xs < 0.5, sh + (sat - sh) * (xs * 2), sat + (hi - sat) * ((xs - 0.5) * 2))
    d.line([mp(float(a), float(b)) for a, b in zip(xs, ys)], fill=col, width=3)

# ── ④ 色相带偏移 ──────────────────────────────────────────
panel(3, "④ 色相带偏移：绿往哪边跑", "对每个色带的代表色实测的色相变化量（度）；正 = 向青/蓝，负 = 向黄/红")
top = legend(3, rows=[[(lab, col) for _, lab, col in F3]])
ax0, ay0, ax1, ay1 = area(3, top + 44)
BANDS = ["red", "orange", "yellow", "green", "cyan", "blue", "magenta"]
res = {}
for k in ("superia400", "velvia50", "gold200"):
    bands = fe.PRESETS[k].get("hue_bands", {})
    out = []
    for b in BANDS:
        c = np.array(fe._BAND_RGB[b], np.float32).reshape(1, 1, 3)
        o = fe.hue_shift(c, bands)[0, 0]

        def hd(v):
            y = 0.299 * v[0] + 0.587 * v[1] + 0.114 * v[2]
            return math.degrees(math.atan2(v[0] - y, v[2] - y))
        out.append((hd(o) - hd(c[0, 0]) + 540) % 360 - 180)
    res[k] = out
lim = max(4.0, max(abs(v) for vv in res.values() for v in vv) * 1.25)
mp = axes(ax0, ay0, ax1, ay1, (-0.55, len(BANDS) - 0.45), (-lim, lim), "色带", "",
          [], [-lim, -lim / 2, 0, lim / 2, lim], yfmt="{:+.0f}")
for j, b in enumerate(BANDS):
    tw = d.textlength(b, font=font(15))
    d.text((mp(j, 0)[0] - tw / 2, ay1 + 10), b, fill=MUTED, font=font(15))
zm = mp(0, 0)[1]
bw = 17
for si, (k, lab, col) in enumerate(F3):
    for j, v in enumerate(res[k]):
        cx = mp(j, 0)[0] - 1.5 * bw + si * bw
        cy = mp(0, v)[1]
        d.rectangle([cx, min(cy, zm), cx + bw - 5, max(cy, zm)], fill=col)
CAPS = ["绿带：富士正向量（青绿 / 祖母绿），柯达负向量（黄橄榄）",
        "黄带：两家都压，富士压得更狠",
        "红 / 蓝带：在富士上只加饱和、不改色相（柯达几乎不动）"]
for i, t in enumerate(CAPS):
    tw = d.textlength(t, font=font(15))
    d.text((ax1 - tw - 6, ay0 + 8 + i * 22), t, fill=MUTED, font=font(15))

img.save(OUT)
print("→", OUT, img.size)
