# -*- coding: utf-8 -*-
"""
film_emulate.py — 本地胶片感批量模拟引擎

管线：**源归一化**（RAW 显影 / 灰片反 log → 中性 sRGB）
      → **色彩与光影再创作**（独立 sRGB 中间图，可选保存）
      → 每通道曲线 LUT（黑位抬升 / S 曲线 / 趾部 / 肩部滚降）
      → 亮度假用插值的独立通道色偏（crossover）→ 可选外部 3D LUT
      → 色彩饱和（暗部 / 中间调 / 高光分档）→ 高光溢出（halation，线性亮度阈值）
      → 高斯颗粒（单色 + 色度，亮度调制）→ 暗角 → sRGB 输出

源归一化在 `--source`（默认 auto）控制下自动判断输入色彩格式：
  · RAW（ARW/CR2/NEF/DNG…）→ 显影成中性 sRGB（只需显影，RAW 不受机内 Log 影响）
  · 灰片 / Log 成片 → 反 log 还原成线性反射率再编码（先判断，再还原）
  · 普通成片（含手机照片）→ 原样通过
理由见 source_normalize.py 顶部的说明；预设是按「已正常显影的 sRGB 成片」标定的，
把 RAW 或灰片直接喂进来，等于让预设建在错误的对比与饱和度基准上。

依赖：Pillow + numpy（RAW 另需 rawpy）。纯本地、无网络、无 API。
内存策略：分带（band）处理 + 半分辨率光晕，24MP 单张峰值占用 < 200MB。
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps

try:
    import source_normalize as SN
except Exception:                                    # 允许单独拷走本文件使用
    SN = None

LUT_N = 4096          # 一维曲线 LUT 精度（1/4096 量化，肉眼不可见）
BAND = 512            # 分带高度（行）
LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)


# ══════════════════════════════════════════════════════════ 色彩工具
def srgb_to_linear(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4).astype(np.float32)


def luma_of(rgb: np.ndarray) -> np.ndarray:
    """rgb (h,w,3) float32 → (h,w) 亮度"""
    return rgb @ LUMA


def smoothstep(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


def gauss_blur_fft(a: np.ndarray, sigma: float) -> np.ndarray:
    """FFT 高斯模糊（不依赖 scipy / cv2，任何 dtype 都可用）。a: (h,w) float32"""
    if sigma <= 0:
        return a
    h, w = a.shape
    # rfft2 只对最后一轴取半谱：轴 0 用 fftfreq(h)，轴 1 用 rfftfreq(w)
    fy = np.fft.fftfreq(h).astype(np.float32)[:, None]
    fx = np.fft.rfftfreq(w).astype(np.float32)[None, :]
    g = np.exp(-2.0 * (math.pi ** 2) * (sigma ** 2) * (fy ** 2 + fx ** 2))
    out = np.fft.irfft2(np.fft.rfft2(a.astype(np.float32)) * g, s=(h, w))
    return out.astype(np.float32)


# ══════════════════════════════════════════════════════════ 曲线
def curve_channel(x, lift, gamma, contrast, shoulder, shoulder_k=3.2,
                  shadow_contrast=0.0, toe=0.0):
    """单通道曲线。x in [0,1]（可传 LUT 采样点数组）

    顺序：gamma（中间调位置）→ 全局 S 曲线 → 暗部硬度 → 趾部（暗部软着陆）
          → 肩部（高光滚降）→ 黑位抬升（胶片基灰）

    shadow_contrast > 0 只压暗部（Fuji 的"暗部硬调"），不动中间调与白点；
    shoulder_k 越大高光滚降越陡（反转片），越小越早越柔（Pro 400H / Eterna）。
    """
    y = np.clip(x, 0.0, 1.0)

    if gamma != 1.0:
        y = np.power(y, 1.0 / gamma)

    if contrast > 0:
        s = y * y * (3.0 - 2.0 * y)                      # smoothstep 保端点
        y = y * (1.0 - contrast) + s * contrast

    if shadow_contrast != 0.0:
        s = y * y * (3.0 - 2.0 * y)
        w = np.clip(1.0 - y / 0.50, 0.0, 1.0) ** 1.2      # 只在暗部区生效
        t = shadow_contrast * w
        y = y * (1.0 - t) + s * t                         # t>0 压暗，t<0 提暗

    if toe > 0:
        soft = 2.0 * y - y * y                            # 抬暗部，保 0/1
        w = np.clip(1.0 - y / 0.45, 0.0, 1.0) ** 1.5
        t = toe * w
        y = y * (1.0 - t) + soft * t

    if shoulder < 1.0:
        t = np.clip((y - shoulder) / (1.0 - shoulder), 0.0, 1.0)
        k = max(0.35, shoulder_k)
        tt = (1.0 - np.exp(-k * t)) / (1.0 - np.exp(-k))
        y = np.where(y > shoulder, shoulder + (1.0 - shoulder) * tt, y)

    if lift > 0:
        y = lift + (1.0 - lift) * y

    return np.clip(y, 0.0, 1.0)


def build_lut(curve: dict, bw: bool = False) -> np.ndarray:
    """→ (3, LUT_N) float32，单调递增保证"""
    x = np.linspace(0.0, 1.0, LUT_N, dtype=np.float32)
    lift = curve.get("lift", [0.0, 0.0, 0.0])
    gamma = curve.get("gamma", [1.0, 1.0, 1.0])
    sk = curve.get("shoulder_k", 3.2)
    sc = curve.get("shadow_contrast", 0.0)
    lut = np.empty((3, LUT_N), dtype=np.float32)
    for c in range(3):
        v = curve_channel(x, lift[c], gamma[c], curve["contrast"],
                          curve["shoulder"], sk, sc, curve.get("toe", 0.0))
        lut[c] = np.maximum.accumulate(v)                 # 强制单调，杜绝反转
    return lut


# ══════════════════════════════════════════════════════════ 色相带选择性调整
# 逐通道曲线无法表达"绿色向青绿偏移"这类色相变化，必须在对偶空间做。
# 用 Rec.601 的 Y/Cb/Cr（可精确逆变换），把每个色相带表示成 (Cb,Cr) 平面的
# 单位方向，权重 = 与该方向的余弦相似度 → 免 atan2、免三角函数。
_R601 = np.array([0.299, 0.587, 0.114], dtype=np.float32)
_BAND_RGB = {
    "red": (1.0, 0.0, 0.0), "orange": (1.0, 0.5, 0.0), "yellow": (1.0, 1.0, 0.0),
    "green": (0.0, 1.0, 0.0), "cyan": (0.0, 1.0, 1.0), "blue": (0.0, 0.0, 1.0),
    "magenta": (1.0, 0.0, 1.0),
}


def _band_dirs():
    out = {}
    for k, (r, g, b) in _BAND_RGB.items():
        y = 0.299 * r + 0.587 * g + 0.114 * b
        cb, cr = b - y, r - y
        n = math.hypot(cb, cr) or 1.0
        out[k] = (cb / n, cr / n)
    return out


_BAND_DIR = _band_dirs()


def hue_shift(rgb: np.ndarray, bands: dict) -> np.ndarray:
    """按色相带做选择性色相旋转 + 饱和度增益。

    bands: {"green": {"hue": +7, "sat": +0.15, "width": 45, "power": 1.5}, ...}
      hue   单位「度」，正=在 (Cb,Cr) 平面逆时针旋转。绿(225°)→青(293°) 即正值；
            负值则绿→黄橄榄色（柯达方向）。
      sat   ± 比例，作用域内该色带的饱和度倍率
      width 角度容差（度），默认 45
    """
    if not bands:
        return rgb
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    Y = 0.299 * r + 0.587 * g + 0.114 * b
    Cb = b - Y
    Cr = r - Y
    chroma = np.sqrt(Cb * Cb + Cr * Cr)
    safe = np.maximum(chroma, 1e-6)
    ub, ur = Cb / safe, Cr / safe

    angle = np.zeros_like(Y)
    gain = np.ones_like(Y)
    for name, cfg in bands.items():
        d = _BAND_DIR.get(name)
        if d is None:
            continue
        width = float(cfg.get("width", 45.0))
        cw = math.cos(math.radians(min(179.0, width)))
        w = (ub * d[0] + ur * d[1] - cw) / max(1e-6, 1.0 - cw)
        w = np.clip(w, 0.0, 1.0) ** float(cfg.get("power", 1.5))
        w *= np.clip(chroma / 0.10, 0.0, 1.0)             # 近灰像素不受影响
        angle += math.radians(float(cfg.get("hue", 0.0))) * w
        gain += float(cfg.get("sat", 0.0)) * w

    if not np.any(angle) and not np.any(gain != 1.0):
        return rgb
    # 小位移沿用旧算法；大位移用多项式近似三角函数，避免强化后色度虚增。
    if float(np.max(np.abs(angle))) <= 0.3:
        Cb2 = (Cb - angle * Cr) * gain
        Cr2 = (angle * Cb + Cr) * gain
    else:
        a2 = angle * angle
        ca = 1.0 - 0.5 * a2 + a2 * a2 / 24.0
        sa = angle * (1.0 - a2 / 6.0 + a2 * a2 / 120.0)
        Cb2 = (ca * Cb - sa * Cr) * gain
        Cr2 = (sa * Cb + ca * Cr) * gain
    R = Y + Cr2
    B = Y + Cb2
    G = (Y - 0.299 * R - 0.114 * B) / 0.587
    return np.stack([R, G, B], axis=-1)


def wb_gain(temp: float, tint: float) -> np.ndarray:
    """简易白平衡增益（temp>0 偏暖，tint>0 偏绿），用于复现各家底片的白点倾向。"""
    t, n = float(temp), float(tint)
    g = np.array([1.0 + 0.07 * t + 0.030 * n,
                  1.0 - 0.05 * n,
                  1.0 - 0.07 * t + 0.030 * n], dtype=np.float32)
    return g


def creative_color_light(rgb: np.ndarray, direction: str, strength: float) -> np.ndarray:
    """基础还原之后的独立再创作：分区光影、冷暖分色与色度塑形。"""
    if strength <= 0:
        return rgb
    lum = luma_of(rgb)
    sh = np.clip((0.55 - lum) / 0.55, 0.0, 1.0) ** 1.5
    hi = np.clip((lum - 0.45) / 0.55, 0.0, 1.0) ** 1.5
    if direction == "warm":
        rgb = rgb + strength * (sh[..., None] * np.array([0.014, 0.003, -0.018], np.float32)
                                + hi[..., None] * np.array([0.035, 0.015, -0.026], np.float32))
        chroma_gain, punch = 1.06, 0.12
    elif direction == "cool":
        rgb = rgb + strength * (sh[..., None] * np.array([-0.026, 0.008, 0.030], np.float32)
                                + hi[..., None] * np.array([-0.009, 0.004, 0.012], np.float32))
        chroma_gain, punch = 0.98, 0.14
    elif direction == "vivid":
        chroma_gain, punch = 1.22, 0.22
    elif direction == "soft":
        chroma_gain, punch = 0.86, -0.13
    else:  # mono: 仅做黑白影调，不重新引入颜色
        chroma_gain, punch = 1.0, 0.15
    lum = luma_of(rgb)
    target_lum = lum + strength * punch * (smoothstep(lum) - lum)
    if direction == "soft":
        target_lum = target_lum * (1.0 - 0.060 * strength) + 0.030 * strength
    rgb = target_lum[..., None] + (rgb - lum[..., None]) * (1.0 + (chroma_gain - 1.0) * strength)
    return rgb


def _prep(block: np.ndarray, lut: np.ndarray, ev: float, wb: np.ndarray | None) -> np.ndarray:
    """曝光 → 白平衡 → 每通道曲线 LUT（分带与 acutance 的 pad 区共用同一套前置步骤）"""
    if ev:
        block = np.clip(block * (2.0 ** ev), 0.0, 1.0)
    if wb is not None:
        block = np.clip(block * wb[None, None, :], 0.0, 1.0)
    idx = np.clip((block * LUT_N).astype(np.int32), 0, LUT_N - 1)
    out = np.empty_like(block)
    for c in range(3):
        out[..., c] = lut[c][idx[..., c]]
    return out


# ══════════════════════════════════════════════════════════ 外部 3D LUT（.cube）
class CubeLUT:
    def __init__(self, path: str):
        self.size, self.data = self._parse(path)

    @staticmethod
    def _parse(path):
        n = None
        vals = []
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                if s.upper().startswith("LUT_3D_SIZE"):
                    n = int(s.split()[-1])
                elif s.upper().startswith(("TITLE", "DOMAIN_MIN", "DOMAIN_MAX", "LUT_1D_SIZE")):
                    continue
                else:
                    parts = s.split()
                    if len(parts) == 3:
                        try:
                            vals.append([float(p) for p in parts])
                        except ValueError:
                            pass
        if n is None or len(vals) != n ** 3:
            raise ValueError(f"无法解析 3D LUT：{path}（size={n}, 点数={len(vals)}）")
        # .cube 顺序：r 变化最快 → 末轴为 r；转置成 data[r, g, b]
        cube = np.asarray(vals, dtype=np.float32).reshape(n, n, n, 3).transpose(2, 1, 0, 3)
        return n, np.ascontiguousarray(cube)

    def apply(self, rgb: np.ndarray, strength: float = 1.0) -> np.ndarray:
        """三线性插值。rgb (h,w,3) float32 [0,1]"""
        n = self.size
        p = np.clip(rgb, 0.0, 1.0) * (n - 1)
        i0 = np.floor(p).astype(np.int32)
        i1 = np.minimum(i0 + 1, n - 1)
        f = (p - i0).astype(np.float32)
        r0, g0, b0 = i0[..., 0], i0[..., 1], i0[..., 2]
        r1, g1, b1 = i1[..., 0], i1[..., 1], i1[..., 2]
        fr, fg, fb = f[..., 0:1], f[..., 1:2], f[..., 2:3]
        d = self.data
        c000 = d[r0, g0, b0]; c100 = d[r1, g0, b0]
        c010 = d[r0, g1, b0]; c110 = d[r1, g1, b0]
        c001 = d[r0, g0, b1]; c101 = d[r1, g0, b1]
        c011 = d[r0, g1, b1]; c111 = d[r1, g1, b1]
        c00 = c000 * (1 - fr) + c100 * fr
        c10 = c010 * (1 - fr) + c110 * fr
        c01 = c001 * (1 - fr) + c101 * fr
        c11 = c011 * (1 - fr) + c111 * fr
        c0 = c00 * (1 - fg) + c10 * fg
        c1 = c01 * (1 - fg) + c11 * fg
        out = c0 * (1 - fb) + c1 * fb
        return rgb * (1.0 - strength) + out * strength


# ══════════════════════════════════════════════════════════ 光晕（halation）
def build_halo_mask(arr: np.ndarray, hal: dict, w: int, h: int) -> np.ndarray:
    """在全图 1/4 分辨率上算溢出遮罩，再放大为 uint8 全图（省内存）。

    阈值以 sRGB 等效值给出（0~1），内部转线性亮度，符合物理散射的衰减。
    """
    amt = hal.get("amount", 0.0)
    if amt <= 0:
        return np.zeros((0, 0), dtype=np.uint8)

    sw, sh = max(1, w // 4), max(1, h // 4)
    small = np.asarray(Image.fromarray(arr).resize((sw, sh), Image.BOX), dtype=np.float32) / 255.0
    lin = srgb_to_linear(small)
    lum = lin @ LUMA

    thr = srgb_to_linear(np.float32(hal.get("threshold", 0.85)))
    m = np.clip((lum - thr) / max(1e-4, 0.55 * (1.0 - thr)), 0.0, 1.0)
    m = smoothstep(m).astype(np.float32)

    long_edge = max(w, h)
    r_wide = max(0.6, hal.get("radius", 0.006) * long_edge / 4.0)   # 已按 1/4 缩放
    r_tight = max(0.3, r_wide * 0.28)

    # 注：PIL 的 GaussianBlur 不支持 'F' 模式，这里用 FFT 高斯（1/4 分辨率下极快）
    wide = gauss_blur_fft(m, r_wide)
    tight = gauss_blur_fft(m, r_tight)

    halo = np.clip(wide * 0.78 + tight * 0.55
                   - m * hal.get("core_protect", 0.0), 0.0, 1.0)
    halo = Image.fromarray((halo * 255.0).astype(np.uint8), mode="L").resize((w, h), Image.BILINEAR)
    return np.asarray(halo, dtype=np.uint8)


# ══════════════════════════════════════════════════════════ 颗粒
def grain_band(rng, y0, y1, h, w, sigma, chroma_amt):
    """生成该带所需的颗粒场（单色 1 通道 + 可选色度 3 通道），带 pad 边距做 FFT 模糊后裁切。"""
    pad = int(math.ceil(3.0 * sigma)) + 4 if sigma > 0 else 2
    a0 = max(0, y0 - pad)
    a1 = min(h, y1 + pad)
    fh = a1 - a0

    mono = rng.standard_normal((fh, w)).astype(np.float32)
    if sigma > 0:
        mono = gauss_blur_fft(mono, sigma)
    s = float(mono.std())
    if s > 1e-6:
        mono /= s
    mono = mono[y0 - a0: y0 - a0 + (y1 - y0)]

    chroma = None
    if chroma_amt > 0:
        chroma = rng.standard_normal((y1 - y0, w, 3)).astype(np.float32)
    return mono, chroma


def apply_creative_stage(arr: np.ndarray, preset: dict) -> np.ndarray:
    """阶段二：对已还原色彩的照片再创作，返回可交给胶卷引擎的 sRGB 图。"""
    strength = preset.get("creative_strength", 0.0)
    if strength <= 0:
        return arr
    out = np.empty_like(arr)
    for y0 in range(0, arr.shape[0], BAND):
        y1 = min(y0 + BAND, arr.shape[0])
        base = arr[y0:y1].astype(np.float32) / 255.0
        graded = creative_color_light(base, preset["creative_direction"], strength)
        out[y0:y1] = np.clip(graded * 255.0 + 0.5, 0, 255).astype(np.uint8)
    return out


# ══════════════════════════════════════════════════════════ 主渲染
def render(arr: np.ndarray, preset: dict, seed: int = 20260926,
           res_scale: float = 1.0, lut3d: CubeLUT | None = None,
           lut_strength: float = 1.0) -> np.ndarray:
    """arr: uint8 (h,w,3) → uint8 (h,w,3)。res_scale 用于预览时按比例缩放颗粒/光晕尺寸。"""
    h, w = arr.shape[:2]
    lut = build_lut(preset["curve"], preset.get("bw", False))

    if preset.get("bw", False):
        halo = np.zeros((0, 0), dtype=np.uint8)
    else:
        halo = build_halo_mask(arr, preset.get("halation", {}), w, h)

    hal_amt = preset.get("halation", {}).get("amount", 0.0)
    hal_tint = np.asarray(preset.get("halation", {}).get("tint", [1.0, 0.5, 0.35]), dtype=np.float32)
    if preset.get("bw", False):
        hal_amt = 0.0

    cross = preset.get("crossover")
    sh = np.asarray(cross["shadow"], dtype=np.float32) if cross else None
    hi = np.asarray(cross["highlight"], dtype=np.float32) if cross else None

    bw_mix = np.asarray(preset.get("mix", [0.2126, 0.7152, 0.0722]), dtype=np.float32)
    tone = np.asarray(preset.get("tone", [0.0, 0.0, 0.0]), dtype=np.float32)

    sat = preset.get("sat", 1.0)
    sat_hi = preset.get("sat_hi", sat)
    sat_sh = preset.get("sat_sh", sat)

    g = preset.get("grain", {}) or {}
    g_amt = g.get("amount", 0.0)
    g_sigma = g.get("size", 1.0) * max(0.35, res_scale)
    g_chroma = g.get("chroma", 0.0)

    v_amt = preset.get("vignette", 0.0)
    ev = preset.get("exposure", 0.0)
    wb = wb_gain(preset.get("temp", 0.0), preset.get("tint", 0.0))
    if preset.get("temp", 0.0) == 0.0 and preset.get("tint", 0.0) == 0.0:
        wb = None

    hue_bands = preset.get("hue_bands")

    ac = preset.get("acutance", {}) or {}
    ac_amt = ac.get("amount", 0.0)
    ac_r = max(0.4, ac.get("radius", 1.0) * max(0.5, res_scale))

    # 暗角（可分离：rx² + ry²）
    if v_amt > 0:
        xs = (np.arange(w, dtype=np.float32) - (w - 1) / 2.0) / ((w - 1) / 2.0)
        ys = (np.arange(h, dtype=np.float32) - (h - 1) / 2.0) / ((h - 1) / 2.0)
        rx2 = (xs ** 2)[None, :]
        ry2 = (ys ** 2)

    rng = np.random.default_rng(seed)
    out = np.empty((h, w, 3), dtype=np.uint8)

    for y0 in range(0, h, BAND):
        y1 = min(y0 + BAND, h)

        # 0) 输入已完成源还原和色彩再创作；从这里开始胶卷模拟。
        blk = _prep(arr[y0:y1].astype(np.float32) / 255.0, lut, ev, wb)
        lum = luma_of(blk)

        # 1) 通道色偏（按亮度在阴影偏色与高光偏色之间插值）
        if sh is not None:
            blk += (sh[None, None, :] * (1.0 - lum)[..., None]
                    + hi[None, None, :] * lum[..., None])
            lum = luma_of(blk)

        # 2) 色相带选择性调整（青绿倾向 / 黄绿抑制 / 肤色微调）
        if hue_bands and not preset.get("bw", False):
            blk = np.clip(hue_shift(np.clip(blk, 0.0, 1.0), hue_bands), 0.0, 1.0)
            lum = luma_of(blk)

        # 3) 黑白通道混合
        if preset.get("bw", False):
            y = blk @ bw_mix
            blk = y[..., None].repeat(3, axis=2)
            blk += tone[None, None, :]
            lum = y

        # 4) 外部 3D LUT
        if lut3d is not None:
            blk = lut3d.apply(np.clip(blk, 0.0, 1.0), lut_strength)
            lum = luma_of(blk)

        # 4.5) 锐度/边缘反差（Fuji 底片的高 acutance 观感）
        if ac_amt > 0:
            pad = int(math.ceil(3.0 * ac_r)) + 2
            a0, a1 = max(0, y0 - pad), min(h, y1 + pad)
            pl = luma_of(_prep(arr[a0:a1].astype(np.float32) / 255.0, lut, ev, wb))
            lp = gauss_blur_fft(pl, ac_r)[y0 - a0: y0 - a0 + (y1 - y0)]
            blk += ((lum - lp) * ac_amt)[..., None]

        # 5) 饱和（暗部 / 中间调 / 高光分档）
        if not preset.get("bw", False) and (sat != 1.0 or sat_hi != 1.0 or sat_sh != 1.0):
            s = np.where(lum < 0.5,
                         sat_sh + (sat - sat_sh) * (lum * 2.0),
                         sat + (sat_hi - sat) * ((lum - 0.5) * 2.0))
            blk = lum[..., None] + (blk - lum[..., None]) * s[..., None]

        # 6) 高光溢出（screen 混合，天然不溢出）
        if hal_amt > 0 and halo.size:
            hh = halo[y0:y1].astype(np.float32) / 255.0
            tint = (hal_tint * hal_amt)[None, None, :]
            blk = 1.0 - (1.0 - blk) * (1.0 - np.clip(hh[..., None] * tint, 0.0, 1.0))

        # 7) 颗粒
        if g_amt > 0:
            mono, chroma = grain_band(rng, y0, y1, h, w, g_sigma, g_chroma)
            # 缩图预览模拟全尺寸调色后再缩小的颗粒衰减，避免预览出现假性粗噪。
            preview_factor = max(0.30, math.sqrt(min(1.0, res_scale)))
            amp = (g_amt * 5.0 / 255.0) * preview_factor * (0.40 + 0.85 * (4.0 * lum * (1.0 - lum)))
            blk += (mono * amp)[..., None]
            if chroma is not None:
                blk += chroma * (amp * g_chroma)[..., None]

        # 8) 暗角
        if v_amt > 0:
            r2 = rx2 + ry2[y0:y1][:, None]
            blk *= (1.0 - v_amt * np.clip(r2, 0.0, 1.4) ** 1.15)[..., None]

        out[y0:y1] = np.clip(blk * 255.0 + 0.5, 0, 255).astype(np.uint8)

    return out


# ══════════════════════════════════════════════════════════ 预设
# 字段说明
#   temp / tint     白平衡倾向（temp>0 偏暖，tint>0 偏绿）——复现各家底片的白点差异
#   curve.lift      每通道黑位抬升（胶片基灰 / 哑光黑），值越大越"灰"
#   curve.gamma     每通道中间调位置，>1 提亮该通道
#   curve.contrast  全局 S 曲线强度 0~0.4
#   curve.shadow_contrast  仅作用于暗部的硬度（>0 压暗 = 反转片/富士的"暗部硬调"；<0 提暗）
#   curve.shoulder / shoulder_k  高光滚降起点 / 滚降陡度（k 越小越早越柔）
#   curve.toe       暗部软着陆（负片不把黑压死）
#   hue_bands       色相带选择性调整 {"green": {"hue": 度, "sat": ±比例, "width": 度}}
#                   hue 为正 = 在 (Cb,Cr) 平面旋转，绿(225°)→青(293°) 即正向
#   crossover.shadow / highlight   阴影 / 高光的每通道加减（单位 1.0 全幅）——胶片色彩交叉
#   sat / sat_hi / sat_sh          中间调 / 高光 / 暗部饱和度
#   grain.amount    颗粒强度（1.0 ≈ 中间调 σ4.9 级）；size 颗粒粗度(px)；chroma 色度颗粒占比
#   halation.amount / threshold / radius(占长边比例) / tint
#   acutance.amount / radius       边缘锐度（富士底片的 acutance 观感）
#   vignette / exposure
#
# 富士 vs 柯达的复现要点（见同目录 Fuji 胶片色彩复现说明）
#   富士：白点偏冷(5000–5200K)、绿色向青绿、黄被压、暗部青绿且更硬、高光偏品红/干净、acutance 高
#   柯达：白点偏暖、绿色向黄橄榄、黄红更饱和、暗部偏琥珀、高光偏暖金
PRESETS = {
    # ─────────────── 柯达系 ───────────────
    "portra400": {
        "label": "Kodak Portra 400",
        "note": "中性偏暖、低对比、高光柔和、肤色友好；建筑/风景也稳",
        "temp": 0.10,
        "curve": {"contrast": 0.13, "shoulder": 0.83, "shoulder_k": 3.0, "toe": 0.26,
                  "lift": [0.014, 0.012, 0.016], "gamma": [1.00, 1.00, 0.99]},
        "crossover": {"shadow": [0.004, 0.002, 0.009], "highlight": [0.011, 0.003, -0.009]},
        "hue_bands": {"green": {"hue": -2, "sat": -0.08}, "orange": {"hue": 3, "sat": -0.06}},
        "sat": 0.93, "sat_hi": 0.80, "sat_sh": 0.97,
        "grain": {"amount": 0.85, "size": 1.0, "chroma": 0.20},
        "halation": {"amount": 0.28, "threshold": 0.86, "radius": 0.0055, "tint": [1.0, 0.62, 0.44]},
        "acutance": {"amount": 0.08, "radius": 1.0},
        "vignette": 0.10,
    },
    "gold200": {
        "label": "Kodak Gold 200",
        "note": "金黄暖调、绿偏黄橄榄、黄红更饱和、颗粒明显；怀旧日常感",
        "temp": 0.32,
        "curve": {"contrast": 0.19, "shoulder": 0.80, "shoulder_k": 3.0, "toe": 0.20,
                  "lift": [0.017, 0.013, 0.010], "gamma": [1.02, 1.00, 0.98]},
        "crossover": {"shadow": [0.011, 0.005, -0.006], "highlight": [0.024, 0.008, -0.014]},
        "hue_bands": {"green": {"hue": -5, "sat": -0.05}, "yellow": {"sat": 0.12},
                      "orange": {"sat": 0.08}, "red": {"sat": 0.06}},
        "sat": 1.04, "sat_hi": 0.92, "sat_sh": 0.98,
        "grain": {"amount": 1.15, "size": 1.1, "chroma": 0.28},
        "halation": {"amount": 0.30, "threshold": 0.85, "radius": 0.006, "tint": [1.0, 0.58, 0.36]},
        "acutance": {"amount": 0.06, "radius": 1.0},
        "vignette": 0.13,
    },
    "ektar100": {
        "label": "Kodak Ektar 100",
        "note": "现代反转片素质：高饱和、高对比、极细颗粒、干净高光",
        "temp": 0.06,
        "curve": {"contrast": 0.27, "shoulder": 0.86, "shoulder_k": 3.6, "toe": 0.10,
                  "lift": [0.006, 0.006, 0.008], "gamma": [1.00, 1.00, 1.00]},
        "crossover": {"shadow": [0.002, 0.002, 0.006], "highlight": [0.006, 0.002, -0.005]},
        "hue_bands": {"green": {"hue": -3, "sat": 0.04}, "blue": {"sat": 0.08}, "red": {"sat": 0.06}},
        "sat": 1.12, "sat_hi": 1.02, "sat_sh": 1.05,
        "grain": {"amount": 0.45, "size": 0.8, "chroma": 0.12},
        "halation": {"amount": 0.18, "threshold": 0.90, "radius": 0.0045, "tint": [1.0, 0.66, 0.50]},
        "acutance": {"amount": 0.14, "radius": 0.9},
        "vignette": 0.08,
    },
    "kodachrome": {
        "label": "Kodachrome 64",
        "note": "浓郁红黄、暗部深、对比强、颗粒细而硬；70 年代国家地理味",
        "temp": 0.16,
        "curve": {"contrast": 0.30, "shoulder": 0.84, "shoulder_k": 4.0, "toe": 0.08,
                  "lift": [0.005, 0.004, 0.007], "gamma": [1.01, 0.99, 0.98]},
        "crossover": {"shadow": [0.010, -0.002, -0.007], "highlight": [0.024, 0.004, -0.016]},
        "hue_bands": {"green": {"hue": -4, "sat": -0.05}, "red": {"sat": 0.08},
                      "yellow": {"sat": 0.06}},
        "sat": 1.06, "sat_hi": 0.95, "sat_sh": 0.98,
        "grain": {"amount": 0.80, "size": 0.9, "chroma": 0.16},
        "halation": {"amount": 0.20, "threshold": 0.88, "radius": 0.005, "tint": [1.0, 0.55, 0.34]},
        "acutance": {"amount": 0.12, "radius": 0.9},
        "vignette": 0.12,
    },
    "cinestill800t": {
        "label": "Cinestill 800T",
        "note": "钨丝灯片日光下强青蓝、暗部湛蓝、高光强红色光晕；夜景神器",
        "temp": -0.20,
        "curve": {"contrast": 0.18, "shoulder": 0.76, "shoulder_k": 3.0, "toe": 0.22,
                  "lift": [0.012, 0.016, 0.028], "gamma": [0.98, 1.00, 1.03]},
        "crossover": {"shadow": [0.000, 0.014, 0.040], "highlight": [-0.014, 0.004, 0.022]},
        "hue_bands": {"green": {"hue": 2, "sat": -0.10}, "blue": {"sat": 0.06}},
        "sat": 1.00, "sat_hi": 0.86, "sat_sh": 1.06,
        "grain": {"amount": 1.55, "size": 1.25, "chroma": 0.38},
        "halation": {"amount": 0.60, "threshold": 0.78, "radius": 0.010, "tint": [1.0, 0.30, 0.16]},
        "acutance": {"amount": 0.08, "radius": 1.0},
        "vignette": 0.16,
    },
    # ─────────────── 富士负片系 ───────────────
    "superia400": {
        "label": "Fuji Superia X-TRA 400",
        "note": "富士负片标杆：阴影深但保留细节、偏青绿，高光偏品红，绿→青绿、黄被压、白点偏冷",
        "temp": -0.30, "tint": 0.04,
        "curve": {"contrast": 0.23, "shoulder": 0.76, "shoulder_k": 3.8,
                  "shadow_contrast": 0.20, "toe": 0.12,
                  "lift": [0.015, 0.017, 0.020], "gamma": [1.00, 1.01, 1.00]},
        "crossover": {"shadow": [-0.006, 0.011, 0.016], "highlight": [0.014, -0.005, 0.011]},
        "hue_bands": {"green": {"hue": 8, "sat": 0.16, "width": 50},
                      "cyan": {"sat": 0.10}, "yellow": {"hue": 3, "sat": -0.18},
                      "red": {"sat": 0.04}},
        "sat": 0.96, "sat_hi": 0.86, "sat_sh": 0.99,
        "grain": {"amount": 1.25, "size": 1.15, "chroma": 0.30},
        "halation": {"amount": 0.28, "threshold": 0.86, "radius": 0.0055, "tint": [1.0, 0.58, 0.46]},
        "acutance": {"amount": 0.16, "radius": 1.0},
        "vignette": 0.14,
    },
    "classic_neg": {
        "label": "Fuji 經典 Neg.（基于 SUPERIA）",
        "note": "饱和被抑制、明暗区色调分离、暗部硬调拉出层次、高光转暖；纪实速写感",
        "temp": -0.24, "tint": 0.02,
        "curve": {"contrast": 0.28, "shoulder": 0.70, "shoulder_k": 2.8,
                  "shadow_contrast": 0.34, "toe": 0.10,
                  "lift": [0.011, 0.013, 0.016], "gamma": [1.00, 1.01, 1.00]},
        "crossover": {"shadow": [-0.002, 0.010, 0.016], "highlight": [0.019, 0.005, -0.009]},
        "hue_bands": {"green": {"hue": 9, "sat": 0.10, "width": 50}, "yellow": {"sat": -0.12},
                      "cyan": {"sat": 0.06}},
        "sat": 0.90, "sat_hi": 0.82, "sat_sh": 0.95,
        "grain": {"amount": 1.05, "size": 1.1, "chroma": 0.26},
        "halation": {"amount": 0.26, "threshold": 0.86, "radius": 0.0055, "tint": [1.0, 0.60, 0.46]},
        "acutance": {"amount": 0.14, "radius": 1.0},
        "vignette": 0.16,
    },
    "c200": {
        "label": "Fuji C200",
        "note": "廉价富士：冷而准，不变黄；黄被压、绿偏青，高光微品红",
        "temp": -0.26,
        "curve": {"contrast": 0.16, "shoulder": 0.80, "shoulder_k": 3.0,
                  "shadow_contrast": 0.10, "toe": 0.14,
                  "lift": [0.012, 0.013, 0.015], "gamma": [1.00, 1.00, 1.00]},
        "crossover": {"shadow": [-0.004, 0.008, 0.012], "highlight": [0.009, -0.001, 0.005]},
        "hue_bands": {"green": {"hue": 6, "sat": 0.08, "width": 50}, "yellow": {"sat": -0.15}},
        "sat": 0.97, "sat_hi": 0.88, "sat_sh": 0.98,
        "grain": {"amount": 1.00, "size": 1.05, "chroma": 0.26},
        "halation": {"amount": 0.22, "threshold": 0.86, "radius": 0.0055, "tint": [1.0, 0.60, 0.46]},
        "acutance": {"amount": 0.14, "radius": 1.0},
        "vignette": 0.12,
    },
    "pro400h": {
        "label": "Fuji Pro 400H",
        "note": "第四层青/品红乳剂：高光极软不爆、暗部永不真黑、粉彩通透、绿更密更冷",
        "temp": -0.20, "exposure": 0.10,
        "curve": {"contrast": 0.05, "shoulder": 0.55, "shoulder_k": 1.2,
                  "shadow_contrast": -0.08, "toe": 0.40,
                  "lift": [0.022, 0.024, 0.026], "gamma": [1.01, 1.01, 1.00]},
        "crossover": {"shadow": [-0.002, 0.007, 0.011], "highlight": [0.006, 0.005, 0.002]},
        "hue_bands": {"green": {"hue": 7, "sat": 0.04, "width": 50}, "magenta": {"sat": -0.08},
                      "orange": {"hue": 4, "sat": -0.10}, "yellow": {"sat": -0.05}},
        "sat": 0.86, "sat_hi": 0.78, "sat_sh": 0.92,
        "grain": {"amount": 0.65, "size": 0.95, "chroma": 0.16},
        "halation": {"amount": 0.22, "threshold": 0.86, "radius": 0.006, "tint": [1.0, 0.68, 0.52]},
        "acutance": {"amount": 0.10, "radius": 1.0},
        "vignette": 0.05,
    },
    # ─────────────── 富士反转片系 ───────────────
    "velvia50": {
        "label": "Fujichrome Velvia 50",
        "note": "极饱和反差、绿如祖母绿、暗部深、开敞阴影偏品红、颗粒极细、高光易爆",
        "temp": -0.10,
        "curve": {"contrast": 0.34, "shoulder": 0.90, "shoulder_k": 5.0,
                  "shadow_contrast": 0.28, "toe": 0.02,
                  "lift": [0.003, 0.003, 0.005], "gamma": [1.00, 1.00, 1.00]},
        "crossover": {"shadow": [0.012, -0.006, 0.010], "highlight": [0.008, 0.000, 0.004]},
        "hue_bands": {"green": {"hue": 5, "sat": 0.34, "width": 55}, "cyan": {"sat": 0.20},
                      "blue": {"sat": 0.22}, "red": {"sat": 0.15}},
        "sat": 1.20, "sat_hi": 1.08, "sat_sh": 1.05,
        "grain": {"amount": 0.35, "size": 0.75, "chroma": 0.10},
        "halation": {"amount": 0.10, "threshold": 0.92, "radius": 0.004, "tint": [1.0, 0.62, 0.44]},
        "acutance": {"amount": 0.18, "radius": 0.9},
        "vignette": 0.10,
    },
    "provia100f": {
        "label": "Fujichrome Provia 100F",
        "note": "富士标准反转片：中性准确、略偏冷、绿色忠实不偏祖母绿、暗部保留好",
        "temp": -0.14,
        "curve": {"contrast": 0.22, "shoulder": 0.86, "shoulder_k": 3.4,
                  "shadow_contrast": 0.12, "toe": 0.10,
                  "lift": [0.006, 0.006, 0.008], "gamma": [1.00, 1.00, 1.00]},
        "crossover": {"shadow": [0.000, 0.003, 0.007], "highlight": [0.003, 0.001, 0.001]},
        "hue_bands": {"green": {"hue": 3, "sat": 0.05}, "blue": {"sat": 0.10},
                      "cyan": {"sat": 0.06}},
        "sat": 1.04, "sat_hi": 0.98, "sat_sh": 1.00,
        "grain": {"amount": 0.40, "size": 0.85, "chroma": 0.10},
        "halation": {"amount": 0.12, "threshold": 0.90, "radius": 0.0045, "tint": [1.0, 0.64, 0.48]},
        "acutance": {"amount": 0.16, "radius": 0.9},
        "vignette": 0.07,
    },
    # ─────────────── 富士胶片模拟（数码原生） ───────────────
    "classic_chrome": {
        "label": "Fuji CLASSIC CHROME",
        "note": "低饱和、暗部硬调且偏青蓝、高光冷静；纪实写实感",
        "temp": -0.22,
        "curve": {"contrast": 0.16, "shoulder": 0.68, "shoulder_k": 2.4,
                  "shadow_contrast": 0.34, "toe": 0.02,
                  "lift": [0.010, 0.010, 0.011], "gamma": [1.00, 1.00, 1.00]},
        "crossover": {"shadow": [-0.006, 0.004, 0.012], "highlight": [-0.004, 0.002, 0.004]},
        "hue_bands": {"green": {"hue": 6, "sat": -0.06}, "yellow": {"sat": -0.18},
                      "red": {"sat": -0.06}, "blue": {"sat": 0.05}},
        "sat": 0.82, "sat_hi": 0.76, "sat_sh": 0.88,
        "grain": {"amount": 0.60, "size": 1.0, "chroma": 0.14},
        "halation": {"amount": 0.16, "threshold": 0.86, "radius": 0.005, "tint": [1.0, 0.60, 0.46]},
        "acutance": {"amount": 0.12, "radius": 1.0},
        "vignette": 0.12,
    },
    "eterna": {
        "label": "Fuji ETERNA",
        "note": "电影胶片：饱和度压到最低、过渡极柔、高光几乎不爆、暗部丰富",
        "temp": -0.10,
        "curve": {"contrast": 0.06, "shoulder": 0.50, "shoulder_k": 1.0,
                  "shadow_contrast": -0.10, "toe": 0.36,
                  "lift": [0.022, 0.023, 0.024], "gamma": [1.00, 1.00, 1.00]},
        "crossover": {"shadow": [0.002, 0.006, 0.010], "highlight": [-0.002, 0.004, 0.006]},
        "hue_bands": {"green": {"hue": 4, "sat": -0.05}, "yellow": {"sat": -0.08}},
        "sat": 0.78, "sat_hi": 0.70, "sat_sh": 0.86,
        "grain": {"amount": 0.55, "size": 1.0, "chroma": 0.14},
        "halation": {"amount": 0.14, "threshold": 0.88, "radius": 0.0055, "tint": [1.0, 0.66, 0.50]},
        "acutance": {"amount": 0.06, "radius": 1.1},
        "vignette": 0.05,
    },
    # ─────────────── 参考画面复刻：江南园林电影色 ───────────────
    "jiangnan_cool": {
        "label": "江南园林·冷青绿",
        "note": "参考古风园林剧照：深青黑阴影、灰紫木石、低饱和青绿、淡黄绿高光；宜园林/旧建筑/植物",
        "temp": -0.22, "tint": 0.035, "exposure": -0.08,
        "curve": {"contrast": 0.24, "shoulder": 0.73, "shoulder_k": 2.5,
                  "shadow_contrast": 0.24, "toe": 0.07,
                  "lift": [0.007, 0.010, 0.013], "gamma": [0.98, 1.00, 1.02]},
        "crossover": {"shadow": [-0.012, 0.004, 0.013],
                      "highlight": [0.005, 0.010, -0.010]},
        "hue_bands": {"green": {"hue": 9, "sat": -0.08, "width": 55},
                      "yellow": {"hue": 4, "sat": -0.15},
                      "orange": {"sat": -0.10}, "red": {"sat": -0.10}},
        "sat": 0.80, "sat_hi": 0.76, "sat_sh": 0.86,
        "grain": {"amount": 0.45, "size": 0.80, "chroma": 0.08},
        "halation": {"amount": 0.12, "threshold": 0.85, "radius": 0.005,
                     "tint": [1.0, 0.78, 0.55]},
        "acutance": {"amount": 0.04, "radius": 1.0},
        "vignette": 0.10,
    },
    "jiangnan_amber": {
        "label": "江南园林·烛光琥珀",
        "note": "同组参考画面的室内暖光分支：深褐黑环境、奶油高光、克制的烛火琥珀；宜室内窗光/烛光",
        "temp": 0.18, "tint": -0.02, "exposure": -0.08,
        "curve": {"contrast": 0.25, "shoulder": 0.74, "shoulder_k": 2.5,
                  "shadow_contrast": 0.24, "toe": 0.06,
                  "lift": [0.009, 0.007, 0.008], "gamma": [1.02, 1.00, 0.96]},
        "crossover": {"shadow": [0.002, -0.004, -0.010],
                      "highlight": [0.018, 0.008, -0.014]},
        "hue_bands": {"green": {"hue": -2, "sat": -0.18},
                      "yellow": {"sat": -0.06}, "orange": {"sat": 0.03},
                      "red": {"sat": -0.12}},
        "sat": 0.83, "sat_hi": 0.78, "sat_sh": 0.88,
        "grain": {"amount": 0.40, "size": 0.80, "chroma": 0.08},
        "halation": {"amount": 0.24, "threshold": 0.80, "radius": 0.006,
                     "tint": [1.0, 0.67, 0.33]},
        "acutance": {"amount": 0.03, "radius": 1.0},
        "vignette": 0.11,
    },
    # ─────────────── 黑白 ───────────────
    "acros": {
        "label": "Fuji ACROS 100",
        "note": "富士黑白：颗粒极细、暗部细节丰富、锐度高、微冷调",
        "bw": True,
        "mix": [0.28, 0.64, 0.08],
        "tone": [0.000, 0.002, 0.004],
        "curve": {"contrast": 0.24, "shoulder": 0.86, "shoulder_k": 3.0,
                  "shadow_contrast": 0.10, "toe": 0.26,
                  "lift": [0.010, 0.010, 0.010], "gamma": [1.0, 1.0, 1.0]},
        "sat": 1.0, "sat_hi": 1.0, "sat_sh": 1.0,
        "grain": {"amount": 0.90, "size": 0.80, "chroma": 0.0},
        "halation": {"amount": 0.0},
        "acutance": {"amount": 0.20, "radius": 0.9},
        "vignette": 0.10,
    },
    "hp5": {
        "label": "Ilford HP5 Plus 400",
        "note": "黑白、全色响应、宽容度高、颗粒明显、中性冷调",
        "bw": True,
        "mix": [0.30, 0.59, 0.11],
        "tone": [0.000, 0.000, 0.002],
        "curve": {"contrast": 0.26, "shoulder": 0.84, "shoulder_k": 3.2, "toe": 0.22,
                  "lift": [0.012, 0.012, 0.012], "gamma": [1.0, 1.0, 1.0]},
        "sat": 1.0, "sat_hi": 1.0, "sat_sh": 1.0,
        "grain": {"amount": 2.30, "size": 1.2, "chroma": 0.0},
        "halation": {"amount": 0.0},
        "acutance": {"amount": 0.10, "radius": 1.0},
        "vignette": 0.12,
    },
    "tri_x": {
        "label": "Kodak Tri-X 400",
        "note": "黑白、对比更硬、颗粒更粗、暗部更实、微暖调",
        "bw": True,
        "mix": [0.36, 0.55, 0.09],
        "tone": [0.008, 0.003, -0.003],
        "curve": {"contrast": 0.36, "shoulder": 0.80, "shoulder_k": 4.0, "toe": 0.10,
                  "lift": [0.008, 0.008, 0.008], "gamma": [0.99, 0.99, 0.99]},
        "sat": 1.0, "sat_hi": 1.0, "sat_sh": 1.0,
        "grain": {"amount": 2.80, "size": 1.3, "chroma": 0.0},
        "halation": {"amount": 0.0},
        "acutance": {"amount": 0.14, "radius": 0.9},
        "vignette": 0.15,
    },
    "neutral": {
        "label": "Neutral（对照组）",
        "note": "几乎不改动，仅极轻颗粒与抬黑；用来和原图对比判断改动量",
        "curve": {"contrast": 0.0, "shoulder": 1.0, "toe": 0.0,
                  "lift": [0.004, 0.004, 0.004], "gamma": [1.0, 1.0, 1.0]},
        "crossover": {"shadow": [0, 0, 0], "highlight": [0, 0, 0]},
        "sat": 1.0, "sat_hi": 1.0, "sat_sh": 1.0,
        "grain": {"amount": 0.35, "size": 0.9, "chroma": 0.05},
        "halation": {"amount": 0.0},
        "vignette": 0.0,
    },
}

FUJI_PRESETS = ["superia400", "classic_neg", "c200", "pro400h",
                "velvia50", "provia100f", "classic_chrome", "eterna", "acros"]
KODAK_PRESETS = ["portra400", "gold200", "ektar100", "kodachrome", "cinestill800t", "tri_x"]
REFERENCE_PRESETS = ["jiangnan_cool", "jiangnan_amber"]

TUNE_KEYS = ("grain", "halation", "vignette", "sat", "contrast", "exposure")

# 显著版的创作目标；数字是本工具的视觉设计值，不声称为原厂配方。
# (中间调饱和度, 曲线对比, 颗粒量)。低饱和风格通过更明显的分色和影调体现。
ENHANCED_TARGETS = {
    "portra400": (0.97, 0.23, 1.25), "gold200": (1.14, 0.31, 1.55),
    "ektar100": (1.30, 0.43, 0.65), "kodachrome": (1.16, 0.48, 1.10),
    "cinestill800t": (1.04, 0.32, 1.95),
    "superia400": (0.93, 0.37, 1.55), "classic_neg": (0.83, 0.46, 1.45),
    "c200": (0.95, 0.28, 1.35), "pro400h": (0.78, 0.09, 0.90),
    "velvia50": (1.30, 0.54, 0.45), "provia100f": (1.10, 0.34, 0.55),
    "classic_chrome": (0.68, 0.28, 0.90), "eterna": (0.66, 0.09, 0.80),
    "jiangnan_cool": (0.65, 0.40, 0.75), "jiangnan_amber": (0.73, 0.40, 0.70),
    "acros": (1.00, 0.36, 1.25), "hp5": (1.00, 0.39, 3.20),
    "tri_x": (1.00, 0.53, 3.90),
}

AUTO_CREATIVE_DIRECTIONS = {
    "portra400": "soft", "gold200": "warm", "ektar100": "vivid",
    "kodachrome": "warm", "cinestill800t": "cool",
    "superia400": "cool", "classic_neg": "cool", "c200": "cool",
    "pro400h": "soft", "velvia50": "vivid", "provia100f": "vivid",
    "classic_chrome": "cool", "eterna": "soft",
    "jiangnan_cool": "cool", "jiangnan_amber": "warm",
    "acros": "mono", "hp5": "mono", "tri_x": "mono",
}


def styled_preset(name: str, strength: float = 1.0, direction: str = "auto") -> dict:
    """0=旧版，1=显著版；neutral 始终作为不强化的对照组。"""
    if not 0.0 <= strength <= 1.0:
        raise ValueError("style strength must be between 0 and 1")
    p = apply_overrides(PRESETS[name])
    if name == "neutral" or strength == 0:
        return p
    p["creative_direction"] = (AUTO_CREATIVE_DIRECTIONS[name] if direction == "auto"
                               else direction)
    p["creative_strength"] = strength
    sat, contrast, grain = ENHANCED_TARGETS[name]
    mix = lambda a, b: a + (b - a) * strength
    old_sat = p.get("sat", 1.0)
    p["sat"] = mix(old_sat, sat)
    p["sat_hi"] = mix(p.get("sat_hi", old_sat), sat + 1.35 * (p.get("sat_hi", old_sat) - old_sat))
    p["sat_sh"] = mix(p.get("sat_sh", old_sat), sat + 1.35 * (p.get("sat_sh", old_sat) - old_sat))
    p["curve"]["contrast"] = mix(p["curve"]["contrast"], contrast)
    if "shadow_contrast" in p["curve"]:
        p["curve"]["shadow_contrast"] *= 1.0 + 0.55 * strength
    p["curve"]["gamma"] = [1.0 + (v - 1.0) * (1.0 + 0.6 * strength)
                              for v in p["curve"]["gamma"]]
    p["temp"] = p.get("temp", 0.0) * (1.0 + 1.25 * strength)
    p["tint"] = p.get("tint", 0.0) * (1.0 + 0.8 * strength)
    if "crossover" in p:
        p["crossover"] = {key: [v * (1.0 + 1.35 * strength) for v in values]
                          for key, values in p["crossover"].items()}
    if "hue_bands" in p:
        p["hue_bands"] = {
            band: {**cfg,
                   "hue": cfg.get("hue", 0.0) * (1.0 + 1.4 * strength),
                   "sat": cfg.get("sat", 0.0) * (1.0 + 0.40 * strength)}
            for band, cfg in p["hue_bands"].items()}
    if name == "velvia50":
        # 已饱和的绿植不再按统一增益推到荧光色；靠深影与整体色彩维持 Velvia 观感。
        p["hue_bands"]["green"]["sat"] = mix(p["hue_bands"]["green"]["sat"], 0.20)
    p["grain"]["amount"] = mix(p["grain"].get("amount", 0.0), grain)
    p["grain"]["size"] = p["grain"].get("size", 1.0) * (1.0 + 0.10 * strength)
    if p["halation"].get("amount", 0.0):
        halo_mult = 2.0 if name == "cinestill800t" else 1.35
        p["halation"]["amount"] *= 1.0 + (halo_mult - 1.0) * strength
        p["halation"]["threshold"] -= 0.025 * strength
        if name == "cinestill800t":
            # 强点光保留红晕；大面积 LED 灯带避免铺成白斑。
            p["halation"]["amount"] = mix(p["halation"]["amount"], 0.88)
            p["halation"]["threshold"] = mix(p["halation"]["threshold"], 0.82)
            p["halation"]["radius"] = mix(p["halation"]["radius"], 0.008)
            p["halation"]["core_protect"] = 0.9 * strength
    p["vignette"] = min(0.28, p.get("vignette", 0.0) * (1.0 + 0.15 * strength))
    return p


def apply_overrides(preset: dict, grain=None, halation=None, vignette=None,
                    sat=None, contrast=None, exposure=None,
                    temp=None, tint=None, acutance=None) -> dict:
    p = {k: (dict(v) if isinstance(v, dict) else v) for k, v in preset.items()}
    p["curve"] = dict(p["curve"])
    p["grain"] = dict(p.get("grain", {}))
    p["halation"] = dict(p.get("halation", {}))
    if grain is not None:
        p["grain"]["amount"] = grain
    if halation is not None:
        p["halation"]["amount"] = halation
    if vignette is not None:
        p["vignette"] = vignette
    if sat is not None:
        p["sat"] = p.get("sat", 1.0) * sat
    if contrast is not None:
        p["curve"]["contrast"] = min(0.6, p["curve"]["contrast"] * contrast)
    if exposure is not None:
        p["exposure"] = p.get("exposure", 0.0) + exposure
    if temp is not None:
        p["temp"] = p.get("temp", 0.0) + temp
    if tint is not None:
        p["tint"] = p.get("tint", 0.0) + tint
    if acutance is not None:
        p["acutance"] = dict(p.get("acutance", {}))
        p["acutance"]["amount"] = acutance
    return p


# ══════════════════════════════════════════════════════════ 单文件处理
def _load_rgb(path: str, max_edge: int | None = None, prep: dict | None = None):
    """读图。prep 存在且模式非 standard/off 时，先做源归一化（RAW 显影 / 反 log）。

    返回 (PIL.Image, SourceInfo | None)
    """
    info = None
    if prep and SN is not None and prep.get("mode", "auto") not in ("standard", "off"):
        info = (prep.get("probes") or {}).get(os.path.abspath(path))
        try:
            img, info = SN.normalize(
                path, info=info, source=prep.get("mode", "auto"),
                log_profile=prep.get("log_profile", "auto"),
                raw_anchor=prep.get("raw_anchor", "reference-tone"),
                raw_denoise=prep.get("raw_denoise", "off"),
                exposure=prep.get("exposure", 0.0),
                knee=prep.get("knee", SN.DEFAULT_KNEE),
                knee_k=prep.get("knee_k", SN.DEFAULT_KNEE_K),
                max_edge=max_edge)
            if img is not None:
                return img, info
        except Exception:
            pass                                     # 归一化失败则退回直接读取
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")
    if max_edge and max(img.size) > max_edge:
        r = max_edge / max(img.size)
        img = img.resize((max(1, int(img.width * r)), max(1, int(img.height * r))), Image.LANCZOS)
    return img, info


def process_one(job) -> tuple:
    (path, out_path, preset_name, opts, quality, max_edge) = job[:6]
    t0 = time.time()
    try:
        img, info = _load_rgb(path, max_edge, opts.get("prep"))
        arr = np.asarray(img, dtype=np.uint8)
        p = apply_overrides(styled_preset(preset_name, opts.get("style_strength", 1.0),
                                          opts.get("creative_direction", "auto")),
                            **opts["override"])
        lut3d = CubeLUT(opts["lut"]) if opts.get("lut") else None
        scale = opts.get("res_scale", 1.0)
        if max_edge and os.path.splitext(path)[1].lower() not in (SN.RAW_EXTS if SN else set()):
            with Image.open(path) as original:
                scale = min(1.0, max(img.size) / max(original.size))
        creative = apply_creative_stage(arr, p)
        if opts.get("save_creative") and creative is not arr:
            stage_dir = os.path.join(os.path.dirname(out_path), "creative-stage")
            os.makedirs(stage_dir, exist_ok=True)
            stage_name = os.path.splitext(os.path.basename(out_path))[0] + "_creative.jpg"
            Image.fromarray(creative, "RGB").save(os.path.join(stage_dir, stage_name),
                                                   quality=quality, subsampling=0)
        del arr
        res = render(creative, p, seed=opts["seed"], res_scale=scale,
                     lut3d=lut3d, lut_strength=opts.get("lut_strength", 1.0))
        exif = None
        if "exif" in img.info:
            try:
                exif = img.info["exif"]
            except Exception:
                exif = None
        else:
            try:
                ex = img.getexif()
                ex[274] = 1
                exif = ex.tobytes()
            except Exception:
                exif = None
        out = Image.fromarray(res, "RGB")
        kw = {"quality": quality, "subsampling": 0, "optimize": True}
        if os.path.splitext(out_path)[1].lower() in (".jpg", ".jpeg"):
            out.save(out_path, exif=exif, **kw)
        else:
            out.save(out_path)
        note = ""
        if info is not None:
            note = f"{info.kind}" + (f"/{info.profile}" if info.profile else "") \
                   + (f" {info.confidence}" if info.confidence not in ("-", "") else "")
        return (path, out_path, True, f"{time.time() - t0:.1f}s", "", note)
    except Exception as e:
        import traceback
        return (path, out_path, False, f"{time.time() - t0:.1f}s",
                traceback.format_exc(limit=3) + str(e), "")


# ══════════════════════════════════════════════════════════ 对比图
def build_sheet(src: str, out_path: str, names: list[str], max_edge: int = 1400,
                cols: int = 3, seed: int = 20260926, opts=None) -> str:
    opts = opts or {}
    base, _info = _load_rgb(src, max_edge, opts.get("prep"))
    w, h = base.size
    full = Image.open(src) if os.path.splitext(src)[1].lower() not in (SN.RAW_EXTS if SN else set()) \
        else base
    scale = max(w, h) / max(full.size)
    arr = np.asarray(base, dtype=np.uint8)

    tiles = [("原图 (Original)", np.asarray(base, dtype=np.uint8))]
    for n in names:
        p = apply_overrides(styled_preset(n, opts.get("style_strength", 1.0),
                                          opts.get("creative_direction", "auto")),
                            **(opts.get("override") or {}))
        creative = apply_creative_stage(arr, p)
        r = render(creative, p, seed=seed, res_scale=scale,
                   lut3d=CubeLUT(opts["lut"]) if opts.get("lut") else None,
                   lut_strength=opts.get("lut_strength", 1.0))
        tiles.append((PRESETS[n]["label"], r))

    pad, cap = 10, 34
    rows = math.ceil(len(tiles) / cols)
    W = cols * w + (cols + 1) * pad
    H = rows * (h + cap) + (rows + 1) * pad
    sheet = Image.new("RGB", (W, H), (24, 24, 26))
    font = None
    for candidate in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf",
                      "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"):
        if os.path.isfile(candidate):
            font = ImageFont.truetype(candidate, 20)
            break
    if font is None:
        font = ImageFont.load_default(size=20)
    draw = ImageDraw.Draw(sheet)
    for i, (label, t) in enumerate(tiles):
        r, c = divmod(i, cols)
        x = pad + c * (w + pad)
        y = pad + r * (h + cap + pad)
        sheet.paste(Image.fromarray(t, "RGB"), (x, y))
        draw.text((x + 2, y + h + 6), label, fill=(235, 235, 240), font=font)
    sheet.save(out_path, quality=92, subsampling=0, optimize=True)
    return out_path


# ══════════════════════════════════════════════════════════ CLI
IMG_RE = r"\.(?:jpe?g|png|tiff?|bmp|webp)$"
RAW_RE = (r"\.(?:" + "|".join(sorted(e.lstrip(".") for e in SN.RAW_EXTS)) + r")$") if SN else None


def collect(inputs, pattern=None, recursive=False, limit=None, allow_raw=True):
    files = []
    for it in inputs:
        if os.path.isdir(it):
            it = it.rstrip("/\\") + ("/**" if recursive else "/*")
            files += [f for f in glob.glob(it, recursive=recursive) if os.path.isfile(f)]
        else:
            files += glob.glob(it)
    if pattern:
        rx = re.compile(pattern, re.I)
    else:
        rx = re.compile(IMG_RE + ("|" + RAW_RE if (allow_raw and RAW_RE) else ""), re.I)
    files = sorted({f for f in files if rx.search(os.path.basename(f))})
    return files[:limit] if limit else files


def build_prep(args, files):
    """构造源归一化配置，并在父进程里先探测一遍（RAW 探测要开文件，别放到子进程重复做）"""
    mode = args.source
    prep = {"mode": mode}
    if SN is None or mode in ("standard", "off"):
        return prep, []
    prep.update({"log_profile": args.log_profile, "raw_anchor": args.raw_anchor,
                 "raw_denoise": args.raw_denoise, "exposure": args.prep_exposure,
                 "knee": args.prep_knee, "knee_k": args.prep_knee_k, "probes": {}})
    infos = []
    for f in files:
        info = SN.probe(f)
        prep["probes"][os.path.abspath(f)] = info
        infos.append(info)
    return prep, infos


def print_prep_report(infos):
    if not infos:
        return
    print("── 源格式判定与预处理 ──────────────────────────────────────────────")
    print(f"{'文件':<24}{'判定':<13}{'置信':<8}处理")
    for i in infos:
        print(f"{i.name:<24}{i.kind:<13}{i.confidence:<8}{i.action}")
        for r in i.reasons[:2]:
            print(f"{'':<45}· {r}")
        if i.note:
            print(f"{'':<45}! {i.note}")
    n = {}
    for i in infos:
        n[i.kind] = n.get(i.kind, 0) + 1
    print("  汇总：" + "，".join(f"{k} {v} 个" for k, v in sorted(n.items())) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description="本地胶片感批量模拟（LUT 曲线 + 通道色偏 + 颗粒 + 高光溢出）")
    ap.add_argument("-i", "--input", nargs="*", default=[], help="输入目录或文件（可多个）")
    ap.add_argument("-o", "--output", default=None, help="输出目录")
    ap.add_argument("-p", "--preset", default="portra400", help="预设名，见 --list")
    ap.add_argument("--pattern", default=None, help=f"文件名正则过滤，默认 {IMG_RE}")
    ap.add_argument("--recursive", action="store_true", help="递归子目录")
    ap.add_argument("--limit", type=int, default=None, help="只处理前 N 张")
    ap.add_argument("--suffix", default=None, help="输出文件名后缀，默认 _<preset>")
    ap.add_argument("--quality", type=int, default=96, help="JPEG 质量（默认 96，4:4:4 无色度抽样）")
    ap.add_argument("--format", default="jpg", choices=["jpg", "png"], help="输出格式")
    ap.add_argument("--jobs", type=int, default=min(6, os.cpu_count() or 4), help="并行进程数")
    ap.add_argument("--max-edge", type=int, default=None, help="限制长边像素（预览用）")
    ap.add_argument("--seed", type=int, default=20260926, help="颗粒随机种子（同种子结果可复现）")
    ap.add_argument("--lut", default=None, help="外部 .cube 3D LUT 路径")
    ap.add_argument("--lut-strength", type=float, default=1.0, help="3D LUT 混合强度")
    ap.add_argument("--style-strength", type=float, default=1.0,
                    help="内置风格强度：1=显著版（默认），0=旧版，0~1 可微调；neutral 不强化")
    ap.add_argument("--creative-direction", default="auto",
                    choices=["auto", "warm", "cool", "vivid", "soft"],
                    help="再创作方向：auto=随预设；warm=琥珀暖调；cool=青蓝暗部；"
                         "vivid=浓艳反差；soft=柔和粉彩")
    ap.add_argument("--save-creative", action="store_true",
                    help="保存胶卷处理前的色彩再创作中间图，便于核对三阶段流程")
    ap.add_argument("--grain", type=float, default=None, help="覆盖颗粒强度（预设值 × 此系数外的绝对值）")
    ap.add_argument("--halation", type=float, default=None, help="覆盖高光溢出强度")
    ap.add_argument("--vignette", type=float, default=None, help="覆盖暗角强度")
    ap.add_argument("--sat", type=float, default=None, help="饱和度乘数")
    ap.add_argument("--contrast", type=float, default=None, help="对比度乘数")
    ap.add_argument("--exposure", type=float, default=None, help="曝光补偿 EV")
    ap.add_argument("--temp", type=float, default=None, help="白平衡偏移（正=偏暖）")
    ap.add_argument("--tint", type=float, default=None, help="白平衡偏移（正=偏绿）")
    ap.add_argument("--acutance", type=float, default=None, help="边缘锐度覆盖")
    ap.add_argument("--sheet", default=None, help="生成全预设对比图（传入一张源图）")
    ap.add_argument("--sheet-presets", default=None, help="对比图包含的预设（逗号分隔）")
    ap.add_argument("--group", default=None, choices=["fuji", "kodak", "all"],
                    help="按家族批量处理或生成对比图")
    ap.add_argument("--source", default="auto",
                    choices=["auto", "raw", "log", "standard", "off"],
                    help="源归一化：auto=先判断色彩格式再决定（默认）；raw=强制显影 RAW；"
                         "log=强制按灰片反 log；standard/off=跳过预处理")
    ap.add_argument("--log-profile", default="auto",
                    help="强制 log 曲线：" + (", ".join(SN.LOG_PROFILES) if SN else "无") + " 或 auto")
    ap.add_argument("--raw-anchor", default="reference-tone",
                    choices=["reference-tone", "reference", "auto", "none"],
                    help="RAW 曝光锚定：reference-tone=对齐同目录同名成片的中位亮度并叠加其色调"
                         "形状（默认，最接近相机直出观感）；reference=只对齐中位亮度；"
                         "auto=无参考成片时按分位；none=不动")
    ap.add_argument("--raw-denoise", default="off", choices=["off", "light", "full"],
                    help="RAW 显影降噪强度（高 ISO 夜景可用 light）")
    ap.add_argument("--prep-exposure", type=float, default=0.0, help="归一化阶段曝光补偿 EV")
    ap.add_argument("--prep-knee", type=float, default=None, help="高光软肩起点（线性反射率，默认 0.90）")
    ap.add_argument("--prep-knee-k", type=float, default=None, help="软肩压缩强度（默认 1.0）")
    ap.add_argument("--no-prep-report", action="store_true", help="不打印源格式判定表")
    ap.add_argument("--prep-json", default=None, help="把判定与归一化参数写入 JSON")
    ap.add_argument("--no-raw", action="store_true", help="默认扫描时不纳入 RAW 文件")
    ap.add_argument("--list", action="store_true", help="列出全部预设")
    ap.add_argument("--list-source-profiles", action="store_true", help="列出支持的 log 曲线与其指纹")
    ap.add_argument("--dry-run", action="store_true", help="只列文件，不处理")
    args = ap.parse_args(argv)

    if not 0.0 <= args.style_strength <= 1.0:
        ap.error("--style-strength 必须在 0 到 1 之间")

    if args.prep_knee is None:
        args.prep_knee = SN.DEFAULT_KNEE if SN else 0.90
    if args.prep_knee_k is None:
        args.prep_knee_k = SN.DEFAULT_KNEE_K if SN else 1.0

    if args.list_source_profiles:
        if SN is None:
            print("source_normalize.py 不可用", file=sys.stderr)
            return 2
        print(f"{'profile':<11}{'名称':<38}{'基灰':>9}{'18%灰':>9}{'90%白':>9}")
        print("-" * 108)
        for k, v in SN.LOG_PROFILES.items():
            print(f"{k:<11}{v['label']:<38}{v['floor']:>9.5f}{v['mid']:>9.5f}{v['white']:>9.5f}")
            print(f"{'':<11}{v['source']}")
        return 0

    if args.list:
        def dump(title, names):
            print(f"\n── {title} " + "─" * max(0, 92 - len(title)))
            print(f"{'预设':<16} {'名称':<28} 说明")
            for k in names:
                v = PRESETS[k]
                print(f"{k:<16} {v['label']:<28} {v['note']}")
        dump("富士 FUJI", FUJI_PRESETS)
        dump("柯达 / 其他 KODAK & OTHERS", KODAK_PRESETS + ["hp5"])
        dump("参考画面复刻", REFERENCE_PRESETS)
        dump("对照", ["neutral"])
        print()
        return 0

    if args.group and args.group != "all" and not args.sheet:
        args.preset = None
        group_names = FUJI_PRESETS if args.group == "fuji" else KODAK_PRESETS

    opts = {
        "seed": args.seed,
        "lut": args.lut,
        "lut_strength": args.lut_strength,
        "style_strength": args.style_strength,
        "creative_direction": args.creative_direction,
        "save_creative": args.save_creative,
        "res_scale": 1.0,
        "override": {
            "grain": args.grain, "halation": args.halation, "vignette": args.vignette,
            "sat": args.sat, "contrast": args.contrast, "exposure": args.exposure,
            "temp": args.temp, "tint": args.tint, "acutance": args.acutance,
        },
    }

    # --group 不带 --sheet 时：一次跑完该家族的全部预设
    if args.group and args.group != "all" and not args.sheet and args.input:
        names = FUJI_PRESETS if args.group == "fuji" else KODAK_PRESETS
        rc = 0
        for n in names:
            sub = list(argv) if argv else sys.argv[1:]
            drop = {"--group", args.group, "--prep-json"}
            if args.prep_json:
                drop.add(args.prep_json)
            sub = [a for a in sub if a not in drop]
            rc = max(rc, main(sub + ["-p", n, "--output",
                                     os.path.join(args.output or ".", n)]))
        return rc

    if args.sheet:
        files = [args.sheet]
        if args.source not in ("standard", "off") and SN is not None:
            prep, infos = build_prep(args, files)
            opts["prep"] = prep
            if not args.no_prep_report:
                print_prep_report(infos)
        names = None
        if args.sheet_presets:
            names = args.sheet_presets.split(",")
        elif args.group == "fuji":
            names = FUJI_PRESETS
        elif args.group == "kodak":
            names = KODAK_PRESETS
        if not names:
            names = [k for k in PRESETS if k != "neutral"]
        out = os.path.join(args.output or os.path.dirname(os.path.abspath(args.sheet)),
                           f"preset-comparison_{os.path.splitext(os.path.basename(args.sheet))[0]}.jpg")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        build_sheet(args.sheet, out, names, max_edge=args.max_edge or 1400, opts=opts)
        print("对比图:", out)
        return 0

    if args.preset not in PRESETS:
        print(f"未知预设 {args.preset}，可用：{', '.join(PRESETS)}", file=sys.stderr)
        return 2
    if not args.input:
        print("需要 -i 指定输入", file=sys.stderr)
        return 2

    files = collect(args.input, args.pattern, args.recursive, args.limit,
                    allow_raw=not args.no_raw)
    if not files:
        print("没有匹配的文件", file=sys.stderr)
        return 2
    prep, infos = build_prep(args, files)
    opts["prep"] = prep
    if not args.dry_run and not args.no_prep_report:
        print_prep_report(infos)
    if args.prep_json:
        with open(args.prep_json, "w", encoding="utf-8") as fh:
            json.dump([i.to_dict() for i in infos], fh, ensure_ascii=False, indent=2)
        print(f"判定与还原参数 → {args.prep_json}")

    outdir = args.output or os.path.join(os.path.dirname(os.path.abspath(files[0])), f"film_{args.preset}")
    os.makedirs(outdir, exist_ok=True)
    suffix = args.suffix if args.suffix is not None else f"_{args.preset}"
    ext = "." + args.format

    jobs = []
    for f in files:
        stem = os.path.splitext(os.path.basename(f))[0]
        jobs.append((os.path.abspath(f), os.path.join(outdir, stem + suffix + ext),
                     args.preset, opts, args.quality, args.max_edge))

    print(f"预设 {args.preset}（{PRESETS[args.preset]['label']}） | {len(jobs)} 张 | {args.jobs} 进程")
    print(f"输出 → {outdir}")
    if args.dry_run:
        for j in jobs:
            print("  ", j[0])
        return 0

    ok = fail = 0
    t0 = time.time()
    if args.jobs <= 1:
        for j in jobs:
            p, o, good, t, err, note = process_one(j)
            ok, fail = (ok + 1, fail) if good else (ok, fail + 1)
            print(("  [OK] " if good else "  [ERR] ") + os.path.basename(o) + f"  {t}"
                  + (f"  [{note}]" if note else "")
                  + ("" if good else "\n" + err))
    else:
        with ProcessPoolExecutor(max_workers=args.jobs) as ex:
            futs = [ex.submit(process_one, j) for j in jobs]
            for fu in as_completed(futs):
                p, o, good, t, err, note = fu.result()
                ok, fail = (ok + 1, fail) if good else (ok, fail + 1)
                print(("  [OK] " if good else "  [ERR] ") + os.path.basename(o) + f"  {t}"
                      + (f"  [{note}]" if note else "")
                      + ("" if good else "\n" + err))

    print(f"完成：成功 {ok}，失败 {fail}，总耗时 {time.time() - t0:.1f}s")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
