# 胶卷处理前的色彩与光影再创作

## 处理目标与先后顺序

管线固定为 **源格式判定与基础色彩还原 → 再创作 → 胶卷模拟**。基础还原只解决 RAW 显影、可信 Log 灰片反曲线和普通成片直通，输出可用的 sRGB。再创作阶段在这张正常照片上决定画面的色彩方向、光影关系和视觉重心，输出新的 sRGB 图给胶卷模拟引擎。胶卷阶段才叠加各预设的曲线、色相分带、暗部/高光色彩交叉、颗粒、光晕和暗角。

再创作的目标是**明显改变观感，同时保留下一阶段处理余量**。尽量保留高光纹理、主体亮度和肤色形状；对已经饱和或噪声很重的源图，应选较低 `--style-strength` 或更合适的方向，而不是靠硬裁切制造冲击力。

## 可调方向

| `--creative-direction` | 色彩与光影动作 | 常见搭配 |
|---|---|---|
| `auto` | 由预设选择对应方向 | 默认；先看同源多预设接触表 |
| `warm` | 暗部保留暖棕、高光推琥珀，并增加明暗分离 | Gold、Kodachrome、江南烛光 |
| `cool` | 暗部偏青蓝、高光较中性，形成冷暖层次 | Superia、Classic Chrome、江南冷调、CineStill |
| `vivid` | 提高色度和局部反差，突出彩色主体 | Velvia、Ektar、Provia |
| `soft` | 压色度、抬暗部、柔化亮部，突出粉彩过渡 | Portra、Pro 400H、ETERNA |

`--style-strength` 范围 0–1，默认 1：0 为原有预设及旧流程，1 为完整的再创作加增强版胶卷模拟，中间值线性插值。`neutral` 不启动再创作，始终作为对照。`--save-creative` 将胶卷前的中间图保存到输出目录的 `creative-stage/`，名称含原片与预设名，可与最终图逐阶段比对。

例如：

```bash
python scripts/film_emulate.py -i photo.jpg -p gold200 -o out --save-creative
python scripts/film_emulate.py -i photo.jpg -p gold200 -o out-cool --creative-direction cool --style-strength 0.7 --save-creative
python scripts/film_emulate.py -i photo.jpg -p gold200 -o out-old --style-strength 0
```

## 胶卷风格识别度的验收

同一原片固定随机种子，至少给出原片、再创作中间图、旧版和新版最终图。整图应能区分**色彩关系、暗部与高光走势、主体突出方式**；1:1 裁切要能看到该预设相称的颗粒/光晕，而非只有 JPEG 噪声。Portra、Pro 400H 与 ETERNA 本来偏柔和，识别度应来自粉彩与影调；Classic Chrome 来自克制色彩和硬暗部；Velvia/Ektar 来自更浓的色彩与反差；CineStill 的红色晕只应围绕强点光源出现。若所有预设都变成同一高饱和效果，应重选方向或回调强度。

源图本身会影响效果。强烈的原有灯光色、极高饱和度、失焦或高 ISO 噪声，不能仅靠胶卷参数转成任何预设的典型样貌。要对实际输出而非参数表做判断。

## 参考依据与边界

设计取向参考 [富士官方 Film Simulation 说明](https://www.fujifilm-x.com/en-us/products/film-simulation/)（Velvia 鲜明、Classic Chrome 柔色硬暗部、ETERNA 柔色深影）、[富士 ETERNA 说明](https://www.fujifilm-x.com/global/products/film-simulation/eterna/)、[Kodak Portra 400 说明](https://www.kodakprofessional.com/photographers/film/color/kodak-professional-portra-400-film/516)、[Kodak Gold 200 技术资料](https://www.kodakprofessional.com/sites/default/files/wysiwyg/E7022-1.pdf)、[Kodak 胶片产品手册](https://www.kodakprofessional.com/sites/default/files/wysiwyg/film/KODAKPROFESSIONAL_Film_Brochure2018.pdf)及 [CineStill 800T 光晕说明](https://cinestillfilm.com/blogs/news/cinestill-800t-in-your-toolbox)。本工具的数值是依据这些定性特征设计的创作参数，并非品牌官方 LUT、实验室测量或原片扫描的精确复现。
