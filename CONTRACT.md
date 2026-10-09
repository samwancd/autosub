# 变更契约（CONTRACT）

改动 autosub 之前先读这份。它只讲**规则**，不讲用法：

- 用法 / 安装 / 参数 → `README.md`
- 已知的坑 → `GOTCHAS.md`
- 发版步骤 → `RELEASE.md`

---

## 1. 不可协商的约束

这 6 条是设计前提，改任何一条都要先想清楚代价，不要顺手破掉。

| # | 约束 | 原因 |
|---|---|---|
| 1 | 纯 Python 3.10–3.13，一套代码跑 Windows / macOS / Linux | 这是项目立项时的硬要求。任何平台专属实现都必须有跨平台分支与兜底 |
| 2 | **前端零构建**：HTML / CSS / JS 只能是 `webui.py` 里的字符串常量 | 打包成 exe 后没有任何模板/静态文件依赖，一个二进制就够。禁止引入外部 `.html`、禁止引 CDN |
| 3 | GUI 只用标准库 `http.server`，不引 Flask / FastAPI | 同上，也是 exe 体积与依赖可控的前提 |
| 4 | wheel 与 exe **行为必须等价** | 两条分发路径共用 `core.run_job()`；不允许"只有 exe 才有"的功能 |
| 5 | 联网只能用于下载模型，且必须可关 | `--offline` / `HF_HUB_OFFLINE=1` 下要能跑完全流程（内网/离线机器） |
| 6 | 不向上抛裸异常 | CLI/GUI 统一消费 `AutosubError` 或 `JobResult(ok=False, message=...)`，日志一律走 `info/step/warn/die` |

---

## 2. 分层与唯一入口

```
cli.py   ┐
gui.py   ┴──▶ core.run_job(video, Settings, ffmpeg, probe, on_progress)  ──▶ JobResult
webui.py ┘        （唯一处理入口：转写 → 断句 → 烧录/软字幕）
```

- `core.py` 是**唯一**的计算与决策中心：设备选择、模型选择、断句、字体、ffmpeg 调用都在这里。
- `cli.py` / `gui.py` 只负责：解析参数 → 填 `Settings` → 收进度 → 展示结果。
- `webui.py` 只负责渲染，不含业务判断。

**推论**：任何"聪明"的判断（比如"这个设备该用什么模型"）必须写在 `core.py`。写在 `cli.py`/`gui.py` 里就等于复制了两份规则，早晚不一致。

---

## 3. 版本号真源（单一来源）

**唯一要改的地方是 `pyproject.toml` 的 `version`**，`core.py` 不再手写版本号：

```python
VERSION = _detect_version()     # importlib.metadata.version("autosub")
```

| 文件 | 作用 |
|---|---|
| `pyproject.toml` 的 `version` | **真源**，发版只改这一处 |
| `core.py` 的 `_FALLBACK_VERSION` | 兜底常量，**只在包元数据不可用时生效**（直接 `PYTHONPATH=src` 跑源码、元数据缺失的冻结环境）。正常运行（源码安装 / wheel / exe）都走元数据，所以忘改它不会导致报错版本 |

- `build_exe.py` 不硬编码版本（读 `pyproject.toml` 生成产物目录名）。
- 打包时 `METADATA` 列表里带了 `"autosub"`，**exe 里也有包元数据**，所以冻结后同样能读到正确版本。
- `tests/smoke.py` 第 1 节会校验 `pyproject.toml == core.VERSION`；发版前跑一次即可（见 `RELEASE.md`）。


---

## 4. "改 A 必改 B" 影响矩阵

改下面任一位置时，对照右列逐项确认。**漏掉的通常不会报错，只会静默不一致。**

| 改了这个 | 必须同步这些 |
|---|---|
| `core.py` 的 `Settings` 字段 | `cli.py` 参数、`gui.py` 的 `build_parser()`、`gui.py` 的 `_settings_for()`、`webui.py` 表单、README 参数表 |
| `core.py` 的 `resolve_model` / `resolve_device` / `resolve_compute_type` | `gui.py` 里复制了一份同样的调用（任务列表「模型」列与 `doctor` 用），**两处必须同规则** |
| `core.py` 的 `_FALLBACK_VERSION` | `pyproject.toml` 的 `version`（见第 3 节；正常运行不影响，但别放任漂移） |
| `webui.py` 新增/删除元素 `id` | `webui.py` 内的 JS `$("...")` 引用，以及 RELEASE.md 第 2 步的 id 一致性检查 |
| `gui.py` 返回的 JSON 字段 | `webui.py` 的渲染逻辑 + `gui.py` 顶部 docstring 的接口一览 + README 第 4 节的接口表 |
| 新增/改动 HTTP 接口 | `gui.py` 顶部 docstring 接口一览 + README 接口表（**两处都要**） |
| 新增 CLI 子命令 / 参数 | `cli.py` 的 `doctor` 输出 + README 第 2 节 |
| 新增第三方 import | `build_exe.py` 的 `COLLECT_ALL` / `HIDDEN` / `METADATA` 三个列表，否则 exe 运行时缺模块 |
| 新增运行时数据文件 | 不要新增！按约束 2 内联进 `.py`（现有做法：`webui.PAGE`） |
| 增删模型 | `MODEL_CHOICES`、`MODEL_SIZES_MB`、`_FALLBACK_REPOS`、`repo_id_for()` **四处** |
| 改产物命名 | `core.pick_output()` + README + GUI 提示文案 |

---

## 5. 产物契约

| 模式 | 输出 | 命名 |
|---|---|---|
| `burn`（默认） | 烧录字幕的视频 | `<原名>_subtitled.mp4` |
| `soft` | 软字幕（字幕轨）的视频 | `<原名>_subbed.<原扩展名>` |
| `srt` | 仅字幕 | `<原名>.srt` |

- 字幕文件**永远**叫 `<原名>.srt`（三种模式都一样），GUI 的下载链接依赖这个约定。
- CLI 默认输出到**原视频所在目录**；GUI 默认输出到任务目录 `~/.autosub/jobs/<日期>-<时间>_<6位ID>/`。
- 任务目录位置可用 `--jobs-dir` 或环境变量 `AUTOSUB_JOBS_DIR` 改。

---

## 6. 代码风格约定（沿用现状，别引入新范式）

- 注释、日志、错误文案**全部中文**，面向使用者而不是开发者。
- 日志用 `info()` / `step()` / `warn()` / `die()`，不要 `print()`；GUI 靠 `set_log_sink()` 把同一份日志抓到页面。
- 阶段日志固定写 `[1/3] / [2/3] / [3/3]` 前缀，README 和用户预期都依赖这个格式。
- 面向最终用户报错时**说人话 + 给下一步**：不要抛 `IndexError`、不要把异常类型写进提示。
  例：不写"tuple index out of range"，写"这个视频里没有音频轨道，无法转写字幕"。
