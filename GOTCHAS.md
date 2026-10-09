# 已知陷阱（GOTCHAS）

这份清单里的每一条都是**踩出来的**，而且大部分无法从代码本身推理出来（依赖 ffmpeg / PyAV / huggingface_hub / PyInstaller 的具体版本行为）。

格式统一为：**症状 → 根因 → 现有修法 → 别改回去的原因**。

改代码时如果动到了下面任一处，先读对应条目再动手。

---

## A. 进程与编码

### A1. 冻结成 exe 后中文输出乱码

- **症状**：`autosub.exe doctor` 输出一堆乱码；被管道/编辑器按 UTF-8 解读时更明显。
- **根因**：冻结后 Python 按系统 ANSI 代码页（中文 Windows 是 936）输出；控制台代码页与流的编码不一致。
- **现有修法**：`core.safe_streams()`（core.py:84）—— ① 用 `SetConsoleOutputCP(65001)` 把控制台切到 UTF-8；② 把 `stdout/stderr` `reconfigure(encoding="utf-8", errors="replace")`。
- **别改回去**：`errors="replace"` 是故意的，用来兜住无法编码的字符，去掉后极端情况会直接抛异常。
  想临时允许符号链接等行为请用环境变量开关（如 `AUTOSUB_ALLOW_SYMLINKS=1`），不要删代码。

---

## B. 模型与缓存（huggingface_hub）

### B1. 缓存里出现 0 字节文件 / 报 `model.bin` 不完整

- **症状**：首次下载后加载模型失败，快照目录里的文件是 0 字节。
- **根因**：`huggingface_hub` 会**误判** Windows 环境支持符号链接，把快照目录写成软链接指针，实际内容没落地。
- **现有修法**：`core.prepare_model_env()`（core.py:128）在 Windows 上默认设 `HF_HUB_DISABLE_SYMLINKS=1`（复制模式）。
- **别改回去**：复制模式的副作用是快照里是真实文件副本（`blobs/` 为空），这反而让缓存目录可以整体拷贝到离线机器 —— 是特性不是缺陷。

### B2. 首次跑卡在下载 / 直连 huggingface.co 不通

- **症状**：卡在"准备中"很久；或 HTTP 000 直接失败。
- **根因**：国内直连 huggingface.co 不稳定甚至不通。
- **现有修法**：`--hf-mirror`（设 `HF_ENDPOINT=https://hf-mirror.com`）、`download-model` 子命令预下载。
- **别改回去**：镜像必须**可选**（约束 5），不要把镜像地址写死成默认值。

### B3. 离线机器上要能跑

- **症状**：内网机器上没有网络，程序卡在超时。
- **现有修法**：`--offline`（设 `HF_HUB_OFFLINE=1`）+ `-m <本地模型目录>`；`local_model_dir()` / `check_local_model()` 会检查目录里有 `model.bin` 和 `config.json`。
- **注意**：`prepare_model_env()` 必须在 `import huggingface_hub` **之前**调用，否则环境变量不生效（hub 在 import 时读配置）。加新代码时不要破坏这个顺序。

### B4. 模型名与仓库名不是一对一

- **症状**：报错文案里提示的缓存目录路径不对（比如 `large-v3-turbo`）。
- **根因**：`turbo` 实际来自 `mobiuslabsgmbh/faster-whisper-large-v3-turbo`，`distil-*` 来自 `Systran/faster-distil-whisper-large-v3`，不是简单的 `Systran/faster-whisper-<名>`。
- **现有修法**：统一走 `repo_id_for(model)`（core.py:727），不要在别处手拼仓库名。

---

## C. GPU

### C1. 有 N 卡却报 `Library cublas64_12.dll is not found`

- **症状**：`auto` 选了 CUDA，模型加载 1.6 秒就绪，**开始转写时才爆**这句错。
- **根因**：pip 版 `ctranslate2` 不带 CUDA 运行库；驱动只提供 `nvcuda.dll`。而 ctranslate2 把 cublas 的加载**延迟到首次转写**，所以"包住模型加载"的兜底接不住。
- **现有修法**：`core.cuda_status()`（core.py:889）在选设备前用 ctypes 预探测 `cublas64_12.dll` / `cudnn64_9.dll`；缺库就让 `cuda_device_count()` 返回 0，`resolve_device("auto")` 自动落 CPU 并打印提示。
- **别改回去**：**不要把探测挪到转写之后，也不要用 `try/except` 包 `WhisperModel()` 来兜底** —— 那个位置拦不住这个错误。
  `--device cuda` 显式指定时应当 `die()` 报错而不是静默降级（用户明确要求了 GPU）。

