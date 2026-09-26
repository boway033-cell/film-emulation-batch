---
name: film-emulation-batch
description: 本地批量胶片感调色（胶片模拟）。用 Pillow+numpy 实现每通道曲线 LUT、色相带选择性调整（绿→青绿）、独立通道色偏(crossover)、暗部硬度、高斯颗粒、高光溢出(halation)、暗角、acutance，支持外部 .cube 3D LUT。含富士（Superia / 經典 Neg / C200 / Pro 400H / Velvia 50 / Provia 100F / Classic Chrome / Eterna / ACROS）与柯达（Portra / Gold / Ektar / Kodachrome / Cinestill / Tri-X）两族预设。当用户说"胶片感""胶片模拟""富士色调""青绿色""Portra 风格""加颗粒""光晕""把一批照片调成胶片"时使用。纯本地离线、无 API 费用。
agent_created: true
---

# 批量胶片感模拟（film-emulation-batch）

纯本地离线胶片调色。不依赖 Lightroom / Photoshop，不需要 API Key，素材不出本机。

## 何时用

- 把一整个目录的照片批量套成某种胶片风格（富士 9 款 / 柯达 6 款 / 黑白）
- 复现**富士的青绿色倾向**、**高光与暗部的色彩交叉**（见 `富士胶片色彩复现_资料与参数映射.md`）
- 给照片加胶片颗粒、高光光晕、暗角、边缘锐度
- 套用外部下载的 `.cube` 3D LUT
- 需要**可复现**（固定随机种子 + 固定参数，两次跑出同一张图）

## 环境

只需要 Python 3.9+ 与两个库：

```bash
pip install pillow numpy
```

脚本：`scripts/film_emulate.py`（单文件，无其它依赖）。

> 作者本机用的是托管解释器 `C:/Users/86153/.workbuddy/binaries/python/envs/default/Scripts/python.exe`（Pillow 12 + numpy 2.5）。下面示例里的 `$PY` 按你的环境替换即可。

## 用法

```bash
PY="python"                       # 换成你的解释器
S="scripts/film_emulate.py"

# 列出全部预设
"$PY" "$S" --list

# 批量：整个目录 → 输出目录（默认输出到 <输入同级>/film_<preset>/）
"$PY" "$S" -i "D:/photos" -p portra400 -o "D:/out" --jobs 6

# 文件名正则筛选（Windows 下 glob 大小写不敏感，*.JPG 会误抓 *.jpg，用正则更稳）
"$PY" "$S" -i "D:/photos" --pattern "^(DSC|_DSC)[0-9]+\.JPG$" -p gold200

# 全预设对比图（先让用户看效果再决定跑哪个）
"$PY" "$S" --sheet "D:/photos/one.jpg" -o "D:/preview" --max-edge 1200

# 单张微调（不改预设文件；一次可以叠多个）
"$PY" "$S" -i a.jpg -p superia400 --grain 1.6 --halation 0.6 --vignette 0.25 --sat 0.9 \
        --exposure 0.3 --temp -0.2 --tint 0.1 --acutance 0.2

# 一次跑完整个家族（输出到 <输出目录>/<preset>/）
"$PY" "$S" -i "D:/photos" --group fuji -o "D:/out"

# 套外部 3D LUT（与预设曲线叠加，strength 控制混合）
"$PY" "$S" -i "D:/photos" -p neutral --lut "D:/luts/Kodak2383.cube" --lut-strength 0.8
```

关键参数：`--jobs`(默认 min(6,CPU))、`--quality`(默认 96，4:4:4)、`--max-edge`(预览降采样)、`--seed`(颗粒种子，固定即复现)、`--suffix`、`--limit`、`--dry-run`、`--group`、`--pattern`。

## 预设

### 富士 FUJI

