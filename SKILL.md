---
name: relayvsr-upscaler
description: "本技能在用户想要对视频进行放大/超分/提升分辨率（中文：放大、超分、超分辨率、画质增强、relayvsr；英文：upscale, video upscaling, super-resolution, enhance video resolution）时使用。它调用本地已运行的 ComfyUI（默认 127.0.0.1:8188），加载技能内捆绑的 RelayVSR x2 工作流，把源视频分段送入 ComfyUI 放大后硬切拼接并复用原片音轨。Use when the user asks to upscale a video, improve video resolution, or run RelayVSR locally."
agent_created: true
---

# RelayVSR 视频放大技能

## Overview

把「用户说想放大一段视频」变成一条可复用的本地流水线：用本机 ComfyUI 跑 RelayVSR x2 工作流，输出分辨率翻倍（如 1536×656 → 3072×1312）、帧率与帧数不变，画面用 packet copy 硬切拼接、音轨从原片取回（**AAC 192k 重编码，非 bit-exact**，见下方踩坑记录）。工作流 JSON 与运行脚本都打包在本技能内，换项目直接调用即可。

## When to Use

- 用户明确说「把这段视频放大 / 超分 / 提升分辨率 / 画质增强」。
- 用户提到 RelayVSR、视频超分辨率、或想让低清视频变清晰。
- 不要用于：静态图片放大（本工作流是视频专用）、x4 放大（见下方限制）、在线/云端放大 API。

## Prerequisites（执行前确认）

1. **本地 ComfyUI 已在运行**，且地址可达（默认 `http://127.0.0.1:8188`）。先用 `/system_stats` 探一下，并确认 `/queue` 为空（脚本遇非空队列会直接退出）。
2. **模型已就位**：`relayvsr\relayvsr_x2_lightx2v_int8_convrot.safetensors` 与 `relayvsr_flash_vae_x2_fp32.safetensors` 已放入 ComfyUI 的 models 目录（脚本会强制写这两个文件名，若用户机器路径不同，用 `--unet` / `--vae` 覆盖）。本机实际位置是 `E:/ComfyUI/models/diffusion_models/relayvsr/` 与 `E:/ComfyUI/models/vae/`；用 `GET /object_info/UNETLoader` 确认 ComfyUI 能列出该文件名，比翻目录可靠。
3. **ComfyUI 目录位置**：脚本默认 input=`E:/ComfyUI/input`、output=`E:/ComfyUI/output/upscale`。若 ComfyUI 装在其他盘，用 `--indir` / `--outdir` 指定。
4. **源视频先探测**：分辨率、总帧数、帧率、是否有音轨。用 `-of json` 按键取值，不要依赖 ffprobe CSV 的字段顺序（见踩坑记录）。

## How to Use（执行流程，逐步确认后再跑）

本流程消耗算力与时间，**在正式提交 ComfyUI 前，必须先把「源路径 / 输出路径 / detail 值」列给用户确认**，拿到确认再执行。

1. **解析请求**：从用户消息提取源视频路径；若用户没给输出路径，默认在源同目录生成 `<原名_放大x2.mp4>`。
2. **确认参数**（关键门控）：向用户回报——
   - 源视频：路径、分辨率、帧数、帧率、是否有音轨（用 ffprobe 先探测）
   - 输出：路径、目标分辨率（x2）
   - `detail`：默认 **0.8**（细节更实）；若用户更看重「贴近原真值、少锐化」，建议 **0.7**（实测拐点档，保留约 84% 高频增益、还原度更高）。先问用户要哪一档。
   - 分段：默认 118 帧/段，一般无需改。
   - **算力预算**：段数 = ceil(总帧数 / 118)；本机 RTX 5080 实测约 **0.38 s/帧**（1650×928 → 3300×1856、60 fps），据此估耗时与磁盘，并给用户「试片 / 全片」档位选择。
3. **运行脚本**（长片必须显式给 `--timeout` 和 `--workdir`）：

   ```bash
   python "<技能目录>/scripts/upscale_video.py" --src "<源视频>" --dst "<输出视频>" --detail 0.8 --seg 118 --tag <本次唯一标签> --timeout 43200 --workdir "<可访问的日志目录>"
   ```

   - `--timeout` **默认只有 7200 秒（2 小时）**，60 段以上的长片必然不够；按段数估后显式调大。
   - `--workdir` 不指定时 `*_result.json` / `*_run.csv` 会落在随机临时目录里，交付时找不到；务必显式指定。
   - `--tag` 每次运行用新值，失败重跑更要换（见踩坑记录「坏片段缓存」）。
   - 其余可选参数：`--host`、`--indir`、`--outdir`、`--unet`、`--vae`、`--fps`（0=自动探测）。

