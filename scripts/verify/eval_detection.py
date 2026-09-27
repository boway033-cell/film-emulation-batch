# -*- coding: utf-8 -*-
"""eval_detection.py — 灰片判定规则的验收测试（混淆矩阵）

走完整 probe()（真实代码路径），四组真值已知的素材：

  A 灰片·含纯黑   → 期望 kind == log，且锁进**同基灰族**（曲线精度另计）
  B 灰片·无纯黑   → 期望 kind ∈ {log, suspect}，**绝不能是 standard**
                    （画面没有纯黑时基灰只能给出下界，曲线属推断，允许保守跳过）
  C 普通成片      → 期望 kind ∈ {standard, suspect}，**绝不能是 log/flat**
                    （那会触发还原，把正常照片改坏 —— 这是最不可接受的错误）
  D RAW           → 期望 kind == raw（只需显影，绝不反 log）

A/B 的正样本用**真实照片的线性值**作场景源（反 sRGB 到线性），并叠加
**按 log 编码后码值域的传感器噪声** —— 真实 log 素材的暗部噪声正是这样被抬到基灰附近的，
不叠噪声的合成样本会高估判定能力。指定 --normal-dir 用真实相机 JPEG，否则退回合成场景。

用法：
  python eval_detection.py <普通成片目录> [--noise 1.5] [--raw 目录里的ARW]
  python eval_detection.py
"""

import argparse
import glob
import os
import re
import sys
import tempfile

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import source_normalize as sn  # noqa: E402

RAW_RX = re.compile(r"\.(?:arw|cr2|cr3|nef|dng|raf|orf|rw2|pef|srw)$", re.I)


def encode(arr_lin, profile, noise=0.0, seed=0):
    """线性场景 → 8bit 码值；noise 以「码值/255」为单位叠加高斯噪声。

    log 编码把传感器噪声在码值域近似拉平，所以噪声加在编码之后才符合实拍。
    量化必须**四舍五入**而非截断：截断会使整幅图系统性偏低 0.5 LSB（≈0.002），
    足以吞掉 S-Log3 / LogC3 / LogC4 之间 0.00005 的基灰差，让判定测试失去意义。
    """
    v = sn.LOG_PROFILES[profile]["enc"](np.clip(arr_lin, 0.0, 64.0))
    v = np.clip(v, 0.0, 1.0) * 255.0
    if noise:
        rng = np.random.default_rng(hash((profile, seed)) & 0xFFFFFFFF)
        v = v + rng.normal(0.0, noise, v.shape)
    return np.floor(np.clip(v, 0.0, 255.0) + 0.5).astype(np.uint8)


def linear_pool(normal_dir, tmp, n=4, edge=360):
    """线性场景池：优先真实照片（反 sRGB → 线性），否则合成。"""
    out = []
    if normal_dir:
        rx = re.compile(r"\.(?:jpe?g|png|tiff?)$", re.I)
        for p in sorted(glob.glob(os.path.join(normal_dir, "*")))[:40]:
            if len(out) >= n:
                break
            if not (os.path.isfile(p) and rx.search(p)):
                continue
            try:
                im = Image.open(p).convert("RGB")
            except Exception:
                continue
            im.thumbnail((edge, edge))
            out.append((os.path.basename(p), sn.srgb_to_linear(np.asarray(im, np.float32) / 255.0)))
    if not out:
        for seed in (3, 5, 7, 11):
            out.append((f"synth{seed}", sn._synth_scene(240, 360, seed=seed, black=False)))
    return out