| preset | 风格 | 反差 | 暗部硬度 | 高光滚降 | 颗粒 | 锐度 | 适合 |
|---|---|---|---|---|---|---|---|
| `superia400` | 深阴影偏青绿、高光偏品红、绿→青绿、黄被压 | 中高 | 中 | 0.76/3.8 | 1.25 | 0.16 | 万能：街拍、植物、夜景 |
| `classic_neg` | 暗部硬调、高光转暖、饱和被抑制 | 高 | 高 | 0.70/2.8 | 1.05 | 0.14 | 纪实、色调分离 |
| `c200` | 冷而准、不变黄 | 低 | 低 | 0.80/3.0 | 1.00 | 0.14 | 日光日常 |
| `pro400h` | 高光极软不爆、暗部永不真黑、粉彩通透 | 极低 | 负 | 0.55/1.2 | 0.65 | 0.10 | 白墙建筑、人像 |
| `velvia50` | 极饱和反差、祖母绿绿、开敞阴影品红 | 极高 | 高 | 0.90/5.0 | 0.35 | 0.18 | 风光、秋叶、蓝天 |
| `provia100f` | 中性准确、略偏冷、绿忠实 | 中 | 中 | 0.86/3.4 | 0.40 | 0.16 | 要准确干净 |
| `classic_chrome` | 低饱和、暗部硬调偏青蓝、高光冷静 | 中 | 很高 | 0.68/2.4 | 0.60 | 0.12 | 冷调纪实 |
| `eterna` | 饱和最低、过渡极柔、几乎不爆 | 极低 | 负 | 0.50/1.0 | 0.55 | 0.06 | 电影感、高动态 |
| `acros` | 黑白、颗粒极细、暗部细节多、微冷 | 中 | 中 | 0.86/3.0 | 0.90 | 0.20 | 黑白纪实 |

### 柯达 / 其他 KODAK

| preset | 风格 | 适合 |
|---|---|---|
| `portra400` | 中性偏暖、低对比、高光柔和 | 通用首选 |
| `gold200` | 金黄暖调、绿偏黄橄榄、黄红更饱和 | 怀旧日常 |
| `ektar100` | 高饱和、高对比、极细颗粒 | 建筑、风光 |
| `kodachrome` | 浓郁红黄、暗部深 | 复古国家地理 |
| `cinestill800t` | 强青蓝、高光红色光晕 | 夜景、霓虹 |
| `tri_x` / `hp5` | 黑白（柯达硬调 / 伊尔福中性） | 纪实黑白 |
| `neutral` | 仅抬黑+极轻颗粒 | 对照组 |

## 管线（改参数前先理解）

`sRGB → 白平衡/曝光 → 每通道曲线 LUT（含暗部硬度）→ 通道色偏 crossover → 色相带调整 → 黑白混合/外部3D LUT → acutance → 饱和度分档 → 高光溢出 → 颗粒 → 暗角 → sRGB`

按顺序的直觉：
1. **曲线 LUT**：`contrast`(S 曲线) / `shoulder`+`shoulder_k`(高光滚降起点与陡度) / `shadow_contrast`(暗部硬度，正=压暗，负=提暗) / `toe`(暗部软着陆) / `lift`(黑位抬升=哑光黑) / `gamma`(每通道中间调位置)。
2. **crossover**：`delta_c = shadow_c*(1-lum) + highlight_c*lum`，按亮度在两组每通道偏移间线性插值。这是"独立通道色偏"的表达方式——阴影往青绿偏、高光往品红偏，就是富士特有的色彩交叉。
3. **hue_bands**：在 Rec.601 的 Y/Cb/Cr 对偶空间里按色带做选择性色相旋转 + 饱和度增益。**逐通道曲线做不到色相变化，青绿倾向必须靠这一步。** 角度约定（已实测校准）：red 113° → orange 146° → yellow 173° → green 225° → cyan 293° → blue 353° → magenta 45°，所以 **`hue` 正值 = 绿→青绿（富士），负值 = 绿→黄橄榄（柯达）**。
4. **粒**：`amount`(1.0 ≈ 中间调 σ4.9 灰阶) × 亮度调制 `0.40+0.85·4·L·(1-L)`（中间调最重，暗部保留底噪）；`size` 为高斯 σ(px)，决定颗粒粗细；`chroma` 为色度颗粒占比。
5. **acutance**：带 pad 边距的 FFT 反锐化，复现"富士观感更锐"（富士 0.10–0.20，柯达 0.06–0.08）。
6. **光晕**：阈值以 **sRGB 等效值**给出，内部转线性亮度计算（符合散射能量衰减）；`radius` 是**占长边比例**，所以在任何分辨率下观感一致；wide+tight 双尺度。
7. **暗角**：可分离 `rx²+ry²`，逐带计算，零额外内存。
8. **白平衡 `temp`/`tint`**：整条富士线偏冷（≈5000–5200K），柯达偏暖。这是"像不像"的第一道关，比任何曲线都先被人眼捕捉。

## 已踩过的坑（改代码前必读）

