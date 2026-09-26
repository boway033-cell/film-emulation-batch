# -*- coding: utf-8 -*-
"""色相带调整的正确性校验：绿色应向青绿旋转、黄被压、近灰不受影响、无带时空操作。"""
import math, os, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import film_emulate as fe


def hue_deg(rgb):
    r, g, b = float(rgb[0]), float(rgb[1]), float(rgb[2])
    y = 0.299 * r + 0.587 * g + 0.114 * b
    cb, cr = b - y, r - y
    return math.degrees(math.atan2(cr, cb)) % 360


def chroma(rgb):
    r, g, b = float(rgb[0]), float(rgb[1]), float(rgb[2])
    y = 0.299 * r + 0.587 * g + 0.114 * b
    return math.hypot(b - y, r - y)


S = fe.PRESETS["superia400"]["hue_bands"]
G = fe.PRESETS["gold200"]["hue_bands"]

# 真实世界取样点：树叶绿 / 草绿 / 天空蓝 / 混凝土灰 / 灰色 / 肤色
SAMPLES = {
    "foliage_green": [0.30, 0.44, 0.22],
    "grass_green":   [0.35, 0.50, 0.14],
    "olive":         [0.40, 0.42, 0.18],
    "sky_blue":      [0.42, 0.60, 0.82],
    "concrete_gray": [0.55, 0.55, 0.54],
    "neutral_gray":  [0.50, 0.50, 0.50],
    "skin":          [0.78, 0.60, 0.49],
    "amber":         [0.80, 0.60, 0.20],
}

print(f"{'样本':<15} {'原色相':>7} {'富士后':>7} {'Δ角度':>7} {'柯达后':>7} {'Δ角度':>7}")
print("-" * 62)
for k, v in SAMPLES.items():
    a = np.array(v, np.float32).reshape(1, 1, 3)
    f = fe.hue_shift(a, S)[0, 0]
    d = fe.hue_shift(a, G)[0, 0]
    h0, hf, hd = hue_deg(a[0, 0]), hue_deg(f), hue_deg(d)
    def dd(x, y):
        t = (y - x + 540) % 360 - 180
        return t
    print(f"{k:<15} {h0:7.1f} {hf:7.1f} {dd(h0, hf):+7.1f} {hd:7.1f} {dd(h0, hd):+7.1f}")

print("\n饱和度倍率（富士绿带 sat +0.16，黄带 sat -0.18）:")
for k in ("foliage_green", "grass_green", "amber"):
    a = np.array(SAMPLES[k], np.float32).reshape(1, 1, 3)
    out = fe.hue_shift(a, S)[0, 0]
    print(f"  {k:<14} chroma {chroma(a[0,0]):.4f} → {chroma(out):.4f}  "
          f"({chroma(out)/chroma(a[0,0]):.3f}×)")

# 空带必须完全不变
a = np.random.rand(4, 4, 3).astype(np.float32)
print("\n空 bands 往返最大误差:", float(np.abs(fe.hue_shift(a, None) - a).max()),
      float(np.abs(fe.hue_shift(a, {}) - a).max()))

# 灰阶必须几乎不动（chroma 加权）
for g in (0.15, 0.5, 0.85):
    a = np.full((1, 1, 3), g, np.float32)
    out = fe.hue_shift(a, S)[0, 0]
    print(f"  灰 {g:.2f} 最大通道偏移 {float(np.abs(out - g).max()):.6f}")