4. **校验交付**（脚本已自动做，但交付前再核一遍）：
   - 输出分辨率 = 输入的 2 倍宽高。
   - 输出帧数 = 输入帧数（零丢失；注意不要用 `-shortest` 封装，会吃掉尾帧）。
   - 音轨：源有音轨则已 **AAC 192k 重编码**并入，无则静音；核时长与源一致即可，AAC 帧数不必相等（实测差 1 帧）。
   - 体积、耗时、显存 p90 写进 `<workdir>/<tag>_result.json`；**`ram_max` 依赖 psutil，未安装时为 -1**，不是采样失败。

## Bundled Resources

- `assets/relayvsr_x2_video_api.json`：RelayVSR x2 视频工作流（LoadVideo → RelayVSRConditioning → KSampler → VSRDecode → CreateVideo → SaveVideo）。脚本会动态改写 LoadVideo 的 `file`、UNET/VAE 名称、`detail` 与 SaveVideo 的 `filename_prefix`。
- `scripts/upscale_video.py`：分段放大 + 拼接 + 音轨复用 + 显存/内存采样。已去除项目硬编码，路径与模型名全部参数化；`probe_frames()` 用 ffprobe JSON 按键取值，返回值是 3 个（帧数 / [宽,高] / 帧率），改函数时务必同步所有调用点。

## Known Limitations（重要，避免踩坑）

- **x4 不可用**：本技能只打包 x2 工作流。x4 在 RTX 5080 16GB 上会撞显存上限（输出像素 19.5M/帧、VRAM 96%+），实测整段失败；需要 x4 请先在 360P 源上验证或换更大显存机器。
- **长视频必须分段**：一次性整段跑在 700–800 帧以上会卡死/假死（内存公式约每帧 60·W·H 字节）；分段默认 118 帧是稳定档。
- **显存口径**：报告用 **p90** 当工作集，max 只作上限参考（Windows 显存有页面迁移抖动，max 会出现假峰值）。
- **帧数硬断言**：VFR 源 trim 会掉帧、`-shortest` 封装会吃尾帧——脚本已用 `cfr` 切段且不截断音轨规避。
- detail 是经验参数，0.7 偏真值、0.8 偏细节；不要让用户误以为越高越好（高 detail 会加剧锐化光晕）。

## 实测踩坑记录（2026-11-15 · ffprobe N-118242 / ComfyUI 0.45.0 / RTX 5080 16GB）

- **ffprobe CSV 字段顺序（已修）**：`probe_frames()` 曾假设 `-of csv=p=0` 输出 `nb_read_frames,width,height,r_frame_rate`，本机实际输出 `width,height,r_frame_rate,nb_read_frames`（如 `1650,928,60/1,7409`）→ 把宽度 1650 当总帧数、把 7409 当帧率，首次启动 34 秒即失败（`-r 7409`）。**已改为 `-of json` 按键取值**，不要改回 CSV。
- **解包数量（已修）**：`probe_frames()` 返回 3 个值，产物收集 / 拼接 / 最终校验三处曾只解包 2 个，走到后半程才 `ValueError`。这类 bug 首段成功也发现不了，必须通读调用点。
- **失败重跑必须换 `--tag`**：脚本会复用 `input/<tag>_sNNN.mp4`（只要帧数对得上就跳过）。首次失败若留下坏片段（例如按错误帧率生成的片段），复用会把错误带进全片；换新 tag 最省事，不要删用户文件。
- **音轨不是无损（已修文档与报告口径）**：脚本用 `-c:a aac -b:a 192k` 从原片重编码（画面才是 `-c:v copy`）。实测源 5319 个 AAC 帧 → 成片 5318 帧、时长同为 123.483991 s。对音质敏感的项目，把该行改成 `-c:a copy`（要求源音轨本身是 MP4 兼容编码），或成片后另做一次无损重封装。`result.json` 的 `audio` 字段现为 `aac_192k_reencode`，不再谎报 `copied`。
- **分段输入是重压过的**：切段用 `libx264 -crf 10 -preset fast`，模型看到的是近似无损重压帧而非源原始像素；对画质极敏感时，别把成片当作「从原像素直接超分」。
- **源宽高不必是 8/16 的倍数**：x2 时 `latent_stride=8`，`comfy/ldm/wan/model_vsr.py` 的 `relay_latent_size()` 按 `ceil(WH/16)*2` 算 latent，`decode()` 用 `F.pad(mode="replicate")` 把低清帧补到 16 的倍数（1650 → 1664），解码后再按输出尺寸裁回。实测 1650×928 → 精确 3300×1856 / 60 fps / 118 帧，不会报错；别把它误判为阻塞项。
- **长片工况参考**：1650×928 / 60 fps / 7409 帧 / 123.48 s → 63 段 × 118 帧，耗时 **46.8 分钟**（0.379 s/帧），显存 **p90 11.9 GB / max 14.9 GB**（max 是假峰值，看 p90），成片 474 MB。默认 7200 s 超时不够。