def add_black(lin, frac=12):
    """在左上角压出一块真正的纯黑（模拟实拍里被压死的暗部）→ 基灰指纹显形"""
    h, w = lin.shape[:2]
    out = lin.copy()
    out[: max(2, h // frac), : max(2, w // frac)] = 0.0
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("normal_dir", nargs="?", default=None, help="普通成片目录（相机/手机 JPEG）")
    ap.add_argument("--normal-dir", dest="normal_dir_kw", default=None, help="同 normal_dir")
    ap.add_argument("--raw-dir", default=None, help="含 RAW 的目录（D 组）")
    ap.add_argument("--pattern", default=r"^(DSC|_DSC)[0-9]+\.JPG$", help="C 组文件名过滤")
    ap.add_argument("--limit", type=int, default=12)
    ap.add_argument("--noise", type=float, default=1.5, help="log 码值域噪声 σ（0-255 单位）")
    args = ap.parse_args()
    normal_dir = args.normal_dir or args.normal_dir_kw

    tmp = tempfile.mkdtemp(prefix="eval_detect_")
    pool = linear_pool(normal_dir, tmp)
    stats = {k: [0, 0] for k in "ABCD"}
    exact = {"A": 0, "B": 0}
    rows = {"A": [], "B": []}

    # ── A / B：合成灰片（A 强制纯黑、B 保持照片原样即「无纯黑」）
    for tag in ("A", "B"):
        for src_tag, lin in pool:
            scene = add_black(lin) if tag == "A" else lin
            for name in sn.LOG_PROFILES:
                p = os.path.join(tmp, f"{tag}_{src_tag}_{name}.png")
                Image.fromarray(encode(scene, name, args.noise, seed=abs(hash(src_tag)) % 9999),
                                "RGB").save(p)
                info = sn.probe(p)
                family = [n for n, q in sn.LOG_PROFILES.items()
                          if abs(q["floor"] - sn.LOG_PROFILES[name]["floor"]) <= sn.QUANT_TOL]
                in_family = info.profile in family
                exact[tag] += (info.profile == name)
                ok = (info.kind == "log" and in_family) if tag == "A" \
                    else (info.kind in ("log", "suspect"))
                stats[tag][1] += 1
                stats[tag][0] += ok
                rows[tag].append((src_tag, name, info.kind, info.confidence,
                                  info.profile or "-", "OK" if ok else "FAIL", in_family))

    for tag, title, note in (
            ("A", "A 灰片·含纯黑（真实照片线性源 + 噪声）", "应判为 log，且锁进同基灰族"),
            ("B", "B 灰片·无纯黑（照片原样 + 噪声）", "允许保守跳过，但绝不能当普通成片")):
        print(f"── {title}：{note} ──")
        print(f"{'源':<16}{'真值':<11}{'判定':<10}{'置信':<8}{'锁定':<11}{'族内':<6}结果")
        for r in rows[tag]:
            print(f"{r[0][:15]:<16}{r[1]:<11}{r[2]:<10}{r[3]:<8}{r[4]:<11}"
                  f"{'是' if r[6] else '否':<6}{r[5]}")
        print(f"  类型正确 {stats[tag][0]}/{stats[tag][1]}；"
              f"曲线完全正确 {exact[tag]}/{stats[tag][1]}\n")

    # ── C：真实普通成片
    normals = []
    if normal_dir:
        rx = re.compile(args.pattern, re.I)
        for p in sorted(glob.glob(os.path.join(normal_dir, "*"))):
            if len(normals) >= args.limit:
                break
            if os.path.isfile(p) and rx.search(os.path.basename(p)):
                normals.append(p)
    if not normals:
        for src_tag, lin in pool:
            p = os.path.join(tmp, f"normal_{src_tag}.png")
            Image.fromarray((sn.render_neutral(lin) * 255 + .5).astype(np.uint8), "RGB").save(p)
            normals.append(p)

    print("── C 普通成片：绝不能触发还原 ──")
    print(f"{'文件':<24}{'判定':<10}{'置信':<8}结果")
    for p in normals:
        info = sn.probe(p)
        ok = info.kind not in ("log", "flat")
        stats["C"][1] += 1
        stats["C"][0] += ok
        print(f"{os.path.basename(p):<24}{info.kind:<10}{info.confidence:<8}"
              f"{'OK' if ok else 'FAIL ← 触发了还原'}")
        if not ok:
            print(f"{'':<42}! {info.action}")
    print(f"  {stats['C'][0]}/{stats['C'][1]}\n")

    # ── D：RAW 只需显影，绝不反 log
    raws = []
    if args.raw_dir:
        for p in sorted(glob.glob(os.path.join(args.raw_dir, "*")))[:3]:
            if RAW_RX.search(p):
                raws.append(p)
    if raws:
        print("── D RAW：只需显影，绝不反 log ──")
        for p in raws:
            info = sn.probe(p)
            ok = info.kind in ("raw", "unsupported")
            stats["D"][1] += 1
            stats["D"][0] += ok
            print(f"{os.path.basename(p):<24}{info.kind:<10}{info.confidence:<8}"
                  f"{'OK' if ok else 'FAIL ← 对 RAW 做了反 log'}")
        print(f"  {stats['D'][0]}/{stats['D'][1]}\n")
    else:
        stats.pop("D")

    parts = "，".join(f"{k} {v[0]}/{v[1]}" for k, v in stats.items())
    allok = all(v[0] == v[1] for v in stats.values())
    print(f"结论：{parts} → {'通过' if allok else '未达标'}")
    print(f"（合成素材留在 {tmp}）")
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