1. **PIL 的 `GaussianBlur` 不支持 `'F'` 模式** → `ValueError: image has wrong mode`。float 数组模糊一律用脚本内的 `gauss_blur_fft`（FFT 高斯，任意 dtype）。
2. **`np.fft.rfft2` 只对最后一轴取半谱**：轴 0 的频率要用 `fftfreq(h)`，轴 1 用 `rfftfreq(w)`。两者都用 `rfftfreq` 会得到形状不匹配的广播错误（`(266,201)` vs `(134,201)`）。
3. **`.cube` 轴的顺序**：文件里 **r 变化最快**，所以 `reshape(n,n,n,3)` 得到的轴 0 是 **b**，必须 `.transpose(2,1,0,3)` 才是 `data[r,g,b]`。搞错会出现"identity LUT 往返误差 0.99"或"红蓝互换 LUT 毫无作用"。
4. **acutance 必须带 pad 边距**：锐化依赖邻域，分带处理若只拿带内像素，会在带边界留下水平接缝。做法是把 `arr[a0:a1]` 用同一套 `_prep`（曝光/白平衡/LUT）预处理后算 luma，模糊再裁回带内。
5. **色相旋转用一阶近似**：`cos a≈1, sin a≈a`。位移通常在 10° 以内，误差可忽略，但换来零 `atan2`／零三角函数——24MP 下这是几秒的差别。权重用色带单位方向的余弦相似度，再乘 chroma 阈值权重，保证中性灰完全不动（实测偏移 0.000000）。
6. **Windows 下 `glob` 大小写不敏感**：`*.JPG` 会把 `xxx.jpg` 一起抓进来，批量前用 `--pattern` 正则锁死。
7. **EXIF 方向**：必须 `ImageOps.exif_transpose`，否则竖拍图是横的；保存时把 `274` 置 1 防止二次旋转。
8. **JPEG 必须 `subsampling=0`**：默认 4:2:0 会抹掉色度颗粒与细边缘，胶片颗粒是高频信号，一定用 4:4:4。带颗粒的输出体积比原片大 20–40%，嫌大就 `--quality 92`。
9. **内存**：24MP float32 全图约 290MB，多进程会爆。已采用分带（`BAND=512`）+ 1/4 分辨率光晕遮罩，6 进程峰值约 1.5GB。改代码时不要再引入全图 float 缓冲。
10. **单调性**：曲线 LUT 建好后 `np.maximum.accumulate` 强制单调，防止参数组合（尤其 `shadow_contrast` 与 `toe` 同时为正）导致色调反转。
11. **PIL 默认字体没有中文字形**。要给图表加中文标签必须 `ImageFont.truetype(r"C:/Windows/Fonts/msyh.ttc", size)`，找不到再退 `simhei.ttf` / `Deng.ttf`。

## 验证方式（改预设后必做）

```bash
"$PY" <skill>/scripts/film_emulate.py --sheet "<一张有高光的图>" -o "<预览目录>" --max-edge 1200
"$PY" <skill>/scripts/film_emulate.py --sheet "<一张有绿植的图>" -o "<预览目录>" --group fuji --max-edge 1050
```

**只看整图缩略图判断不了颗粒、色相偏移与暗部硬度，必须 1:1 裁切复核**：全分辨率渲染 → 裁 1100×740 的区域 → 拼图。`scripts/verify/` 下的校验脚本（全部按 argv 传参，从仓库根目录跑）：

- `probe.py` — 尺寸 / EXIF 方向 / 色彩信息
- `check.py` — 光晕触发率 + `.cube` 往返正确性（identity 与红蓝互换两个夹具）
- `check_hue.py` — 色相带调整：绿是否向青、灰是否完全不动、空带是否严格恒等
- `chart.py` — 色调曲线 / 色彩交叉矢量 / 饱和度-亮度 / 色相带偏移 四联图
- `verify_crop.py` `verify_fuji.py` `verify_halation.py` — 1:1 裁切校样（`verify_fuji.py` 自动定位绿色最密集窗口）
- `verify_batch.py` — 批量输出校验（数量、尺寸、EXIF 方向、平均改动量、体积变化）

## 富士 / 柯达的设计依据

见 `docs/fuji-color-science.md`：逐款性格、资料来源、富士特征 → 参数落点映射表、高光/暗部的复现清单、已知局限（是风格复现，不是物理仿真）。逐款完整数值见 `docs/preset-reference.md`。