### C2. 装了 `nvidia-cublas-cu12` 仍然找不到 DLL

- **根因**：pip wheel 自带的 DLL 在 `site-packages/nvidia/**/bin`，**不在系统 PATH 上**。
- **现有修法**：`_add_nvidia_dll_dirs()`（core.py:870）用 `os.add_dll_directory()` 把它们加进搜索路径。
- **别改回去**：靠改 `PATH` 是不行的（Windows 的 DLL 搜索不认后改的 PATH），必须用 `add_dll_directory`。

---

## D. ffmpeg / 媒体处理

### D1. 报"没有 subtitles 滤镜"但 ffmpeg 明明是 full build

- **症状**：误判 ffmpeg 能力。
- **根因**：`ffmpeg -filters` 的**标志列宽度随版本变化**（2 字符或 3 字符），早期用定长匹配会漏；且 `--enable-libass` 出现在 `-version` 的 `configuration` 行，**不是首行**。
- **现有修法**：`core.ffmpeg_probe()`（core.py:485）改用宽松正则 `^\s*[TSC.]+\s+(subtitles|ass)\s+\S+->`，并从完整 `-version` 输出里搜 `--enable-libass` / `--enable-fontconfig`。
- **别改回去**：不要恢复成"只取首行"或固定宽度匹配，换个 ffmpeg 版本就会误判。

### D2. 识别不到语音时烧录失败（`Unable to open sub.srt`）

- **症状**：视频没声音/没识别出内容，SRT 是空文件，ffmpeg 的 `subtitles` 滤镜直接报错，任务失败。
- **根因**：libass 打不开"没有任何字幕条目"的 SRT。
- **现有修法**：两层兜底 —— `run_job()` 在 `cues` 为空时提前返回友好提示；`burn_in()`（core.py:1158）检查 SRT 里没有 `-->` 就把滤镜退化成 `null`。
- **别改回去**：两层都要保留，`burn_in` 可能被单独调用（软字幕/其他入口）。

### D3. 纯画面视频报"内部错误：tuple index out of range"

- **根因**：PyAV 找不到音频流时取 `streams[...]` 抛 `IndexError`，被 GUI 的通用 except 显示成内部错误。真实场景会遇到（无声录屏、监控片段）。
- **现有修法**：`core.has_audio_stream()`（core.py:1314），`run_job()` 先拦一道给中文提示（core.py:1337）。
- **别改回去**：探测失败时**返回 True**（放过），让转写阶段报具体错误 —— 不要因为探测不出来就拒绝处理。

### D4. `filter_complex` / 滤镜里的 Windows 路径出问题

