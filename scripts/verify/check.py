# -*- coding: utf-8 -*-
"""检查：1) 高光阈值触发比例 2) .cube LUT 解析与三线性插值正确性"""
import os, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import film_emulate as fe

SAMPLE = sys.argv[1] if len(sys.argv) > 1 else "sample.jpg"
_TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp")
os.makedirs(_TMP, exist_ok=True)
img = fe._load_rgb(SAMPLE)
arr = np.asarray(img, np.uint8)
lum_srgb = (arr.astype(np.float32) / 255.0) @ fe.LUMA
print("luma 分位: max=%.3f p99.9=%.3f p99=%.3f p95=%.3f" % (
    lum_srgb.max(), np.percentile(lum_srgb, 99.9), np.percentile(lum_srgb, 99),
    np.percentile(lum_srgb, 95)))
lin = fe.srgb_to_linear(lum_srgb)
for thr in (0.72, 0.78, 0.82, 0.86, 0.90):
    tl = float(fe.srgb_to_linear(np.float32(thr)))
    frac = float(np.mean(lin > tl)) * 100
    print(f"  threshold sRGB {thr:.2f} (linear {tl:.3f}) → 触发像素 {frac:.2f}%")

# --- 光晕遮罩实际能量 ---
for name in ("portra400", "cinestill800t"):
    h = fe.build_halo_mask(arr, fe.PRESETS[name]["halation"], img.width, img.height)
    v = h.astype(np.float32) / 255.0
    print(f"  {name}: 光晕遮罩 mean={v.mean():.4f} max={v.max():.3f} 非零占比={np.mean(v > 0.02)*100:.2f}%")

# --- 3D LUT 往返测试（identity cube，应几乎不变） ---
p = os.path.join(_TMP, "identity.cube")
n = 17
with open(p, "w") as f:
    f.write(f"LUT_3D_SIZE {n}\n")
    for b in range(n):
        for g in range(n):
            for r in range(n):
                f.write(f"{r/(n-1):.6f} {g/(n-1):.6f} {b/(n-1):.6f}\n")
lut = fe.CubeLUT(p)
probe = np.random.rand(4096, 3).astype(np.float32).reshape(1, -1, 3)
out = lut.apply(probe)
print("  identity cube 往返最大误差: %.5f" % float(np.abs(out - probe).max()))
lut2 = fe.CubeLUT(  # 红蓝互换，验证通道顺序
    (lambda q: (open(q, "w").write(f"LUT_3D_SIZE {n}\n" + "".join(
        f"{b/(n-1):.6f} {g/(n-1):.6f} {r/(n-1):.6f}\n" for b in range(n) for g in range(n) for r in range(n))), q)[1]
    )(os.path.join(_TMP, "swap.cube")))
sw = lut2.apply(probe)
print("  swap cube 前两格应为 [b,g,r]:", probe[0, 1].round(3), "→", sw[0, 1].round(3))
