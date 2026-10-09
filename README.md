# autosub

给视频自动加字幕：**本地 Whisper 转写 → SRT → ffmpeg 烧录 / 软字幕**，一套代码跑 Windows / Linux / macOS。

喂进去一个视频，拿出来两样东西：**字幕文件（`.srt`）** 和 **带字幕的视频**。有命令行，也有浏览器界面。

> 转写全程在本机完成，视频不会上传到任何服务器。只有第一次用某个模型时会下载权重（可用国内镜像，也可以提前下载好离线部署）。

---

## 目录

- [它解决什么问题](#它解决什么问题)
- [特性](#特性)
- [快速开始](#快速开始)
- [命令行用法](#命令行用法)
- [浏览器界面](#浏览器界面)
- [模型管理（预下载 / 离线部署）](#模型管理预下载--离线部署)
- [打包与分发](#打包与分发)
- [项目结构](#项目结构)
- [常见问题](#常见问题)
- [维护者文档](#维护者文档)
- [许可证](#许可证)
- [致谢](#致谢)

---

## 它解决什么问题

剪视频、做课程、整理会议录像时，手动敲时间轴字幕是最枯燥的一步。现成的在线服务要么按分钟计费、要么得把素材上传到别人的服务器；本地跑的方案往往卡在环境上——ffmpeg 缺 libass、中文字幕变成一排方块、模型下载到一半断掉、有显卡却因为缺 CUDA 运行库直接崩掉。

autosub 把这些坑一次性收在一处：**下载 → 转写 → 断句排版 → 烧录**跑成一条命令，环境问题由 `autosub doctor` 一条条告诉你缺什么、怎么补。

## 特性

| 特性 | 说明 |
|---|---|
| **本地离线** | 转写不联网。`--offline` 可以强制只用本地权重，缺文件立刻报错而不是偷偷去下载 |
| **两种输出** | `.srt` 字幕文件 + 带字幕视频，一次都给 |
| **三种成片模式** | 烧录进画面（任何播放器都能看）/ 软字幕轨（播放器里可开关）/ 只要字幕文件（不需要 ffmpeg） |
| **中文排版** | 自动断句、每行字数与单条时长都可调；标点优先切分、两行长度均衡，**不会把词从中间切断** |
| **中文字体** | 自动寻找系统 CJK 字体（Windows 微软雅黑 / macOS 苹方 / Linux Noto CJK），也可手动指定字体文件 |
| **跨平台** | 纯 Python，一份代码跑三平台；`uv sync` 或 `pip install .` 均可 |
| **两种外壳** | `autosub` 命令行 + `autosub-gui` 浏览器界面（只用标准库，不需要 npm / 构建步骤） |
| **设备自适应** | 自动挑模型和设备；有显卡优先 GPU，**但会先探测 CUDA 运行库是否真的能加载**，缺库就安静地走 CPU 而不是崩掉 |
| **可分发** | 一条命令打包成两个免安装可执行文件，或打成通用 wheel |
| **可编程** | `core.run_job()` 是唯一处理入口，CLI 和 GUI 都是它的薄壳；HTTP API 也是公开的，可以自己接 |

---

## 快速开始

需要 **Python 3.10 ~ 3.13**。推荐用 [uv](https://docs.astral.sh/uv/)：

```bash
cd autosub
uv sync                     # 按 pyproject.toml 建环境、装依赖
uv run autosub doctor       # 环境自检：ffmpeg / 字体 / 显卡都看一眼
```

不想用 uv 也行：

```bash
pip install .               # 装完就有 autosub 和 autosub-gui 两个命令
```

然后：

```bash
uv run autosub 视频.mp4     # 出 视频.srt + 视频_subtitled.mp4
uv run autosub-gui          # 或：开浏览器界面
```

ffmpeg 只在「烧录 / 软字幕」这一步需要（纯出 SRT 不需要）：

| 平台 | 安装 |
|---|---|
| Windows | `winget install Gyan.FFmpeg` |
| macOS | `brew install ffmpeg` |
| Linux | `sudo apt install ffmpeg` |

也可以什么都不装——把 `ffmpeg`（Windows 是 `ffmpeg.exe`）放在程序**同一个目录**里，程序会自动找到它；或设 `AUTOSUB_FFMPEG=/path/to/ffmpeg`。

---

## 命令行用法

```bash
uv run autosub 视频.mp4                    # 默认：烧录 + 出 SRT
uv run autosub 视频.mp4 --srt-only         # 只要字幕文件（不需要 ffmpeg）
uv run autosub 视频.mp4 --soft             # 软字幕轨（播放器里可开关）
uv run autosub 视频.mp4 -m large-v3 --language zh --hf-mirror
uv run autosub ./素材目录 -o ./out         # 目录批量处理
uv run autosub doctor                      # 环境自检
uv run autosub download-model --list       # 看各模型的本地缓存状态
```

常用参数：

| 参数 | 说明 |
|---|---|
| `-m/--model` | `auto`（有显卡用 large-v3，否则 small）/ `tiny` / `base` / `small` / `medium` / `large-v3` / `large-v3-turbo` / `distil-large-v3`，**或一个本地模型目录路径** |
| `--device` | `auto` / `cpu` / `cuda` |
| `--compute-type` | `auto` / `int8` / `float16` 等，CPU 默认 int8 |
| `--language` | 强制语言（`zh` / `en` / `yue` …），不填自动识别 |
| `--initial-prompt` | 喂专有名词（地名、人名、术语），**中文识别准确率提升最明显的一招** |
| `--no-vad` | 关闭静音切分（默认开启，能减少空转） |
| `--max-chars` / `--max-lines` | 每行字数（默认 16）/ 每条行数（默认 2） |
| `--max-duration` / `--min-duration` | 单条字幕最长（6.0s）/ 最短（0.8s）时长 |
| `--font-size` / `--font-color` / `--outline` / `--margin-v` | 烧录样式 |
| `--font-file` / `--font-name` | 手动指定字体文件或字体家族名 |
| `--video-codec` / `--preset` / `--crf` | 编码与画质（`--video-codec h264_nvenc` 可走 N 卡硬件编码） |
| `-o/--outdir` | 输出目录，默认与视频同目录 |
| `--hf-mirror` | 用 hf-mirror.com 下模型（国内网络推荐） |
| `--offline` | 只用本地已下载的权重，绝不联网（缺文件立刻报错） |
| `--ffmpeg` | 手动指定 ffmpeg 路径 |
| `--keep-temp` / `-q` | 保留临时目录便于排查 / 减少输出 |

输出的命名规则：`视频.srt`、`视频_subtitled.mp4`（沿用原文件名加后缀，方便和原片对应）。

---

## 浏览器界面

```bash
uv run autosub gui                 # 起服务并自动打开浏览器（默认 http://127.0.0.1:8765/）
uv run autosub-gui --port 9000 --no-browser
```

页面上：

1. **选择原始视频** —— 把文件拖进框里，或点击选择；
2. **参数** —— 模型、语言、输出方式、每行字数、字号、距底边距离、专有名词提示，另有「本地模型目录」「国内镜像」「离线模式」三个开关；
3. **结果** —— 左侧原始视频、右侧带字幕视频**左右并排对比**，两边的预览**点一下就能全屏放大播放**（点背景 / 右上角 × / 按 Esc 关闭），下方给「字幕」「视频」两个下载链接；
4. **任务列表** —— 视频 / 状态 / 模型 / 结果 / 时间 / 耗时，六个字段一眼看全，运行中的任务耗时实时增长。

顶部有版本号徽标和设备徽标（**GPU 蓝色、CPU 红色**），处理过程中能看到阶段、百分比和实时日志。

任务与产物放在 `~/.autosub/jobs/<时间戳>_<id>/`（可用 `AUTOSUB_JOBS_DIR` 或 `--jobs-dir` 改）。默认只监听 `127.0.0.1`；`--host 0.0.0.0` 可让同网段设备访问（自己承担风险）。

> 端口被占用时会自动顺延到下一个，不会出现两个实例悄悄抢同一个端口的情况。

### HTTP 接口（想自己接的话）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 页面 |
| GET | `/api/doctor` | 环境信息（版本、设备、ffmpeg、字体、烧录能力） |
| POST | `/api/jobs` | JSON `{name, mode, model, language, initial_prompt, max_chars, font_size, margin_v, hf_mirror, offline}` → `{id}` |
| PUT | `/api/jobs/<id>/file?name=x.mp4` | 原始字节流直接上传（不用 multipart，几十 GB 也不爆内存） |
| POST | `/api/jobs/<id>/start` | 开始处理 |
| GET | `/api/jobs/<id>` | 状态 / 进度 / 日志 / 模型 / 耗时 |
| GET | `/api/jobs/<id>/media?what=input\|video` | 预览（支持 Range，可拖进度条） |
| GET | `/api/jobs/<id>/download?what=srt\|video` | 下载 |
| DELETE | `/api/jobs/<id>` | 删任务和产物 |

用命令行测一遍（不用开浏览器）：

```bash
ID=$(curl -s -X POST localhost:8765/api/jobs -H 'Content-Type: application/json' \
     -d '{"name":"demo.mp4","model":"tiny"}' | python -c "import sys,json;print(json.load(sys.stdin)['id'])")
curl -s -X PUT --data-binary @demo.mp4 "localhost:8765/api/jobs/$ID/file?name=demo.mp4"
curl -s -X POST "localhost:8765/api/jobs/$ID/start"
curl -s "localhost:8765/api/jobs/$ID"
```

---

## 模型管理（预下载 / 离线部署）

```bash
uv run autosub doctor                            # 环境自检，附各模型的本地缓存状态
uv run autosub download-model --list             # 只看模型列表与缓存状态
uv run autosub download-model large-v3           # 下到 HF 缓存，之后 -m large-v3 直接用
uv run autosub download-model large-v3 -o D:\mdl # 下成纯文件目录，可整个拷走
```

| 下载方式 | 产物形态 | 之后怎么用 |
|---|---|---|
| 不带 `-o` | HuggingFace 缓存结构（`hub/models--<org>--<名>/`） | `-m large-v3` |
| 带 `-o 目录` | 纯文件目录（含 `model.bin`） | `-m "D:\mdl"`，也可整目录拷到别的机器 |

**给没网的机器准备模型：**

1. 在联网机器上 `autosub download-model large-v3 -o D:\mdl`（国内加 `--hf-mirror`）；
2. 把 `D:\mdl` 整个目录（U 盘 / 压缩包随意，里面没有软链接）拷到离线机器；
3. 离线机器上运行：

```bash
autosub 视频.mp4 -m "D:\mdl" --offline
```

也可以按缓存方式部署：把模型目录放进离线机器的 `%USERPROFILE%\.cache\huggingface\hub\`（或设 `HF_HOME` 指向拷过去的目录），然后直接 `-m large-v3 --offline`，不用写长路径。

模型名 → 仓库与体积对照：

| `-m` 值 | HuggingFace 仓库 | 约多大 |
|---|---|---|
| tiny | `Systran/faster-whisper-tiny` | 75 MB |
| base | `Systran/faster-whisper-base` | 141 MB |
| small | `Systran/faster-whisper-small` | 464 MB |
| medium | `Systran/faster-whisper-medium` | 1.4 GB |
| large-v3 | `Systran/faster-whisper-large-v3` | 2.9 GB |
| large-v3-turbo | `mobiuslabsgmbh/faster-whisper-large-v3-turbo` | 1.5 GB |
| distil-large-v3 | `Systran/faster-distil-whisper-large-v3` | 1.4 GB |

> 注意：`large-v3-turbo` 和 `distil-large-v3` **不在 Systran 组织下**，删缓存时别按 `Systran/...` 找。
> 缓存里出现 `blobs\*.incomplete`，或 `snapshots` 下没有 `model.bin`，就是没下完——`doctor` 会标成「不完整」。

---

## 打包与分发

```bash
uv run python build_exe.py                 # 两个可执行程序 → dist/autosub-<版本>-<平台标签>/
uv run python build_exe.py --wheel         # wheel → dist/autosub-<版本>-py3-none-any.whl
uv run python build_exe.py --wheel --exe   # 两个都出
uv run python build_exe.py --onefile       # 退回单文件（一个 exe 承担 CLI + GUI）
```

默认一次构建产出**两个可执行文件**，它们共享同一份依赖目录（`_internal/` 只存一份，体积不翻倍）：

```
dist/autosub-<版本>-win-x86_64/
├── autosub.exe         ← 命令行
├── autosub-gui.exe     ← 浏览器界面（双击即用）
└── _internal/          ← 两者共用的依赖
```

```bash
autosub 视频.mp4        # 命令行：出 SRT + 带字幕的视频
autosub doctor          # 环境自检
autosub-gui             # 浏览器界面，自动开浏览器
autosub-gui --port 9000 --no-browser
```

`autosub-gui.exe` 双击即可运行，不需要额外参数。加 `--windowed` 可让 GUI 程序不弹控制台窗口（默认保留控制台，方便看地址和日志）。

构建脚本会先自动跑一次环境自检做冒烟测试，两个程序各测一次。

注意：

- **可执行程序是平台专用的**。Windows 上打出来的 `.exe` 只能在 Windows 跑；Linux / macOS 要在各自机器上重新执行一次 `uv run python build_exe.py`。
- **wheel 是各平台通用的**，但它只包含 Python 代码，依赖（faster-whisper、ctranslate2 等）由 pip 装。
- 两者都**不含模型权重**：第一次跑某个模型时才会下载（`large-v3` 约 3 GB），下载一次长期有效，之后可离线用。
- `--no-smoke` 可跳过构建后的自检；`--keep-build` 保留 `build/` 中间目录便于排查。

---

## 项目结构

```
autosub/
├── pyproject.toml           # 依赖与入口声明（版本号唯一真源）
├── build_exe.py             # 打包脚本（可执行程序 / wheel）
├── packaging/
│   ├── entry_cli.py         # 可执行程序入口：autosub
│   ├── entry_gui.py         # 可执行程序入口：autosub-gui
│   └── entry.py             # 多态入口（--onefile 单文件模式用）
├── tests/smoke.py           # 自检（离线可跑）：uv run python tests/smoke.py
├── CONTRACT.md              # 改代码前必读：约束与影响矩阵
├── GOTCHAS.md               # 已知陷阱
├── RELEASE.md               # 发版清单
├── AGENTS.md                # 给 AI 协作者的说明
└── src/autosub/
    ├── core.py              # 全部核心逻辑：断句、字体、转写、烧录
    ├── cli.py               # 命令行外壳
    ├── gui.py               # HTTP 服务（只用标准库）
    ├── webui.py             # 页面 HTML/CSS/JS（内联，打包不会漏文件）
    └── __main__.py          # python -m autosub
```

`core.run_job(video, Settings(...), ffmpeg, probe, on_progress=...)` 是唯一的处理入口，CLI 和 GUI 都只是给它填参数、收进度。想挂到别的系统（比如行程视频一键出字幕）直接调它就行。

> 小提醒：`test/`（单数）是本地 venv，`tests/`（复数）才是测试代码。

---

## 常见问题

**模型下载慢 / 卡住** — 加 `--hf-mirror`（GUI 里勾「国内网络」）。首次下载 `large-v3` 约 3 GB。

**报错 `model.bin` 不完整、或缓存里出现 0 字节文件** — Windows 上下载中断或软链接异常。删掉对应缓存再重试：

```bash
rmdir /s /q "%USERPROFILE%\.cache\huggingface\hub\models--Systran--faster-whisper-large-v3"   # Windows
rm -rf ~/.cache/huggingface/hub/models--Systran--faster-whisper-large-v3                      # macOS/Linux
```

程序在 Windows 上已默认禁用软链接（`HF_HUB_DISABLE_SYMLINKS=1`），想改回来设 `AUTOSUB_ALLOW_SYMLINKS=1`。

**有显卡却报 `Library cublas64_12.dll is not found`** — pip 装的 ctranslate2 不带 CUDA 运行库。现在程序会自动探测并改走 CPU（不会崩），想要 GPU 加速就补上运行库：

```bash
uv pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
```

**CPU 跑得太慢** — 用 `-m small --compute-type int8`（默认就是这套）；`large-v3` 在纯 CPU 上基本不可接受。

**画面里中文是方块** — 没找到中文字体。用 `--font-file /path/to/字体.ttf` 指定，或装一套 Noto CJK。

**ffmpeg 说没有 subtitles 滤镜** — 你的 ffmpeg 没编 libass，换成 full build 版本，或改用 `--soft`。

**播放器看不到软字幕** — 只有支持字幕轨的播放器（VLC、PotPlayer、mpv）才会自动显示；想让所有播放器都能看到就用烧录模式（默认）。

**视频里没有音频轨道 / 识别不到语音** — 会得到一条明确的中文提示，而不是一串堆栈：前者说明是纯画面素材，后者会告诉你只生成了空字幕文件。

**GPU 用户想更快** — `--video-codec h264_nvenc`（N 卡）或 `h264_videotoolbox`（Mac）能把烧录这步也交给硬件。

---

## 维护者文档

改代码前建议按这个顺序看：

| 文件 | 什么时候看 |
|---|---|
| [`CONTRACT.md`](CONTRACT.md) | **改代码前必读**：不可协商约束、分层、影响矩阵、版本号真源、产物命名 |
| [`GOTCHAS.md`](GOTCHAS.md) | 遇到怪现象，或要动 ffmpeg / PyAV / PyInstaller / huggingface_hub / GPU 相关代码时 |
| [`RELEASE.md`](RELEASE.md) | 要出 wheel / exe，或改了版本号 |
| [`AGENTS.md`](AGENTS.md) | 由 AI 协作者接手时 |

改完跑一次自检：

```bash
uv run python tests/smoke.py                                     # 默认全跑（离线，约 1 分钟）
uv run python tests/smoke.py --video "带人声的视频.mp4"            # 额外验成功路径
uv run python tests/smoke.py --exe-dir dist/autosub-1.2.7-win-x86_64   # 额外验打包产物
```

---

## 许可证

本项目以 [MIT 许可证](LICENSE) 发布，可自由使用、修改、分发（包括商用），请保留版权声明与许可声明。

```
Copyright (c) 2026 autosub contributors
```

---

## 致谢

- 转写能力来自 [faster-whisper](https://github.com/SYSTRAN/faster-whisper)（CTranslate2 推理）与 [OpenAI Whisper](https://github.com/openai/whisper) 的开源模型。
- 视频处理与字幕烧录依赖 [FFmpeg](https://ffmpeg.org/) 及其 `subtitles`（libass）滤镜。
- 本项目的设计、编码、测试与文档由 **[WorkBuddy](https://www.workbuddy.cn/)** 协助完成。
