# AGENTS.md

给 AI 协作者（Claude Code / CodeBuddy / 其他 agent）的项目说明。**动手前先读完本文件，再按需读下面三份文档。**

## 这是什么

`autosub` —— 跨平台视频自动加字幕工具：本地 faster-whisper 转写 + ffmpeg 烧录/软字幕。同一套代码提供 CLI 和浏览器 GUI（标准库 `http.server`）。

## 文档索引（按需要读）

| 文档 | 什么时候读 |
|---|---|
| `README.md` | 要回答"怎么用 / 参数是什么" |
| `CONTRACT.md` | **要改代码之前必读**：不可协商约束、影响矩阵、版本号真源、产物命名 |
| `GOTCHAS.md` | 遇到怪现象、要动 ffmpeg/PyAV/PyInstaller/huggingface_hub/GPU 相关代码时 |
| `RELEASE.md` | 要出 wheel / exe，或改了版本号 |
| `tests/smoke.py` | 改完跑一次：`uv run python tests/smoke.py`（约 1 分钟，离线可跑） |

## 硬约束（违反 = 改坏了）

1. 纯 Python 3.10–3.13，Windows / macOS / Linux 一套代码。
2. **前端零构建**：HTML/CSS/JS 只能是 `src/autosub/webui.py` 里的字符串常量。不要新建 `.html`、不要引 CDN。
3. GUI 只用标准库，不引入 Web 框架。
4. wheel 与 exe 行为等价 —— 共用 `core.run_job()`，不要出现"只有 exe 才有"的功能。
5. 不向上抛裸异常：给 `AutosubError` 或 `JobResult(ok=False, message=...)`；日志走 `info/step/warn/die`，不要 `print()`。
6. 所有面向用户的文案、注释、日志用中文，说人话并给出下一步（不要暴露 `IndexError` 这类异常名）。

## 常用命令

```bash
uv run autosub 视频.mp4                 # CLI 处理（默认烧录，输出到原视频目录）
uv run autosub doctor                   # 环境自检：设备 / ffmpeg / 字体 / 模型缓存
uv run autosub download-model tiny -o ./mdl   # 预下载模型（离线部署用）
uv run autosub-gui                      # 浏览器界面，默认 http://127.0.0.1:8765
uv run python build_exe.py --wheel --exe      # 同时出 wheel 和两个 exe
uv run python build_exe.py --smoke-only       # 只对已有产物跑自检
uv run python tests/smoke.py                  # 项目自检（离线可跑，约 1 分钟）
```

## 改代码的固定动作

1. 先看 `CONTRACT.md` 第 4 节的「改 A 必改 B」影响矩阵，确认要同步哪些文件。
2. 改完跑 `uv run python tests/smoke.py`（含版本号一致性、前端 id 引用、语法、烧录直测、GUI 全链路）。
3. 逻辑变了就**真跑一遍**验证（`tests/smoke.py --video 某.mp4` 走成功路径），不要只依赖静态检查。
4. 踩到新坑就补进 `GOTCHAS.md`；改了对外行为就更新 `README.md`。

> `tests/smoke.py` 里每节都对应一条历史事故（见文件顶部 docstring）。加新检查时保持这个习惯：
> 写清楚它防的是哪一类故障。

## 环境约定（重要）

- **环境类操作由用户自己执行**：建 venv、装包、起长驻服务。AI 只给命令，不要代劳。
- 用户 venv 在 `autosub/test/test/`（嵌套一层，**不可移动或改名**）。
  ⚠️ `test/`（单数）是 venv，`tests/`（复数）才是测试代码，别搞混、别删错。
- GUI 是长驻服务，不要用后台任务方式启动（会被回收）；由用户在自己的终端里起。
  `tests/smoke.py` 里的 GUI 是短命进程（自己起、自己停），不受此限。
- `dist/` 按版本号分目录产物，不要删除用户已有的 `.whl`（用户会拿它装）。

## 当前版本

唯一真源是 `pyproject.toml` 的 `version`；`core.py` 通过包元数据读回来（`_FALLBACK_VERSION` 只是兜底）。
发版只改 `pyproject.toml`，然后跑 `tests/smoke.py` 校验，见 `CONTRACT.md` 第 3 节。
