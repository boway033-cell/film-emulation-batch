# -*- coding: utf-8 -*-
"""
source_normalize.py — 源归一化：调色之前先把输入还原成「中性 sRGB」基片

为什么需要这一步
────────────────
胶片模拟预设的曲线、色偏、颗粒，都是按「已经正常显影的 sRGB 成片」标定的。
下面两类素材直接喂进去，预设的前提就不成立：

1. **RAW**（ARW / CR2 / CR3 / NEF / N-RW / DNG / RAF / ORF / RW2 / PEF / SRW / X3F…）
   传感器原始数据：没有白平衡、没有色调曲线、没有饱和度。
   注意 —— RAW **只需要显影，绝不需要反 log**。Sony 官方技术文档写得很直接：
   「RAW is sensor native data and RAW does not apply any color space nor log curve.
   RAW is always recorded by 16bit Scene linear.」
   机内 Picture Profile 选 S-Log3 也只影响成片，不影响 RAW。
   所以「RAW + 反 log」= 对同一份数据连续两次错误处理。

2. **灰片 / Log 成片**（S-Log3、S-Log2、V-Log、LogC3、LogC4、Apple Log…）
   暗部被抬到固定基灰、高光被对数压缩、饱和度整体摊薄。
   必须先反 log 还原成线性反射率，否则预设会建在错误的对比与饱和度基准上 ——
   典型后果是「再加一次对比、再加一次饱和」，画面直接发灰发脏。

手机照片默认**跳过**本步骤（其 JPEG/HEIC 通常已是完整显影的成品），
除非实测判定它同样是灰片。判定**只看像素，不看机型**；机型只用来抬高门槛
（手机误判代价更高：把正常照片当灰片处理会毁掉它，反之只是白干一步）。

判定依据（全部来自像素，可复核）
────────────────
每条 log 曲线都把「线性 0」映射到一个**固定的基灰码值**，这是它的指纹 ——
真实灰片画面的最暗处不可能低于该值。实测 0.2% 分位亮度与之比对即可定位曲线。

    profile      基灰 enc(0)   18% 中灰   90% 白    曲线出处
    s_log3        0.09286     0.41056    0.58445   Sony 白皮书
    s_log2        0.08825     0.38497    0.62218   Sony S-Log2 OETF
    v_log         0.12500     0.42331    0.58817   Panasonic 手册
    logc3         0.09281     0.39101    0.55943   ARRI LogC v3
    logc4         0.09286     0.27840    0.41796   ARRI LogC4 spec
    apple_log     0.15048     0.48827    0.68169   Apple 白皮书

**基灰只能定位「族」**：S-Log3 / LogC3 / LogC4 的基灰完全相同（都是 95/1023，
这是 Rec.709 视频范围黑电平的通用设计点），S-Log2 也只低 0.0046；
`v_log`、`apple_log` 则各自独立。但同族曲线的中间调差别极大 ——
同一张 S-Log3 画面按 LogC4 解码，渲染结果平均差 **68/255**，绝不能不区分。

所以族内还要再裁决一层：① EXIF 的机厂牌（Sony→S-Log，ARRI→LogC，Apple→Apple Log）；
② 场景中灰先验（假设曝光正常、场景中位反射率接近 10%，这正是相机测光所用的同一假设）。
族内差异仍可见时置信度只标 medium，并把备选与它们的渲染差一并列出，不假装确定。

判定流程：`6 项像素特征计分 → 是否灰片 → 可行性过滤（基灰不得高于实测黑位）
→ 族内裁决 → 渲染等效性检验 → 输出曲线 + 置信度`。

CLI
────────────────
  python source_normalize.py <文件或目录...>                 # 只判断，打印判定表
  python source_normalize.py <目录> -o <输出目录>            # 判断 + 还原写出
  python source_normalize.py --selftest [--crosscheck]       # 曲线锚点/往返/判定自检
  python source_normalize.py --list-profiles                 # 各曲线指纹与出处

依赖：Pillow + numpy；RAW 另需 rawpy（缺失时该文件标 unsupported 并给出提示）。
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field, asdict

import numpy as np
from PIL import Image, ImageOps

# ══════════════════════════════════════════════════════════ 常量
RAW_EXTS = {
    ".arw", ".srf", ".sr2", ".cr2", ".cr3", ".crw", ".nef", ".nrw", ".dng",
    ".raf", ".orf", ".rw2", ".pef", ".srw", ".x3f", ".3fr", ".erf", ".kdc",
    ".mef", ".mos", ".mrw", ".raw", ".rwl", ".iiq", ".r3d",
}

# 手机厂商：只用于抬高判定门槛（+1 分），不用于豁免
PHONE_MAKES = (
    "apple", "iphone", "xiaomi", "redmi", "huawei", "honor", "samsung", "galaxy",
    "oppo", "vivo", "iqoo", "oneplus", "realme", "google", "pixel", "motorola",
    "nokia", "meizu", "zte", "nubia", "blackshark", "sony ericsson", "nothing",
)

LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)

SCORE_LOG_CAMERA = 4      # 相机素材：总分 >= 4 判为灰片
SCORE_LOG_PHONE = 5       # 手机素材：门槛 +1
FLOOR_TOL = 0.035         # 黑位残差上限（超过则曲线未知，走通用还原）
FLOOR_TOL_HIGH = 0.020    # 单成员组内的高置信度残差上限
FEAS_TOL = 0.0022         # 可行性容差：曲线基灰不得高于实测黑位 + 此值（8bit 量化上限 1/510）
QUANT_TOL = 0.0025        # 「黑位正好触到基灰」的判定带：只有落在这里才算指纹确认
GROUP_TOL = 0.0025        # 基灰相近（视为同一族）的残差带宽
DELTA_EQUIV = 0.6         # 两候选曲线渲染结果平均差 < 0.6/255 视为等效
MID_TARGET = 0.10         # 场景中位反射率先验（相机测光用的同一假设）
TOP_TARGET = 1.0          # 场景 p99.5 反射率的典型值（介于漫反射白与镜面高光之间）

# ── 灰片签名（必要条件，先于计分）：log OETF 的两个直接后果
#   ① 高光被对数压缩 → 动态跨度收缩（画面很难同时出现真正的黑与真正的白）
#   ② 基灰托底 → 高光离 255 有余量
# 实测校准（30 个真灰片样本 = 5 个真实线性源 × 6 条曲线；12 张真实相机 JPEG）：
#   真灰片 span 0.276~0.741、headroom 0.154~0.605（两条同时成立）
#   普通成片 span 0.765~0.970、headroom 0.000~0.139（两条同时不成立）
# 取 (span <= 0.75) or (headroom >= 0.14) 作为必要条件：两侧各留 ≥0.009 余量，
# 用「或」让任一条件单独成立即可放行，避免单点阈值卡死。
# —— 这一条专门拦「低对比雾天照 / 逆光大光比照」这类只靠计分会误判的素材。
FLAT_SPAN_MAX = 0.75      # 灰片签名①：动态跨度上限
FLAT_HEAD_MIN = 0.14      # 灰片签名②：高光余量下限

# ── 合理性检验：反 log 后场景必须像「一个曝光正常的场景」，否则不予还原。
# log 曲线近似 V = a + b·log10(L)，用错曲线解码得到的是 c·L^g —— 真场景的幂律变形。
# 因此「解码后中位反射率」在测 g 是否≈1，「解码后 p99.5」在测高光是否被拉爆。
# 实测校准（同上）：真灰片用**自身曲线**解码后 中位 0.017~0.103、p99.5 0.67~3.47；
#   12 张普通成片用**任意**曲线解码后 p99.5 均 ≥4.19（最低 4.19 = DSC05718 用 s_log2）。
# p99.5 上界取 3.9：真值上限 3.47 的 1.12 倍、误判下限 4.19 的 0.93 倍，居中切开。
# 中位带取 [0.012, 0.60]：下界覆盖最暗的正常场景（实测 0.017），上界留 6 档。
# 偏严格一侧是有意的：把正常照片当灰片还原会毁图，而漏掉一张真 Log 只是少还原一步，
# 可用 --source log 强制。
MED_LO, MED_HI = 0.012, 0.60
TOP_LO, TOP_HI = 0.50, 3.9
DEFAULT_KNEE = 0.90       # 高光软肩起点（线性反射率）
DEFAULT_KNEE_K = 1.0      # 肩部压缩强度（越大越紧）


# ══════════════════════════════════════════════════════════ sRGB ↔ 线性
def srgb_to_linear(x):
    x = np.asarray(x, dtype=np.float32)
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4).astype(np.float32)


def linear_to_srgb(x):
    x = np.clip(np.asarray(x, dtype=np.float32), 0.0, None)
    return np.where(x <= 0.0031308, x * 12.92,
                    1.055 * np.power(x, 1.0 / 2.4) - 0.055).astype(np.float32)


# ══════════════════════════════════════════════════════════ Log 曲线（官方系数）
# 每条都是「归一化码值 0-1 ↔ 线性反射率」的可逆曲线，系数逐条注明出处。

def _enc_slog3(L):
    L = np.maximum(np.asarray(L, np.float32), 0.0)
    return np.where(L >= 0.01125000,
                    (420.0 + np.log10((L + 0.01) / 0.19) * 261.5) / 1023.0,
                    (L * (171.2102946929 - 95.0) / 0.01125000 + 95.0) / 1023.0)


def _dec_slog3(V):
    V = np.asarray(V, np.float32)
    return np.where(V >= 171.2102946929 / 1023.0,
                    np.power(10.0, (V * 1023.0 - 420.0) / 261.5) * 0.19 - 0.01,
                    (V * 1023.0 - 95.0) * 0.01125000 / (171.2102946929 - 95.0))


def _enc_slog2(L):
    L = np.maximum(np.asarray(L, np.float32), 0.0)
    return 4.0 * (16.0 + 219.0 * (0.616596 + 0.03
                                  + 0.432699 * np.log10(0.037584 + L / 0.9))) / 1023.0


def _dec_slog2(V):
    V = np.asarray(V, np.float32)
    return (np.power(10.0, ((V * 1023.0 / 4.0 - 16.0) / 219.0
                            - 0.616596 - 0.03) / 0.432699) - 0.037584) * 0.9


def _enc_vlog(L):
    L = np.maximum(np.asarray(L, np.float32), 0.0)
    return np.where(L < 0.01, 5.6 * L + 0.125,
                    0.241514 * np.log10(L + 0.00873) + 0.598206)


def _dec_vlog(V):
    V = np.asarray(V, np.float32)
    return np.where(V < 0.181, (V - 0.125) / 5.6,
                    np.power(10.0, (V - 0.598206) / 0.241514) - 0.00873)


# ARRI LogC3：SUP 3.x + Linear Scene Exposure Factor + EI 800（业界最常用组合）
LOGC3 = {"cut": 0.010591, "a": 5.555556, "b": 0.052272, "c": 0.247190,
         "d": 0.385537, "e": 5.367655, "f": 0.092809}


def _enc_logc3(L):
    L = np.maximum(np.asarray(L, np.float32), 0.0)
    return np.where(L > LOGC3["cut"],
                    LOGC3["c"] * np.log10(LOGC3["a"] * L + LOGC3["b"]) + LOGC3["d"],
                    LOGC3["e"] * L + LOGC3["f"])


def _dec_logc3(V):
    V = np.asarray(V, np.float32)
    thr = LOGC3["e"] * LOGC3["cut"] + LOGC3["f"]
    return np.where(V > thr,
                    (np.power(10.0, (V - LOGC3["d"]) / LOGC3["c"]) - LOGC3["b"]) / LOGC3["a"],
                    (V - LOGC3["f"]) / LOGC3["e"])


# ARRI LogC4（ARRI 官方 specification，2025-01 版）
_L4_A = (2 ** 18 - 16) / 117.45
_L4_B = (1023 - 95) / 1023
_L4_C = 95 / 1023
_L4_S = (7 * np.log(2) * 2 ** (7 - 14 * _L4_C / _L4_B)) / (_L4_A * _L4_B)
_L4_T = (2 ** (14 * (-_L4_C / _L4_B) + 6) - 64) / _L4_A


def _enc_logc4(E):
    E = np.asarray(E, np.float32)
    return np.where(E >= _L4_T,
                    (np.log2(_L4_A * E + 64) - 6) / 14 * _L4_B + _L4_C,
                    (E - _L4_T) / _L4_S)


def _dec_logc4(P):
    P = np.asarray(P, np.float32)
    return np.where(P >= 0,
                    (np.power(2.0, 14 * ((P - _L4_C) / _L4_B) + 6) - 64) / _L4_A,
                    P * _L4_S + _L4_T)


# Apple Log Profile（Apple 白皮书，经 ACES IDT.Apple.AppleLog_BT2020.ctl 核对）
APL = {"R0": -0.05641088, "Rt": 0.01, "sigma": 47.28711236,
       "beta": 0.00964052, "gamma": 0.08550479, "delta": 0.69336945}
APL["Pt"] = APL["sigma"] * (APL["Rt"] - APL["R0"]) ** 2


def _enc_apple(R):
    R = np.asarray(R, np.float32)
    return np.where(
        R >= APL["Rt"], APL["gamma"] * np.log2(np.maximum(R + APL["beta"], 1e-6)) + APL["delta"],
        np.where(R >= APL["R0"], APL["sigma"] * (R - APL["R0"]) ** 2, 0.0))


def _dec_apple(P):
    P = np.asarray(P, np.float32)
    return np.where(
        P >= APL["Pt"], np.power(2.0, (P - APL["delta"]) / APL["gamma"]) - APL["beta"],
        np.where(P >= 0.0, np.sqrt(np.maximum(P, 0.0) / APL["sigma"]) + APL["R0"], APL["R0"]))


# ══════════════════════════════════════════════════════════ 曲线档案
LOG_PROFILES: dict[str, dict] = {}


def _register(name, label, vendor, enc, dec, source):
    LOG_PROFILES[name] = {
        "name": name, "label": label, "vendor": vendor,
        "enc": enc, "dec": dec, "source": source,
        "floor": float(np.asarray(enc(0.0)).reshape(-1)[0]),
        "mid": float(np.asarray(enc(0.18)).reshape(-1)[0]),
        "white": float(np.asarray(enc(1.0)).reshape(-1)[0]),
    }


_register("s_log3", "Sony S-Log3", "sony", _enc_slog3, _dec_slog3,
          "Sony《Technical Summary for S-Gamut3/S-Log3》附录 S-Log3 Formula")
_register("s_log2", "Sony S-Log2", "sony", _enc_slog2, _dec_slog2,
          "Sony S-Log2 OETF（与 colour-science _linear_to_s_log2 同式）")
_register("v_log", "Panasonic V-Log", "panasonic", _enc_vlog, _dec_vlog,
          "Panasonic《V-Log/V-Gamut Reference Manual》")
_register("logc3", "ARRI LogC3 (SUP3.x / LSEF / EI800)", "arri", _enc_logc3, _dec_logc3,
          "ARRI LogC v3 技术说明（cut=0.010591, a=5.555556, b=0.052272…）")
_register("logc4", "ARRI LogC4", "arri", _enc_logc4, _dec_logc4,
          "ARRI《LogC4 Logarithmic Color Space》specification 2025-01")
_register("apple_log", "Apple Log Profile", "apple", _enc_apple, _dec_apple,
          "Apple《Apple Log Profile》白皮书 / ACES IDT.Apple.AppleLog_BT2020.ctl")

# 机型 → 优先曲线（仅当黑位指纹无法区分时用于裁决，不替代像素判定）
VENDOR_PRIOR = {
    "sony": ["s_log3", "s_log2"],
    "arri": ["logc3", "logc4"],
    "panasonic": ["v_log"],
    "apple": ["apple_log"],
}


# ══════════════════════════════════════════════════════════ 曲线工具
def soft_shoulder(y, knee=DEFAULT_KNEE, k=DEFAULT_KNEE_K):
    """指数软肩：knee 以下恒等，以上斜率连续地衰减到 0、渐近于 1。

    y' = knee + (1-knee) * (1 - exp(-k * (y-knee)/(1-knee)))
    在 y=knee 处值与一阶导都连续（导数为 1），不会出现折点。
    """
    y = np.asarray(y, np.float32)
    if knee >= 1.0:
        return y
    over = np.maximum(y - knee, 0.0)
    return np.where(y > knee,
                    knee + (1.0 - knee) * (1.0 - np.exp(-k * over / (1.0 - knee))),
                    y)


def render_neutral(lin, exposure=0.0, knee=DEFAULT_KNEE, knee_k=DEFAULT_KNEE_K,
                   saturation=1.0):
    """线性反射率 → 中性 sRGB 显示值（无 S 曲线、无饱和提升，给后续调色留余地）"""
    L = np.asarray(lin, np.float32)
    if exposure:
        L = L * (2.0 ** exposure)
    L = soft_shoulder(np.clip(L, 0.0, 64.0), knee, knee_k)
    if saturation != 1.0:
        g = (L @ LUMA).astype(np.float32)[..., None]
        L = g + (L - g) * saturation
    return linear_to_srgb(np.clip(L, 0.0, 1.0))


# ══════════════════════════════════════════════════════════ 像素特征与判定
def _box3(a):
    """3×3 盒式均值（边界复制），用于压制像素级噪声"""
    p = np.pad(a, 1, mode="edge")
    s = (p[:-2, :-2] + p[:-2, 1:-1] + p[:-2, 2:] +
         p[1:-1, :-2] + p[1:-1, 1:-1] + p[1:-1, 2:] +
         p[2:, :-2] + p[2:, 1:-1] + p[2:, 2:])
    return (s / 9.0).astype(np.float32)


def _noise_sigma(lum):
    """用相邻像素差的中位绝对值估计噪声 σ（i.i.d. 时 MAD(|Δ|)=0.6745·σ√2）"""
    d = np.abs(np.diff(lum, axis=1)).astype(np.float32)
    if d.size < 64:
        return 0.0
    return float(np.median(d) / (0.6745 * math.sqrt(2.0)))


def _features(arr_u8):
    x = arr_u8.astype(np.float32) / 255.0
    lum = (x @ LUMA).astype(np.float32)
    mx = x.max(2)
    mn = x.min(2)
    sat = np.where(mx > 1e-6, (mx - mn) / np.maximum(mx, 1e-6), 0.0)
    p = np.percentile(lum, [0.2, 5, 50, 95, 99.5])
    # 稳健黑位（pedestal）：先 3×3 均值压噪再取 0.2% 分位。
    # 噪声零均值对称，直接对含噪画面取低分位会系统性偏低（理论 −0.55σ，
    # σ=1.5/255 时约 −0.0032）—— 而 log 基灰指纹要求 ≤0.002 的精度，
    # 不滤波就会把真曲线误判成「基灰高于实测黑位」（实测：噪声让 24/24 全错）。
    lb = _box3(lum)
    return {
        "floor": float(p[0]), "pedestal": float(np.percentile(lb, 0.2)),
        "noise": round(_noise_sigma(lum), 4),
        "p05": float(p[1]), "mid": float(p[2]),
        "p95": float(p[3]), "top": float(p[4]),
        "span": float(p[4] - p[0]), "headroom": float(1.0 - p[4]),
        "sat_mean": float(sat.mean()), "sat_p90": float(np.percentile(sat, 90)),
        "frac_dark": float((lum < 0.10).mean()),
        "frac_bright": float((lum > 0.95).mean()),
    }


def _flat_signature(f):
    """灰片签名：log OETF 留下的两个必要条件之一必须成立。

    `span <= FLAT_SPAN_MAX`（高光被对数压缩、画面难以同时出现真黑与真白）
    或 `headroom >= FLAT_HEAD_MIN`（基灰托底、高光离 255 有余量）。

    这是**必要条件不是充分条件**，但足以拦掉最危险的一类误判 ——
    低对比的雾天照 / 逆光大光比照：它们黑位正常、饱和度偏低、深暗部很少，
    能骗过六项计分，却同时拥有很大的动态跨度与几乎为零的高光余量。
    实测 12 张普通成片全部不成立（span ≥0.765 且 headroom ≤0.139），
    30 个真灰片样本全部成立（span ≤0.741 / headroom ≥0.154）。
    """
    span_ok = f["span"] <= FLAT_SPAN_MAX
    head_ok = f["headroom"] >= FLAT_HEAD_MIN
    if span_ok or head_ok:
        return True, (f"灰片签名成立：动态跨度 {f['span']:.3f} ≤ {FLAT_SPAN_MAX}"
                      if span_ok else
                      f"灰片签名成立：高光余量 {f['headroom']:.3f} ≥ {FLAT_HEAD_MIN}")
    return False, (f"灰片签名不成立：动态跨度 {f['span']:.3f} > {FLAT_SPAN_MAX} 且"
                   f"高光余量 {f['headroom']:.3f} < {FLAT_HEAD_MIN}"
                   f"→ 画面里同时有真黑与真白，不是 log/flat 编码")


def _score_flat(f):
    """灰片六项特征各计 1 分，返回 (得分, 依据)"""
    hits = []
    if f["floor"] >= 0.055:
        hits.append(f"黑位抬升 {f['floor']:.3f}（正常成片一般 <0.05）")
    if f["span"] <= FLAT_SPAN_MAX:
        hits.append(f"动态跨度仅 {f['span']:.3f}（正常成片一般 >0.78）")
    if f["headroom"] >= FLAT_HEAD_MIN:
        hits.append(f"高光未顶到 255，余量 {f['headroom']:.3f}")
    if f["frac_dark"] < 0.006:
        hits.append(f"几乎无深于 10% 的像素（{f['frac_dark'] * 100:.2f}%）")
    if f["frac_bright"] < 0.004:
        hits.append(f"几乎无浅于 95% 的像素（{f['frac_bright'] * 100:.2f}%）")
    if f["sat_mean"] <= 0.22:
        hits.append(f"平均饱和度 {f['sat_mean']:.3f} 明显摊薄")
    return len(hits), hits


def _candidates(f):
    """按「稳健黑位残差」给全部曲线排序，返回 [(profile, 残差)]

    残差 = 稳健黑位 pedestal − 该曲线基灰。log 编码把线性 0 映射到固定基灰，
    所以真实画面的黑位不可能低于该曲线基灰 —— 残差显著为负的曲线直接判为不可能。
    但 8bit 量化会让残差出现最多 −1/510 ≈ −0.002 的假负值（例：Apple Log 基灰
    0.15048 × 255 = 38.37 → 存成 38 → 实测 0.14902，残差 −0.0015），
    所以容差之内一律按「绝对值更小 = 解释更紧」排序，而不是把负残差一律排在后面。

    注意用 pedestal（3×3 压噪后的 0.2% 分位）而非原始 floor：噪声会把原始低分位
    往下拉约 0.55σ，足以吞掉 S-Log3 / LogC3 / LogC4 之间 0.00005 的基灰差。
    可行性容差随噪声自适应放宽（残差为负只说明「不可能是这条曲线」，而噪声本身
    会让残差虚低），但**接触判定（QUANT_TOL）不放大** —— 噪声里本就无法精确锁定曲线，
    此时应当降低置信度，而不是假装确认。
    """
    ped = f.get("pedestal", f["floor"])
    feas = FEAS_TOL + 0.6 * float(f.get("noise", 0.0) or 0.0) / 3.0
    out = [(n, ped - p["floor"]) for n, p in LOG_PROFILES.items()
           if ped - p["floor"] >= -feas]
    out.sort(key=lambda t: abs(t[1]))
    return out


def _output_delta(sub01, a, b):
    """两张曲线在同一张图上渲染结果的平均差（0-255 单位）"""
    ra = render_neutral(np.clip(LOG_PROFILES[a]["dec"](sub01), 0, 64))
    rb = render_neutral(np.clip(LOG_PROFILES[b]["dec"](sub01), 0, 64))
    return float(np.abs(ra - rb).mean() * 255.0)


def _plausibility(arr_u8, profile):
    """曲线选定后的合理性检验：反 log 出来的场景必须像「曝光正常的场景」。

    必须用**足够大**的图来算：p99.5 对缩放很敏感，128px 缩略图的 LANCZOS 平滑
    会把高光极值抹平，导致本该被拦下的画面通过检验（实测过：同一张图 420px 的
    p99.5=8.12，128px 掉到 5 以下）。故一律用 700px 级的图算。

    返回 (是否通过, 说明, 指标 dict)
    """
    v = arr_u8.astype(np.float32) / 255.0
    lum = np.clip(LOG_PROFILES[profile]["dec"](v), 0.0, 64.0) @ LUMA
    med = float(np.median(lum))
    top = float(np.percentile(lum, 99.5))
    metrics = {"decoded_median": round(med, 4), "decoded_top99": round(top, 3)}
    ok = (MED_LO <= med <= MED_HI) and (TOP_LO <= top <= TOP_HI)
    if ok:
        why = f"反 log 后场景中位反射率 {med:.3f}、p99.5 {top:.2f}，符合正常曝光"
    else:
        bad = []
        if not (MED_LO <= med <= MED_HI):
            bad.append(f"中位反射率 {med:.3f} 超出 [{MED_LO}, {MED_HI}]")
        if not (TOP_LO <= top <= TOP_HI):
            bad.append(f"p99.5 反射率 {top:.2f} 超出 [{TOP_LO}, {TOP_HI}]")
        why = "反 log 后场景不像正常曝光（" + "；".join(bad) + "）"
    return ok, why, metrics


def _prior_profiles(make):
    mk = (make or "").lower()
    for vendor, names in VENDOR_PRIOR.items():
        if vendor in mk:
            return names
    return []


def _select_profile(f, sub01, plaus_arr, make, cands):
    """曲线裁决（三层）：

    ① **基灰否决**：残差显著为负（基灰高于实测黑位）的曲线不可能，直接剔除。
       —— 注意 8bit 量化会带来 ≤0.002 的假负值；画面没有纯黑时残差会大幅偏正。
    ② **场景合理性仲裁**：逐个候选反 log，看解出来的场景是否像「曝光正常的场景」
       （中位反射率与 p99.5 落在实测标定的带内）。log 曲线近似 V = a + b·log10(L)，
       用错曲线解码得到 c·L^g —— 真场景的幂律变形，中位测 g、p99.5 测高光是否被拉爆。
       **这是唯一能在画面没有纯黑时区分曲线的证据**（S-Log3 / LogC3 / LogC4 基灰相同）。
    ③ **排序**，分两档：
       · 若黑位**正好触到某条曲线的基灰**（残差 ≤ QUANT_TOL）→ 指纹确认，
         按 |残差| 升序（解释最紧者胜），机厂牌先验在并列时收敛；
       · 否则画面里没有纯黑，基灰只给出下界、不含信息 —— 此时按 |残差| 排序会
         系统性偏向基灰最高的曲线，所以改按「场景典型度代价」
         `|log2(med/0.10)| + |log2(top/1.0)|` 升序（越像典型曝光越优先）。

    返回 (profile|'', confidence, 依据, 提示, 指标)
    """
    reasons, metrics = [], {}
    if not cands:
        return "", "low", ["所有曲线的基灰都高于实测黑位 → 不是已知 log 曲线"], "", metrics

    order = {n: i for i, n in enumerate(LOG_PROFILES)}
    ped = f.get("pedestal", f["floor"])
    scored = []
    for name, r in cands:
        ok, why, pm = _plausibility(plaus_arr, name)
        scored.append((name, r, ok, why, pm))
    plausible = [s for s in scored if s[2]]
    if not plausible:
        why = "；".join(f"{n}:{w}" for n, _, _, w, _ in scored[:2])
        return "", "low", ["没有任何曲线能把画面解释成正常曝光的场景（" + why + "）"], "", metrics

    names = [s[0] for s in plausible]
    rmap = {s[0]: s[1] for s in scored}
    pmet = {s[0]: s[4] for s in scored}
    pris = set(n for n in _prior_profiles(make) if n in names)
    r_min = abs(cands[0][1])
    # 指纹是否「确认」：黑位正好落在某条曲线的基灰上（量化带内），说明画面真的拍到了纯黑。
    # 否则画面里没有纯黑，基灰只能给出下界 —— 曲线并未被像素证据确认。
    contact = [n for n in names if abs(rmap[n]) <= QUANT_TOL]
    confirmed = r_min <= QUANT_TOL and bool(contact)

    def _cost(n):
        m = float(pmet[n].get("decoded_median", MID_TARGET))
        t = float(pmet[n].get("decoded_top99", TOP_TARGET))
        return (abs(math.log2(max(m, 1e-4) / MID_TARGET))
                + abs(math.log2(max(t, 1e-3) / TOP_TARGET)))

    def _key(n):
        is_contact = abs(rmap[n]) <= QUANT_TOL
        return (0 if is_contact else 1,
                0 if n in pris else 1,
                abs(rmap[n]) if is_contact else _cost(n),
                order[n])

    best = min(names, key=_key)
    metrics["plausible_candidates"] = names
    metrics["pedestal_candidates"] = contact
    r0 = rmap[best]
    metrics["pedestal_confirmed"] = bool(confirmed)
    metrics["floor_residual"] = round(r0, 5)

    # 置信度：只有「指纹确认 + 厂牌先验收敛到唯一候选」才算 high。
    # 同族（S-Log3 / LogC3 / LogC4 基灰只差 0.00005）无法靠像素区分时最多 medium，
    # 画面没有纯黑时只能凭场景合理性推断 → low。
    narrowed = ([n for n in contact if n in pris] if pris else contact)
    if confirmed and len(narrowed) == 1:
        conf = "high"
    elif confirmed:
        conf = "medium"
    else:
        conf = "low"

    reasons.append(
        (f"稳健黑位 {ped:.4f} 正落在 {LOG_PROFILES[best]['label']} 的基灰上"
         f"（残差 {r0:+.5f}，量化误差内）→ 基灰指纹确认"
         if confirmed else
         f"稳健黑位 {ped:.4f} 高于**全部**曲线的基灰（最小残差 {r_min:+.4f}）"
         f"→ 画面里没有纯黑，基灰指纹只能给出下界，曲线未被像素证据确认"))
    reasons.append(f"可解释成正常曝光的曲线 {', '.join(names)}"
                   f"（基灰残差 " + ", ".join(f"{n}:{rmap[n]:+.3f}" for n in names) + "）")
    reasons.append(("机厂牌 " + make + " 先验收敛到 " if pris else
                    ("按基灰残差选定 " if confirmed else "基灰不可用，按场景典型度选定 "))
                   + best)
    if confirmed and len(narrowed) > 1:
        reasons.append("同族曲线基灰相差仅 ≤"
                       f"{abs(LOG_PROFILES[narrowed[0]]['floor'] - LOG_PROFILES[narrowed[-1]]['floor']):.5f}，"
                       "像素无法再细分；如需精确请用 --log-profile 指定")
    # 等效性：所选曲线与其余可解释候选渲染差多大
    note = ""
    others = [n for n in names if n != best]
    if others:
        deltas = [(n, _output_delta(sub01, best, n)) for n in others]
        worst = max(deltas, key=lambda t: t[1])
        if worst[1] < DELTA_EQUIV:
            reasons.append(f"与备选 {worst[0]} 渲染均差仅 {worst[1]:.2f}/255（<{DELTA_EQUIV}）"
                           f"→ 选哪条都一样")
            if confirmed:
                conf = "high" if len(narrowed) == 1 else conf
        else:
            note = (f"备选 {worst[0]} 的渲染均差 {worst[1]:.2f}/255（肉眼可见）；"
                    f"已按"
                    + (f"机厂牌 {make} 先验" if pris else
                       ("基灰残差" if confirmed else "场景典型度"))
                    + f"选定 {best}。如知实际曲线请用 --log-profile 指定")
    rel = float(pmet[best].get("decoded_median", 0.0))
    reasons.append(f"选定 `{best}`：基灰残差 {r0:+.5f}，解码后中位反射率 {rel:.3f}")
    return best, conf, reasons, note, metrics


# ══════════════════════════════════════════════════════════ 探测
@dataclass
class SourceInfo:
    path: str = ""
    name: str = ""
    kind: str = "standard"        # raw | log | flat | standard | unsupported
    action: str = ""
    score: int = 0
    reasons: list = field(default_factory=list)
    profile: str = ""
    profile_label: str = ""
    confidence: str = "-"
    is_phone: bool = False
    make: str = ""
    model: str = ""
    size: list = field(default_factory=list)
    orientation: int = 1
    metrics: dict = field(default_factory=dict)
    normalize: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)
    note: str = ""

    def to_dict(self):
        return asdict(self)


def _exif_of(im):
    try:
        ex = im.getexif()
        return (str(ex.get(0x010F, "") or "").strip(),
                str(ex.get(0x0110, "") or "").strip())
    except Exception:
        return "", ""


def _is_phone(make, model):
    s = f"{make} {model}".lower()
    return any(k in s for k in PHONE_MAKES)


def probe(path, thumbnail=760):
    """只读探测：判断文件的色彩格式，不做任何转换。返回 SourceInfo"""
    info = SourceInfo(path=path, name=os.path.basename(path))
    ext = os.path.splitext(path)[1].lower()

    if ext in RAW_EXTS:
        info.kind, info.confidence = "raw", "high"
        info.action = "显影为 sRGB（相机白平衡 / 不自动提亮 / 不反 log）"
        info.reasons = [f"扩展名 {ext} 属 RAW 容器"]
        try:
            import rawpy
            with rawpy.imread(path) as raw:
                s = raw.sizes
                rot = int(s.flip) in (5, 6, 7, 8)
                # rawpy 的 sizes.width/height 是**传感器**方向；90° 翻转时显示尺寸要交换宽高
                info.size = ([int(s.height), int(s.width)] if rot
                             else [int(s.width), int(s.height)])
                info.orientation = int(s.flip)
                info.model = "RAW"
                info.raw = {
                    "camera_wb": [float(v) for v in raw.camera_whitebalance],
                    "black_level": [int(v) for v in raw.black_level_per_channel],
                    "white_level": int(raw.white_level),
                    "flip": int(s.flip),
                    "sensor": [int(s.raw_width), int(s.raw_height)],
                }
            info.reasons.append(
                "RAW 是传感器场景线性数据，机内 Log/风格不写入 RAW → 只需显影，不反 log")
        except ImportError:
            info.kind, info.confidence = "unsupported", "low"
            info.action = "无法处理：缺 rawpy（pip install rawpy）"
        except Exception as e:
            info.kind, info.confidence = "unsupported", "low"
            info.action = f"无法读取：{e}"
        return info

    try:
        im = Image.open(path)
    except Exception as e:
        info.kind, info.confidence = "unsupported", "low"
        info.action = f"Pillow 无法打开（HEIC 等需插件）：{e}"
        return info

    info.make, info.model = _exif_of(im)
    info.is_phone = _is_phone(info.make, info.model)
    info.size = [int(im.size[0]), int(im.size[1])]
    try:
        info.orientation = int(im.getexif().get(274, 1) or 1)
    except Exception:
        info.orientation = 1

    full = ImageOps.exif_transpose(im).convert("RGB")
    thumb = _thumb(full, thumbnail)
    arr = np.asarray(thumb, dtype=np.uint8)
    f = _features(arr)
    info.metrics = {k: round(v, 4) for k, v in f.items()}

    score, hits = _score_flat(f)
    info.score, info.reasons = score, hits
    thresh = SCORE_LOG_PHONE if info.is_phone else SCORE_LOG_CAMERA
    sig_ok, sig_why = _flat_signature(f)
    info.metrics["flat_signature"] = bool(sig_ok)

    if not sig_ok:
        # 必要条件不成立：画面同时拥有真黑与真白 → 不是 log/flat 编码。
        # 这一步专门保护「低对比雾天照 / 逆光大光比照」——它们黑位抬升、饱和偏低、
        # 深暗部稀少，能骗过六项计分，但动态跨度与高光余量同时暴露了正常成片的身份。
        info.reasons = hits + [sig_why]
        info.kind = "suspect" if score >= thresh else "standard"
        info.confidence = "high"
        info.action = "跳过预处理：已是完整显影的成片"
        if info.kind == "suspect":
            info.note = (f"平坦特征命中 {score}/6 项，但{sig_why}；"
                         f"按普通成片处理。如确为 Log 请用 --source log 强制")
        return info

    if score < thresh:
        info.kind = "standard"
        info.confidence = "high" if score <= thresh - 2 else "medium"
        info.action = "跳过预处理：已是完整显影的成片" + ("（手机拍摄）" if info.is_phone else "")
        if info.is_phone and score >= thresh - 1:
            info.note = "已接近灰片门槛；确有 Log 拍摄请加 --source log"
        return info

    sub = np.asarray(_thumb(full, 128), np.float32) / 255.0
    cands = _candidates(f)
    prof, conf, reasons, note, pm = _select_profile(f, sub, arr, info.make, cands)
    info.reasons = hits + reasons
    info.note = note
    info.metrics.update(pm)
    if not prof:
        # 灰片签名成立，但没有任何曲线能把画面解释成正常曝光的场景：
        # 要么是未知的 log 变体/机内 flat 风格，要么曝光极端（死黑或过曝）。
        # 此时**不能**退回到「按机厂牌猜一条曲线」—— 厂牌只是先验，没有像素证据支撑，
        # 猜错曲线反 log 的破坏远大于通用还原。所以走通用扁平还原，并明确标注。
        if score >= SCORE_LOG_CAMERA + 1:
            # 曲线未知时门槛再抬高 1 分：通用还原的把握本就比精确曲线弱
            info.kind, info.confidence = "flat", "low"
            info.action = "通用扁平还原（未知曲线：黑/白点拉伸 + 中灰重定位 + 饱和度恢复）"
            ped = f.get("pedestal", f["floor"])
            hint = (f"稳健黑位 {ped:.4f} 与最贴近曲线的基灰差 "
                    f"{ped - LOG_PROFILES[cands[0][0]]['floor']:+.4f}"
                    if cands else f"稳健黑位 {ped:.4f} 低于全部已知曲线的基灰")
            info.reasons.append(
                f"灰片签名成立且平坦特征达 {score} 项，但无已知 log 曲线能解释成正常曝光"
                f"（{hint}）→ 按未知扁平风格通用还原")
            if info.is_phone:
                info.reasons.append("手机拍摄：门槛已抬高 1 分，仍达标 → 按灰片处理")
            return info
        else:
            info.kind, info.confidence = "suspect", "low"
            info.action = ("保守跳过预处理：画面像灰片，但曲线未知且平坦特征不足；"
                           "如确认是 Log，用 --source log 强制还原")
            info.reasons.append(f"曲线未知且平坦特征仅 {score} 项，不足以判定为灰片")
            if info.is_phone:
                info.reasons.append("手机拍摄：门槛已抬高 1 分，未达标")
            return info

    confirmed = bool(pm.get("pedestal_confirmed"))
    if confirmed:
        info.kind, info.confidence, info.profile = "log", conf, prof
        info.profile_label = LOG_PROFILES[prof]["label"]
        info.action = f"反 {info.profile_label} → 线性反射率 → 中性 sRGB"
        if info.is_phone:
            info.reasons.append("手机拍摄：门槛已抬高 1 分，仍达标 → 按灰片处理")
        return info

    # 基灰未被确认（画面没有纯黑）：像素证据只能给出下界，曲线是**推断**而非确认。
    # 单张 8bit 图无法在数学上区分「log 编码的暗场景」与「正常显影但扁平的照片」，
    # 所以只有在扁平特征足够强时才动手，否则保守跳过 —— 把正常照片当灰片还原会毁图，
    # 漏掉一张真 Log 只是少还原一步（可用 --source log 强制）。
    if score >= SCORE_LOG_CAMERA + 1:
        info.kind, info.confidence, info.profile = "log", "low", prof
        info.profile_label = LOG_PROFILES[prof]["label"]
        info.action = (f"反 {info.profile_label} → 线性反射率 → 中性 sRGB"
                       f"（基灰未确认，按扁平特征 {score}/6 + 场景合理性推断）")
        info.note = (f"画面没有纯黑，基灰指纹只能给出下界，{prof} 是推断结果；"
                     f"如知实际曲线请用 --log-profile 指定")
    else:
        info.kind, info.confidence = "suspect", "low"
        info.profile, info.profile_label = prof, LOG_PROFILES[prof]["label"]
        info.action = ("保守跳过预处理：画面像灰片，但没有纯黑可确认曲线，"
                       "扁平特征也不足；如确认是 Log，用 --source log --log-profile "
                       f"{prof} 强制还原")
        info.note = (f"像素证据不足：{info.reasons[-3] if len(info.reasons) >= 3 else ''}"
                     f" 最可能的曲线是 {prof}，但无法确认")
    if info.is_phone:
        info.reasons.append("手机拍摄：门槛已抬高 1 分，仍达标 → 按灰片处理")
    return info


# ══════════════════════════════════════════════════════════ 显影与还原
def _thumb(im, n):
    if max(im.size) <= n:
        return im
    s = n / max(im.size)
    return im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))), Image.LANCZOS)


def _sibling_jpeg(raw_path):
    stem = os.path.splitext(raw_path)[0]
    for e in (".JPG", ".jpg", ".JPEG", ".jpeg", ".TIF", ".tif", ".PNG", ".png"):
        p = stem + e
        if os.path.exists(p):
            return p
    return None


def _match_gain(lin, ref_path, n=760):
    """用同目录参考成片的线性中位亮度对齐 RAW 曝光 → 单一线性增益"""
    try:
        r = ImageOps.exif_transpose(Image.open(ref_path)).convert("RGB")
        r = _thumb(r, n)
        rl = (srgb_to_linear(np.asarray(r, np.float32) / 255.0) @ LUMA).astype(np.float32)
        src = lin
        k = max(1, int(max(src.shape[:2]) / n))
        src = src[::k, ::k]
        a = float(np.median((src @ LUMA).astype(np.float32)))
        b = float(np.median(rl))
        if a <= 1e-5 or b <= 1e-5:
            return None
        return float(np.clip(b / a, 0.05, 32.0))
    except Exception:
        return None



def _tone_transfer_lut(lin, ref_lin, n=48):
    """由参考成片反推色调映射：把 lin 的分位点映射到参考成片的分位点。

    在这里的用途是 `--raw-anchor reference-tone`：显影时带上相机自己的色调形状
    （而不是只对齐亮度）。得到的是单调折线 LUT，按通道套用。
    """
    qs = np.linspace(0.2, 99.8, n)
    a = np.percentile((lin @ LUMA).astype(np.float32), qs).astype(np.float64)
    b = np.percentile((ref_lin @ LUMA).astype(np.float32), qs).astype(np.float64)
    a = np.maximum.accumulate(a)
    b = np.maximum.accumulate(b)
    ka = np.concatenate([[0.0], a, [max(a[-1] * 4.0, 1.0)]])
    kb = np.concatenate([[0.0], b, [max(b[-1] * 1.6, b[-1] + 0.05)]])
    return ka.astype(np.float32), kb.astype(np.float32)


def develop_raw(path, anchor="reference-tone", denoise="off", exposure=0.0,
                knee=DEFAULT_KNEE, knee_k=DEFAULT_KNEE_K):
    """RAW → 线性 → 中性 sRGB。返回 (uint8 arr, meta)

    anchor='reference-tone'（默认）对齐同目录同名成片的中位亮度，再按亮度套上参考成片
                            的色调形状；近白高光场景需预览确认，异常时改 reference
    anchor='reference'      只对齐参考成片的中位亮度，不套色调形状（更「中性」，但高光偏平）
    anchor='auto'           无参考成片时把 99.5% 分位锚定到 sRGB 0.95（上限 +4 EV）
    anchor='none'           完全不动曝光
    显影固定：相机白平衡、不自动提亮、sRGB 色彩空间、16bit 内部精度。
    """
    import rawpy
    # 后续曝光锚定、参考成片匹配与 render_neutral 都按线性反射率计算。
    # rawpy 默认 gamma=(2.222, 4.5) 会先编码 Rec.709；这里必须显式取线性输出。
    params = dict(use_camera_wb=True, no_auto_bright=True, output_bps=16,
                  gamma=(1, 1),
                  output_color=rawpy.ColorSpace.sRGB)
    if denoise in ("light", "full"):
        params["fbdd_noise_reduction"] = (rawpy.FBDDNoiseReductionMode.Light
                                          if denoise == "light"
                                          else rawpy.FBDDNoiseReductionMode.Full)
    with rawpy.imread(path) as raw:
        arr16 = raw.postprocess(**params).astype(np.float32)
        flip = int(raw.sizes.flip)
    lin = arr16 / 65535.0
    del arr16
    # 注意：rawpy.postprocess 已经按相机方向旋转过（user_flip 默认 -1 = 用相机值）。
    # 实测 _DSC5690.ARW（flip=6）输出 (6024,4024,3) → PIL(4024,6024) 竖幅，
    # 与相机 JPEG 摆正后的 (4000,6000) 一致 —— 再自己转一次就横过来了。

    ref_path = _sibling_jpeg(path)
    note, gain, tone = "", 1.0, False
    if anchor in ("reference", "reference-tone"):
        g = _match_gain(lin, ref_path) if ref_path else None
        if g:
            gain = g
            note = f"曝光锚定 {os.path.basename(ref_path)} 的线性中位亮度"
        else:
            anchor = "auto"
            note = "无同参考成片，退回自动锚定"
    if anchor == "auto":
        lum = (lin @ LUMA).astype(np.float32)
        p = float(np.percentile(lum, 99.5))
        if p > 1e-4:
            gain = min(float(srgb_to_linear(np.float32(0.95))) / p, 16.0)
            note = (note + "；" if note else "") + f"自动锚定 ×{gain:.3f}"
        else:
            note = (note + "；" if note else "") + "画面近乎全黑，未做曝光锚定"

    if gain != 1.0:
        lin = lin * gain
    if anchor == "reference-tone" and ref_path:
        try:
            r = _thumb(ImageOps.exif_transpose(Image.open(ref_path)).convert("RGB"), 760)
            rl = srgb_to_linear(np.asarray(r, np.float32) / 255.0)
            ka, kb = _tone_transfer_lut(lin, rl)
            # 分位曲线由亮度拟合，只作用到亮度；同一像素的 RGB 使用同一增益。
            # 逐通道 np.interp 会放大高光里微小的通道差异，在近白天空形成青紫色带。
            for y0 in range(0, lin.shape[0], 512):
                band = lin[y0:y0 + 512]
                lum = (band @ LUMA).astype(np.float32)
                mapped = np.interp(np.clip(lum, 0.0, float(ka[-1])), ka, kb).astype(np.float32)
                scale = np.divide(mapped, lum, out=np.zeros_like(lum), where=lum > 1e-6)
                band *= scale[..., None]
            tone = True
            note += "；叠加参考成片的色调形状（reference-tone）"
        except Exception as e:
            note += f"；色调迁移失败（{e}）"
    out = render_neutral(lin, exposure=exposure + (0.0 if tone else 0.0),
                         knee=(1.0 if tone else knee), knee_k=knee_k)
    meta = {"gain": round(gain, 4), "ev": round(math.log2(gain) + exposure, 3),
            "anchor": anchor, "note": note, "flip": flip, "wb": "camera",
            "denoise": denoise, "tone_transfer": tone,
            "shoulder": [1.0 if tone else knee, knee_k]}
    return (out * 255.0 + 0.5).astype(np.uint8), meta


def restore_log(arr_u8, profile, exposure=0.0, knee=DEFAULT_KNEE, knee_k=DEFAULT_KNEE_K):
    p = LOG_PROFILES[profile]
    v = arr_u8.astype(np.float32) / 255.0
    lin = np.clip(p["dec"](v), 0.0, 64.0)
    out = render_neutral(lin, exposure=exposure, knee=knee, knee_k=knee_k)
    return (out * 255.0 + 0.5).astype(np.uint8), {"profile": profile,
                                                  "source": p["source"],
                                                  "shoulder": [knee, knee_k]}


def restore_flat(arr_u8, f, exposure=0.0, sat_target=0.52,
                 knee=DEFAULT_KNEE, knee_k=DEFAULT_KNEE_K):
    """未知扁平曲线的兜底还原。所有参数回传，便于复核与复现。"""
    x = arr_u8.astype(np.float32) / 255.0
    lo, hi = float(f["floor"]), float(f["top"])
    if hi - lo < 1e-3:
        lo, hi = 0.0, 1.0
    y = np.clip((x - lo) / (hi - lo), 0.0, 1.0)
    med = float(np.median((y @ LUMA).astype(np.float32)))
    gamma = float(np.clip(math.log(0.46) / math.log(max(med, 1e-3)), 0.45, 2.4)) if med > 1e-3 else 1.0
    if abs(gamma - 1.0) > 0.02:
        y = np.power(y, gamma)
    mx, mn = y.max(2), y.min(2)
    sat = float(np.percentile(np.where(mx > 1e-6, (mx - mn) / np.maximum(mx, 1e-6), 0.0), 90))
    sat_mul = float(np.clip(sat_target / max(sat, 0.05), 1.0, 2.4))
    g = (y @ LUMA).astype(np.float32)[..., None]
    y = np.clip(g + (y - g) * sat_mul, 0.0, 1.0)
    out = render_neutral(y, exposure=exposure, knee=knee, knee_k=knee_k)
    meta = {"black_anchor": round(lo, 4), "white_anchor": round(hi, 4),
            "gamma": round(gamma, 3), "sat_measured": round(sat, 3),
            "sat_multiplier": round(sat_mul, 3), "shoulder": [knee, knee_k],
            "assumption": "假设画面内存在接近纯黑与接近纯白的像素（未知曲线的兜底）"}
    return (out * 255.0 + 0.5).astype(np.uint8), meta


# ══════════════════════════════════════════════════════════ 统一入口
def normalize(path, info=None, source="auto", log_profile="auto", raw_anchor="reference-tone",
              raw_denoise="off", exposure=0.0, knee=DEFAULT_KNEE, knee_k=DEFAULT_KNEE_K,
              max_edge=None, keep_exif=True):
    """把任意输入还原成中性 sRGB。返回 (PIL.Image | None, SourceInfo)"""
    if info is None:
        info = probe(path)
    if source in ("standard", "off"):
        info.kind, info.action = "standard", "跳过预处理（命令行强制）"
    elif source == "log" and info.kind in ("standard", "unsupported", "suspect", "flat"):
        info.kind = "log" if (info.profile or (log_profile and log_profile != "auto")) else "flat"
        info.action = "命令行强制按灰片还原"

    img, detail = None, {}
    if info.kind == "raw":
        try:
            arr, detail = develop_raw(path, anchor=raw_anchor, denoise=raw_denoise,
                                      exposure=exposure, knee=knee, knee_k=knee_k)
            img = Image.fromarray(arr, "RGB")
            info.note = detail.get("note", "") or info.note
            info.action = f"显影为 sRGB（{detail.get('note') or '相机白平衡 / 不自动提亮 / 不反 log'}）"
        except Exception as e:
            info.kind, info.confidence = "unsupported", "low"
            info.action = f"显影失败：{e}"
    else:
        try:
            img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
        except Exception as e:
            info.kind, info.confidence = "unsupported", "low"
            info.action = f"读取失败：{e}"
            return None, info
        if info.kind == "log":
            prof = log_profile if (log_profile and log_profile != "auto") else info.profile
            if prof and prof in LOG_PROFILES:
                arr, detail = restore_log(np.asarray(img, np.uint8), prof,
                                          exposure=exposure, knee=knee, knee_k=knee_k)
                info.profile, info.profile_label = prof, LOG_PROFILES[prof]["label"]
                info.action = f"反 {info.profile_label} → 线性反射率 → 中性 sRGB"
                img = Image.fromarray(arr, "RGB")
            else:
                info.kind = "flat"
        if info.kind == "flat":
            f = info.metrics or {k: float(v) for k, v in
                                 _features(np.asarray(_thumb(img, 760), np.uint8)).items()}
            for key in ("floor", "top"):
                f.setdefault(key, _features(np.asarray(_thumb(img, 760), np.uint8))[key])
            arr, detail = restore_flat(np.asarray(img, np.uint8), f, exposure=exposure,
                                       knee=knee, knee_k=knee_k)
            img = Image.fromarray(arr, "RGB")
            info.action = (f"通用扁平还原（拉伸 {detail['black_anchor']}→{detail['white_anchor']}"
                           f"，γ {detail['gamma']}，饱和 ×{detail['sat_multiplier']}）")

    if img is not None:
        if max_edge and max(img.size) > max_edge:
            r = max_edge / max(img.size)
            img = img.resize((max(1, int(img.width * r)), max(1, int(img.height * r))),
                             Image.LANCZOS)
        if keep_exif:
            src = path if info.kind != "raw" else _sibling_jpeg(path)
            if src:
                try:
                    ex = Image.open(src).getexif()
                    ex[274] = 1
                    img.info["exif"] = ex.tobytes()
                except Exception:
                    pass
    info.normalize = {k: (round(v, 4) if isinstance(v, float) else v)
                      for k, v in detail.items()}
    return img, info


# ══════════════════════════════════════════════════════════ 自检
ANCHORS = [
    # (profile, 线性反射率, 期望码值, 依据)
    ("s_log3", 0.18, 420 / 1023, "Sony 白皮书 10bit：18% 灰 = 420"),
    ("s_log3", 0.90, 598 / 1023, "Sony 白皮书 10bit：90% 白 = 598"),
    ("s_log3", 0.00, 95 / 1023, "Sony 白皮书 10bit：0 = 95（黑位）"),
    ("v_log", 0.18, 0.4232, "Panasonic 官方：18% 灰 ≈ 42% IRE"),
    ("v_log", 0.00, 0.1250, "Panasonic 官方：0 → 7.3% IRE 黑位"),
    ("logc3", 0.18, 0.3910068, "ARRI 官方样例 log_encoding_ARRILogC3(0.18)=0.3910068"),
    ("logc4", 0.18, 0.2783958, "ARRI 官方样例 log_encoding_ARRILogC4(0.18)=0.2783958"),
    ("apple_log", 0.18, 0.4882724, "Apple 白皮书样例 log_encoding_AppleLogProfile(0.18)=0.4882724"),
    ("s_log2", 0.18, 0.38495, "S-Log2 18% 灰 ≈ 38.5 IRE（Sony OETF 实算）"),
]


def _synth_scene(h=240, w=360, seed=7, black=True, lift=0.0):
    """合成一个「像照片」的场景线性图：对数正态亮度分布 + 局部深阴影/高光。

    线性斜坡（uniform）不像真实画面，会让判定试验失真 —— 真实场景的亮度近似
    对数正态，且有明确的暗部与高光两端。
    """
    rng = np.random.default_rng(seed)
    lum = np.exp(rng.normal(np.log(0.10), 1.15, size=(h, w))).astype(np.float32)
    # 加两处结构：一片深阴影、一片高光
    lum[: h // 5, : w // 4] *= 0.05
    lum[-h // 4:, -w // 4:] *= 6.0
    if black:
        lum[: h // 12, : w // 12] = 0.0          # 真正的纯黑区 → 基灰指纹会显现
    lum = np.clip(lum + lift, 0.0, 16.0)
    # 造一点色偏，让饱和度统计有意义
    scene = np.stack([lum * 1.05, lum, lum * 0.85], -1).astype(np.float32)
    return np.clip(scene, 0.0, 16.0)


def selftest(verbose=True, crosscheck=False):
    ok = True
    lines = []

    def chk(cond, msg):
        nonlocal ok
        ok = ok and bool(cond)
        lines.append(("  OK  " if cond else "  FAIL") + "  " + msg)

    # 1) 官方锚点
    for name, lin, want, why in ANCHORS:
        got = float(np.asarray(LOG_PROFILES[name]["enc"](lin)).reshape(-1)[0])
        d = abs(got - want)
        chk(d < 0.0025, f"{name:<10} 线性 {lin:<5} → {got:.5f}（期望 {want:.5f}，差 {d:.5f}）｜{why}")

    # 2) enc/dec 互为逆
    for name, p in LOG_PROFILES.items():
        L = np.linspace(0.0, 4.0, 40001).astype(np.float64)
        rt = p["dec"](p["enc"](L))
        err = float(np.max(np.abs(rt - L)))
        chk(err < 3e-4, f"{name:<10} 往返最大误差 {err:.2e}（线性 0→4.0，4 万点）")

    # 2b) 与 colour-science 独立实现交叉核对（装了才跑；仓库本身不依赖它）
    if crosscheck:
        try:
            from colour.models.rgb import transfer_functions as _tf
            # 注意：S-Log2 要与 colour-science 的 log_encoding_SLog 比对（码值约定，
            # 官方 10bit 全范围锚点 0/18%/90% → 90/394/636），
            # 而不是 log_encoding_SLog2（那是 219/155 归一化约定，数值不同）。
            pairs = [("s_log3", _tf.log_encoding_SLog3, _tf.log_decoding_SLog3),
                     ("s_log2", _tf.log_encoding_SLog, _tf.log_decoding_SLog),
                     ("v_log", _tf.log_encoding_VLog, _tf.log_decoding_VLog),
                     ("logc3", _tf.log_encoding_ARRILogC3, _tf.log_decoding_ARRILogC3),
                     ("logc4", _tf.log_encoding_ARRILogC4, _tf.log_decoding_ARRILogC4),
                     ("apple_log", _tf.log_encoding_AppleLogProfile,
                      _tf.log_decoding_AppleLogProfile)]
            L = np.array([0.0, 0.005, 0.018, 0.05, 0.18, 0.5, 1.0, 2.0])
            for name, ref_enc, ref_dec in pairs:
                mine = np.asarray(LOG_PROFILES[name]["enc"](L), np.float64)
                theirs = np.asarray(ref_enc(L), np.float64)
                d = float(np.max(np.abs(mine - theirs)))
                rt = float(np.max(np.abs(np.asarray(LOG_PROFILES[name]["dec"](mine), np.float64) - L)))
                chk(d < 5e-4 and rt < 5e-4,
                    f"{name:<10} 与 colour-science 0.4.7 独立实现最大差 {d:.2e}")
        except Exception as e:
            lines.append(f"  SKIP  未做 colour-science 交叉核对（{type(e).__name__}: {e}）")

    # 3) 有纯黑的合成灰片 → 曲线应被锁定到「同基灰族」内
    #    （基灰完全相同的曲线 —— 如 S-Log3 / LogC3 / LogC4 都锚在 95/1023 ——
    #      单张图不可能再区分，只能靠 EXIF 机厂牌或用户指定，故只要求落进族内）
    scene = _synth_scene(black=True)
    for name, p in LOG_PROFILES.items():
        enc8 = (np.clip(p["enc"](scene), 0, 1) * 255 + 0.5).astype(np.uint8)
        f = _features(enc8)
        sub = enc8[::2, ::3].astype(np.float32) / 255.0
        picked, conf, _, _, pm = _select_profile(f, sub, enc8, "", _candidates(f))
        family = [n for n, q in LOG_PROFILES.items()
                  if abs(q["floor"] - p["floor"]) <= QUANT_TOL]
        chk(picked in family and pm.get("pedestal_confirmed"),
            f"合成 {name:<10}（含纯黑）→ 锁定 {picked or 'flat':<10} 置信 {conf:<6}"
            f"｜同族 {','.join(family)}｜基灰 {f['floor']:.4f} vs {p['floor']:.4f}")

    # 3b) 同族用机厂牌先验收敛（Sony → S-Log3，ARRI → LogC3）
    for make, truth, want in (("SONY", "s_log3", "s_log3"), ("ARRI", "logc4", "logc4")):
        enc8 = (np.clip(LOG_PROFILES[truth]["enc"](scene), 0, 1) * 255 + 0.5).astype(np.uint8)
        f = _features(enc8)
        sub = enc8[::2, ::3].astype(np.float32) / 255.0
        picked, conf, why, _, _ = _select_profile(f, sub, enc8, make, _candidates(f))
        chk(picked == want, f"厂牌 {make:<5} 的真 {truth:<10} → 判定 {picked or 'flat':<10}"
                            f"（{'厂牌先验已用' if any('厂牌' in r for r in why) else '按中灰先验'}）")

    # 4) 合成灰片必须被判为「需要还原」
    for name, p in LOG_PROFILES.items():
        enc8 = (np.clip(p["enc"](scene), 0, 1) * 255 + 0.5).astype(np.uint8)
        f = _features(enc8)
        s, _ = _score_flat(f)
        chk(s >= SCORE_LOG_CAMERA, f"合成 {name:<10} 灰片得分 {s} ≥ {SCORE_LOG_CAMERA}")

    # 5) 反 log 往返精度（不截断，只验数学正确性；截断会引入与曲线无关的误差）
    sub01 = scene[::2, ::3]
    for name, p in LOG_PROFILES.items():
        enc = p["enc"](sub01)                       # 不 clip：亮于 90% 白的反射率 >1
        back_lin = np.clip(p["dec"](enc), 0, 64)
        ref_lin = np.clip(sub01, 0, 64)
        err = float(np.abs(back_lin - ref_lin).mean() / max(float(ref_lin.mean()), 1e-6))
        chk(err < 1e-5, f"{name:<10} 反 log 往返相对误差 {err:.2e}（无量化、无截断）")

    # 6) 正常成片不得误判为灰片
    normal = np.clip(_synth_scene(black=True, seed=11) * 1.0, 0, 1)
    normal = (sn_like_gamma(normal) * 255).astype(np.uint8)
    f = _features(normal)
    s, _ = _score_flat(f)
    chk(s < SCORE_LOG_CAMERA, f"常规成片（线性→sRGB 正常显影）得分 {s} < {SCORE_LOG_CAMERA}"
                              f"（floor={f['floor']:.3f} span={f['span']:.3f}）")

    # 7) 无纯黑的高调灰片：不得硬报一个具体曲线，但必须仍被判为需要还原
    hi = _synth_scene(black=False, lift=0.06, seed=3)
    enc8 = (np.clip(LOG_PROFILES["s_log3"]["enc"](hi), 0, 1) * 255 + 0.5).astype(np.uint8)
    f = _features(enc8)
    s, _ = _score_flat(f)
    sub = enc8[::2, ::3].astype(np.float32) / 255.0
    picked, conf, why, _, _ = _select_profile(f, sub, enc8, "", _candidates(f))
    chk(s >= SCORE_LOG_CAMERA and (picked == "" or conf != "high"),
        f"高调灰片（无纯黑）得分 {s} ≥ {SCORE_LOG_CAMERA}，"
        f"曲线判定 {picked or '未知'}（置信 {conf}，不应为 high）")

    # 8) 软肩无折点、knee 以下恒等
    y = np.linspace(0, 3, 60001, dtype=np.float64)
    d = np.diff(soft_shoulder(y)) / np.diff(y)
    jump = float(np.max(np.abs(np.diff(d))[np.abs(y[:-2] - DEFAULT_KNEE) < 0.01]))
    chk(jump < 0.02, f"软肩一阶导在 knee={DEFAULT_KNEE} 处跳变 {jump:.4f}（应≈0）")
    chk(abs(float(soft_shoulder(np.float32(0.5))) - 0.5) < 1e-6, "软肩在 knee 以下恒等")
    chk(abs(float(soft_shoulder(np.float32(4.0))) - 1.0) < 0.01,
        f"软肩把 +4 档高光压到 {float(soft_shoulder(np.float32(4.0))):.4f}（≤1）")

    if verbose:
        print("═══ source_normalize 自检 ═══")
        for l in lines:
            print(l)
        n_ok = sum(1 for l in lines if l.startswith("  OK"))
        print(f"—— {n_ok}/{len(lines)} 项通过" + ("" if ok else "，存在失败项"))
    return ok, lines


def sn_like_gamma(x):
    """把线性场景按标准 sRGB 编码成「正常成片」用于负样本"""
    return np.clip(linear_to_srgb(np.clip(x, 0, 1)), 0, 1)


# ══════════════════════════════════════════════════════════ CLI
def collect(inputs, pattern=None, limit=None):
    out = []
    rx = re.compile(pattern, re.I) if pattern else None
    for it in inputs:
        if os.path.isdir(it):
            for p in sorted(glob.glob(os.path.join(it, "*"))):
                if os.path.isfile(p) and (rx is None or rx.search(os.path.basename(p))):
                    out.append(p)
        else:
            out += glob.glob(it)
    return out[:limit] if limit else out


def main(argv=None):
    ap = argparse.ArgumentParser(description="源归一化：先判断色彩格式，再还原为中性 sRGB")
    ap.add_argument("inputs", nargs="*", help="文件或目录")
    ap.add_argument("-o", "--out", default=None, help="输出目录（给出则写出还原后的图）")
    ap.add_argument("--pattern", default=None, help="文件名正则过滤")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--source", default="auto", choices=["auto", "raw", "log", "standard", "off"])
    ap.add_argument("--log-profile", default="auto",
                    help="强制 log 曲线：" + ", ".join(LOG_PROFILES) + " 或 auto")
    ap.add_argument("--raw-anchor", default="reference-tone",
                    choices=["reference-tone", "reference", "auto", "none"])
    ap.add_argument("--raw-denoise", default="off", choices=["off", "light", "full"])
    ap.add_argument("--exposure", type=float, default=0.0, help="还原后追加 EV")
    ap.add_argument("--knee", type=float, default=DEFAULT_KNEE, help="高光软肩起点（线性反射率）")
    ap.add_argument("--knee-k", type=float, default=DEFAULT_KNEE_K, help="软肩压缩强度")
    ap.add_argument("--max-edge", type=int, default=None)
    ap.add_argument("--quality", type=int, default=95)
    ap.add_argument("--json", default=None, help="判定与还原参数写入 JSON")
    ap.add_argument("--list-profiles", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--crosscheck", action="store_true",
                    help="自检时额外与 colour-science 独立实现交叉核对（需已安装）")
    args = ap.parse_args(argv)

    if args.list_profiles:
        print(f"{'profile':<11}{'label':<38}{'黑位':>9}{'18%灰':>9}{'90%白':>9}")
        print("-" * 108)
        for k, p in LOG_PROFILES.items():
            print(f"{k:<11}{p['label']:<38}{p['floor']:>9.5f}{p['mid']:>9.5f}{p['white']:>9.5f}")
            print(f"{'':<11}{p['source']}")
        return 0

    if args.selftest:
        ok, _ = selftest(crosscheck=args.crosscheck)
        return 0 if ok else 1

    files = collect(args.inputs or ["."], args.pattern, args.limit)
    if not files:
        print("没有匹配的输入", file=sys.stderr)
        return 1

    TAG = {"log": "灰片", "flat": "扁平", "suspect": "疑似", "raw": "RAW",
           "standard": "成片", "unsupported": "不支持"}
    print(f"{'文件':<26}{'判定':<8}{'置信':<8}处理")
    print("-" * 108)
    reports = []
    for p in files:
        info = probe(p)
        print(f"{info.name:<26}{TAG.get(info.kind, info.kind):<8}{info.confidence:<8}{info.action}")
        for r in info.reasons:
            print(f"{'':<42}· {r}")
        if info.kind == "standard" and not info.reasons:
            print(f"{'':<42}· 平坦特征 {info.score}/6 项，未达门槛")
        if info.note:
            print(f"{'':<42}! {info.note}")
        reports.append(info)

    final = list(reports)
    if args.out:
        os.makedirs(args.out, exist_ok=True)
        print()
        for idx, info in enumerate(reports):
            img, info2 = normalize(info.path, info=info, source=args.source,
                                   log_profile=args.log_profile, raw_anchor=args.raw_anchor,
                                   raw_denoise=args.raw_denoise, exposure=args.exposure,
                                   knee=args.knee, knee_k=args.knee_k, max_edge=args.max_edge)
            final[idx] = info2
            if img is None:
                print("  跳过", info.name, "—", info.action)
                continue
            dst = os.path.join(args.out, os.path.splitext(info.name)[0] + ".jpg")
            kw = {"quality": args.quality, "subsampling": 0, "optimize": True}
            if "exif" in img.info:
                img.save(dst, exif=img.info["exif"], **kw)
            else:
                img.save(dst, **kw)
            print(f"  写出 {dst}  {img.size}")
    reports = [i.to_dict() for i in final]

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(reports, fh, ensure_ascii=False, indent=2)
        print("JSON →", args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