- **根因**：滤镜表达式里 `C:\` 的冒号和反斜杠需要转义，很容易踩。
- **现有修法**：`burn_in()` 统一把字幕和字体**复制进临时目录**，用相对路径（`sub.srt` + `fontsdir=fonts`）喂滤镜；`_escape_filter_path()` 处理必须传绝对路径的情况。
- **别改回去**：这是 Windows/Unix 都能跑的关键，绕开转义比猜转义更稳。

### D5. 找不到 ffmpeg

- **现有修法**：`find_ffmpeg()`（core.py:440）固定查找顺序：手动指定 → `AUTOSUB_FFMPEG` → exe 同级目录（`_MEIPASS` 与 `sys.executable` 所在目录）→ `PATH` → `imageio-ffmpeg`。
- **别改回去**：exe 版必须能识别"用户把 ffmpeg.exe 丢在程序旁边"这个最常见的用法。

---

## E. 打包（PyInstaller）

### E1. `FileNotFoundError: base_library.zip`

- **根因**：某些 PyInstaller 版本里 `CONF['workpath']` 会被拼上 spec 名，而建目录那步只建到上一级；配合 `--clean` 更容易触发。
- **现有修法**：`build_exe.py:_prepare_dirs()`（build_exe.py:139）自己把 `dist/ workpath/ specpath/ workpath/name/` **全部建好**，并**弃用 `--clean`**。
- **别改回去**：不要"顺手"把 `--clean` 加回命令行。

### E2. `_internal/` 被删掉后两个 exe 都跑不起来

- **症状**：只复制 `autosub.exe` 到别处，双击无反应或报缺 DLL。
- **根因**：默认是 onedir 模式，两个 exe 用 `MERGE()`（build_exe.py:206）共享同一份 `_internal/`，依赖不能只拷一个文件。
- **现有修法**：分发时必须给**整个产物目录**；想要单文件的用 `--onefile`。
- **别改回去**：不要为了"看起来干净"把 `_internal` 拆成两份，体积会翻倍。

### E3. **`build_exe.py` 默认不出 wheel**

- **症状**：`dist/` 里只有 `autosub-<版本>-<平台>/` 目录，没有 `.whl`；但你以为已经出了。（本项目 1.2.2–1.2.6 的 wheel 就是这样漏掉的。）
- **根因**：`build_exe.py:592` 的逻辑是 `--wheel` / `--sdist` 才构建包，**不加参数只构建两个 exe**；只有 `--wheel` 配 `--exe` 才是"两者都出"。
- **正确用法**：

  | 想要 | 命令 |
  |---|---|
  | 只出两个 exe（默认） | `uv run python build_exe.py` |
  | **两者都出** | `uv run python build_exe.py --wheel --exe` |
  | 只出 wheel | `uv run python build_exe.py --wheel` |

- **别改回去**：发版时若只想要 exe，就**别对外说 wheel 已更新**；对外给 wheel 前先用 RELEASE.md 第 3–4 步校验 `dist/` 里确实有对应的 `.whl`。

### E4. 打包产物自报的版本号是兜底值（不是真实版本）

- **症状**：`autosub.exe --version` 打印的不是 `pyproject.toml` 的版本，或直接打印 `core._FALLBACK_VERSION`。
- **根因**：`core._detect_version()` 靠 `importlib.metadata.version("autosub")` 读版本；如果打包时没把 **autosub 自己的包元数据**带进 exe，冻结后读不到，只能退回兜底常量。
- **现有修法**：`build_exe.py` 的 `METADATA` 列表第一项就是 `"autosub"`，会把 `autosub-<版本>.dist-info` 打进 `_internal/`。
- **验证方法**：看 `_internal/autosub-<版本>.dist-info/METADATA` 是否存在；或直接跑
  `uv run python tests/smoke.py --exe-dir dist/autosub-<版本>-<平台标签>`（第 9 节专门验这个）。
- **别改回去**：不要把 `"autosub"` 从 `METADATA` 里删掉 —— 删了不会报错，只会让版本号静默变成兜底值。

---

## F. GUI 运行时

### F1. 任务永远卡在"排队中"

- **症状**：日志只有"收到视频"一条，状态永远是"排队中"。
- **根因**：GUI 只有**一个工作线程**（`gui.py:151`，`autosub-worker`）。前一个任务卡住不结束，后面的任务永远排不上。历史上第一次踩是"旧版服务里一个 CUDA 任务卡死在准备阶段"。
- **排查顺序**：① 确认浏览器连的是**新装的那个服务**（`/api/doctor` 看 `version`）；② 看前一个任务的状态是不是卡住；③ 端口被两个服务共用时请求会随机分流（见 F2）。
- **注意**：这不只是显示问题 —— 详细日志只在任务真正开跑后才写，排队中的任务本来就只有一条日志。

### F2. 两个服务悄悄共用同一个端口

- **症状**：行为诡异、像"状态随机错乱"；实际上请求被分到了两个不同进程。
- **根因**：Windows 下 `SO_REUSEADDR` 允许**第二个进程绑定已被监听的端口**（类 Unix 的语义相反，那边是为 TIME_WAIT 快速重绑）。
- **现有修法**：`Server.allow_reuse_address = os.name != "nt"`（gui.py:518），`_free_port()`（gui.py:573）探测时同样不带 `SO_REUSEADDR`。
- **别改回去**：不要在 Windows 上恢复 `allow_reuse_address = True`。第二实例现在会明确报端口占用并自动顺延，这是期望行为。

### F3. 换了 HTML/JS 之后页面没变化

- **原因**：① 浏览器缓存（要 Ctrl+F5）；② 页面是 `webui.PAGE` 常量，**改了要重启服务**才会生效（没有热重载）。
- **排查**：看 GUI 顶部版本徽标（来自 `/api/doctor`）确认打开的是不是新版本。

### F4. 全局日志 sink 与单线程假设

- `set_log_sink()` 是**进程级全局**的（`core.py:49`）。当前"单工作线程 + 一次一个任务"的模型下没问题；**将来若改成并发多任务，必须先把日志 sink 改成按任务隔离**，否则日志会串台。
