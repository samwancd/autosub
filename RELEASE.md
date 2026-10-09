# 发布流程（RELEASE）

按顺序执行，不要跳步。每步都有可复制的命令。**第 3 步是历史上真出过错的一步，不能省。**

---

## 0. 前置：环境干净

同一端口上同时跑两个 GUI 会导致"请求随机分流"的诡异现象（见 `GOTCHAS.md` F2）。发版测试前先确认只有一个实例：

```bash
# Windows：看谁占了 8765
netstat -ano | findstr :8765
tasklist | findstr /i "autosub python"
```

确认没有残留进程（尤其是**旧版本**的服务）再继续。

---

## 1. 改版本号（**只改一处**）

版本号唯一真源是 `pyproject.toml`（`core.py` 通过包元数据读回来，见 `CONTRACT.md` 第 3 节）：

```bash
# 以 1.2.8 为例
sed -i 's/^version = "[^"]*"/version = "1.2.8"/' pyproject.toml
```

- [ ] `pyproject.toml` 的 `version` 已改
- [ ] 顺手把 `core.py` 的 `_FALLBACK_VERSION` 也改成同值（只是兜底，不改不会有症状，但别放任漂移）
- [ ] 检查 README 里是否有写死的版本/产物路径示例需要同步（`grep -n 'autosub-1\.' README.md`）

---

## 2. 自检（**发版前必跑**）

```bash
uv run python tests/smoke.py                            # 约 1 分钟，离线可跑
uv run python tests/smoke.py --video "一条带人声的视频.mp4"   # 额外验证成功路径
```

`tests/smoke.py` 覆盖：版本号一致性、语法编译、前端 id 引用、界面元素存在性、依赖可导入、
CUDA 缺失时落 CPU 的不变量、音轨探测、**烧录直测（含空字幕退化）**、`run_job` 全流程不崩、
GUI 全链路（doctor 字段 / 任务终态 / 模型列 / 耗时列 / 产物）、**端口独占**。

- [ ] 输出末尾是「失败 0」（有失败项会逐条列出，并给出排查用的临时产物目录）
- [ ] 输出里有跳过项时看清楚原因（比如"没有 ffmpeg"，那说明本机环境不完整）

> 只看静态检查（版本号 + 前端 id + 语法）的话，直接看 `tests/smoke.py` 第 1、2 节即可；
> 想单独跑就用 `uv run python tests/smoke.py --skip-gui`。


---

## 3. 构建产物（**看清楚要出什么**）

`build_exe.py` 默认**只出两个 exe，不出 wheel**（`GOTCHAS.md` E3）。按需选命令：

```bash
uv run python build_exe.py --wheel --exe   # 出 wheel + 两个 exe（推荐：发版用这个）
uv run python build_exe.py                 # 只出两个 exe
uv run python build_exe.py --wheel         # 只出 wheel
uv run python build_exe.py --onefile       # 单文件 exe（一个程序两种模式）
```

构建结束会自动跑自检（CLI 跑 `doctor`，GUI 真起服务探 `/api/doctor`）。要单独重跑自检：

```bash
uv run python build_exe.py --smoke-only
```

- [ ] 控制台最后是「全部完成。」+「自检通过」（不是「有步骤失败」）

---

## 4. 核对 dist/（**这一步曾经漏过，务必执行**）

确认这次版本的两类产物都真的存在、且 wheel 里的 `Version` 与预期一致：

```bash
uv run python -c "
import zipfile, glob, pathlib
want = pathlib.Path('pyproject.toml').read_text().split('version = \"')[1].split('\"')[0]
for w in sorted(glob.glob('dist/*.whl')):
    z = zipfile.ZipFile(w)
    m = [n for n in z.namelist() if n.endswith('METADATA')][0]
    ver = next(l.split(': ')[1] for l in z.read(m).decode().splitlines() if l.startswith('Version:'))
    print(f'{pathlib.Path(w).name:40s} {ver}')
whl = f'dist/autosub-{want}-py3-none-any.whl'
d = f'dist/autosub-{want}-win-x86_64'
print()
print('wheel 存在 :', pathlib.Path(whl).exists(), whl)
print('exe 目录存在:', pathlib.Path(d).exists(), d)
"
```

- [ ] `dist/autosub-<版本>-py3-none-any.whl` 存在
- [ ] `dist/autosub-<版本>-<平台标签>/` 存在（Windows 上是 `win-x86_64`），目录里同时有 `autosub.exe`、`autosub-gui.exe`、`_internal/`
- [ ] **只把实际存在的文件给出**（不要对外声称 wheel 已更新却没构建）

---

## 5. 安装与验证（真实跑一遍）

```bash
# wheel 版（--python 指向目标 venv 的 python）
uv pip install --python "<venv>\Scripts\python.exe" dist/autosub-<版本>-py3-none-any.whl
<venv>\Scripts\autosub.exe --version      # 应打印本次版本
<venv>\Scripts\autosub.exe doctor         # 设备/ffmpeg/字体/模型缓存状态

# exe 版：直接双击 dist/autosub-<版本>-win-x86_64/autosub-gui.exe
# 或者让自检脚本替你验（会跑 exe 的 --version 并真起一次 GUI 服务）：
uv run python tests/smoke.py --exe-dir dist/autosub-<版本>-win-x86_64
```

- [ ] `--version` 输出与本次版本号一致（不一致 = 装到了旧的 / wheel 没更新，这是最容易出的一类事故）
- [ ] `--exe-dir` 自检通过（尤其看「exe 自报版本与 pyproject 一致」这一项——它验证的是打包时把 autosub 的包元数据也带进了 exe）
- [ ] GUI 页面顶部版本徽标显示新版本（若还是旧版本，Ctrl+F5 强刷）
- [ ] 提交一条真实视频，跑到 `done`，任务列表里「模型 / 耗时」两列有值，两侧预览都能点开放大

---

## 6. 收尾

- [ ] 关掉测试期间起的服务，避免下次发版又被旧服务混淆
- [ ] 若本次改了运行逻辑，在 `GOTCHAS.md` 里补一条新踩的坑（症状 → 根因 → 修法）
- [ ] 若本次改了对外接口/参数，同步 README 对应章节（见 `CONTRACT.md` 第 4 节影响矩阵）

---

## 附：产物对照

| 产物 | 命令 | 平台 | 说明 |
|---|---|---|---|
| `autosub-<版本>-py3-none-any.whl` | `--wheel` | 全平台通用 | 纯 Python 包，**不含** ffmpeg 和模型；装完脚本入口是 39KB 启动器，代码在 `site-packages` |
| `autosub-<版本>-<平台标签>/` | 默认 | **平台专用** | 两个 exe 共享 `_internal/`；分发必须给整个目录，不能只拷 exe |
| `autosub-<版本>.tar.gz` | `--sdist` | 全平台 | 源码包，含 `build_exe.py`、`packaging/`、文档 |

> wheel 和 exe 的依赖都由用户网络下载（faster-whisper 等），模型首次使用时下载。要交付**离线机器**，需另附模型目录并用 `-m <目录> --offline`（见 `GOTCHAS.md` B3）。
