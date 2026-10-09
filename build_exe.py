#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""autosub 打包脚本。

    uv run python build_exe.py               # 生成当前平台的可执行程序（两个 exe）
    uv run python build_exe.py --wheel       # 生成 python wheel
    uv run python build_exe.py --sdist       # 生成源码包
    uv run python build_exe.py --wheel --exe # 两个都要
    uv run python build_exe.py --onefile     # 退回单文件（一个 exe 承担 CLI+GUI）
    uv run python build_exe.py --no-smoke    # 跳过构建后的自检

产物（默认，分目录模式）：
    dist/autosub-<平台标签>/autosub[.exe]       命令行
    dist/autosub-<平台标签>/autosub-gui[.exe]   浏览器界面
    dist/autosub-<平台标签>/_internal/           两者共享的依赖（只存一份）

产物（--onefile 单文件模式）：
    dist/autosub[.exe]                          一个二进制：autosub 视频.mp4 / autosub gui
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
WORK = ROOT / "build"
PKG = ROOT / "packaging"
ENTRY = PKG / "entry.py"            # 多态入口（CLI+GUI，用于 --onefile）
ENTRY_CLI = PKG / "entry_cli.py"    # 纯 CLI
ENTRY_GUI = PKG / "entry_gui.py"    # 纯 GUI

APP = "autosub"
GUI_APP = "autosub-gui"


def log(msg: str = "") -> None:
    print(msg, flush=True)


def head(msg: str) -> None:
    log()
    log("=" * 66)
    log(msg)
    log("=" * 66)


def platform_tag() -> str:
    m = platform.machine().lower()
    arch = {"amd64": "x86_64", "x86_64": "x86_64", "arm64": "arm64", "aarch64": "arm64",
            "x86": "x86", "i386": "x86", "i686": "x86"}.get(m, m or "unknown")
    if sys.platform.startswith("win"):
        return f"win-{arch}"
    if sys.platform == "darwin":
        return f"macos-{arch}"
    return f"linux-{arch}"


def exe_suffix() -> str:
    return ".exe" if sys.platform.startswith("win") else ""


def read_version() -> str:
    """从 pyproject.toml 读版本号，用于产物目录名。"""
    try:
        import tomllib
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        return str(data["project"]["version"])
    except Exception:
        return "0.0.0"


def default_name() -> str:
    """产物目录名：autosub-<版本>-<平台标签>（和 wheel 的命名风格一致）。"""
    return f"{APP}-{read_version()}-{platform_tag()}"


def run(cmd: list[str], **kw) -> int:
    log("$ " + " ".join(str(c) for c in cmd))
    return subprocess.call([str(c) for c in cmd], cwd=str(ROOT), **kw)


def need_pyinstaller() -> str | None:
    try:
        import PyInstaller  # type: ignore
        return getattr(PyInstaller, "__version__", "?")
    except Exception:
        return None


# --------------------------------------------------------------------------
# 可执行程序
# --------------------------------------------------------------------------

# 这些包带二进制扩展 / 数据文件，必须整套搬过去，否则运行时报缺 DLL 或缺模型资源
COLLECT_ALL = [
    "faster_whisper",   # 内含 silero VAD 的 onnx 资源
    "ctranslate2",      # 转写核心 + 各类 DLL
    "av",               # PyAV，ffmpeg 解码库
    "tokenizers",       # 分词器原生扩展
    "onnxruntime",      # VAD 推理
    "huggingface_hub",  # 模型下载
]

HIDDEN = [
    "faster_whisper", "ctranslate2", "av", "onnxruntime", "tokenizers",
    "huggingface_hub", "numpy",
    "autosub", "autosub.cli", "autosub.gui", "autosub.webui",
]

# 体积大又用不到的
EXCLUDES = [
    "tkinter", "matplotlib", "pandas", "scipy", "IPython", "pytest",
    "PyQt5", "PySide2", "PySide6", "torch", "torchaudio", "tensorflow",
]

# autosub 自己也要带上：core._detect_version() 靠 importlib.metadata 读版本，
# 不带元数据的话冻结后只能退回 core._FALLBACK_VERSION。
METADATA = ["autosub", "faster-whisper", "ctranslate2", "huggingface-hub", "tokenizers",
            "onnxruntime", "numpy"]


def collect_all_list() -> list[str]:
    """COLLECT_ALL + 装了才带的 imageio-ffmpeg。"""
    mods = list(COLLECT_ALL)
    try:
        import imageio_ffmpeg  # type: ignore  # noqa: F401
        mods.append("imageio_ffmpeg")
        log("已包含 imageio-ffmpeg（没装系统 ffmpeg 时的兜底）")
    except Exception:
        log("未安装 imageio-ffmpeg，可执行程序将使用 PATH 里的 ffmpeg")
    return mods


def _prepare_dirs(name: str) -> tuple[Path, Path, Path]:
    """先把目录建好，并且不用 PyInstaller 的 --clean：
    某些版本里 CONF['workpath'] 会被拼上 spec 名（workpath/<name>），而创建目录那一步
    可能只建到上一级，于是写 base_library.zip 时报 FileNotFoundError。"""
    workpath = WORK / "pyinstaller"
    specpath = WORK / "spec"
    shutil.rmtree(workpath, ignore_errors=True)
    for p in (DIST, workpath, specpath, workpath / name):
        p.mkdir(parents=True, exist_ok=True)
    return workpath, specpath, workpath / name


SPEC_TEMPLATE = '''# -*- mode: python ; coding: utf-8 -*-
"""由 build_exe.py 自动生成 —— 一次构建产出两个可执行文件，共享同一份依赖目录。

    autosub[.exe]      命令行
    autosub-gui[.exe]  浏览器界面（HTTP 服务 + 自动开浏览器）
"""
from PyInstaller.utils.hooks import collect_all, copy_metadata

SRC = r"@@SRC@@"
ENTRY_CLI = r"@@ENTRY_CLI@@"
ENTRY_GUI = r"@@ENTRY_GUI@@"

COLLECT_ALL = @@COLLECT_ALL@@
HIDDEN = @@HIDDEN@@
EXCLUDES = @@EXCLUDES@@
METADATA = @@METADATA@@
ICON = @@ICON@@
GUI_CONSOLE = @@GUI_CONSOLE@@
DIR_NAME = @@DIR_NAME@@

datas, binaries, hiddenimports = [], [], []
for _pkg in COLLECT_ALL:
    _d, _b, _h = collect_all(_pkg)
    datas += _d
    binaries += _b
    hiddenimports += _h
for _pkg in METADATA:
    try:
        datas += copy_metadata(_pkg)
    except Exception:
        pass
hiddenimports += HIDDEN


def _analyse(script):
    return Analysis(
        [script],
        pathex=[SRC],
        binaries=binaries,
        datas=datas,
        hiddenimports=hiddenimports,
        hookspath=[],
        hooksconfig={},
        runtime_hooks=[],
        excludes=EXCLUDES,
        noarchive=False,
    )


a_cli = _analyse(ENTRY_CLI)
pyz_cli = PYZ(a_cli.pure)

a_gui = _analyse(ENTRY_GUI)
pyz_gui = PYZ(a_gui.pure)

# 两个 EXE 共用依赖：MERGE 会把重复的二进制/数据只保留一份
MERGE((a_cli, "autosub", "autosub"), (a_gui, "autosub-gui", "autosub-gui"))

_COMMON = dict(
    exclude_binaries=True,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

exe_cli = EXE(pyz_cli, a_cli.scripts, [],
              name="autosub", console=True, icon=ICON, **_COMMON)

exe_gui = EXE(pyz_gui, a_gui.scripts, [],
              name="autosub-gui", console=GUI_CONSOLE, icon=ICON, **_COMMON)

coll = COLLECT(
    exe_cli,
    exe_gui,
    a_cli.binaries,
    a_cli.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=DIR_NAME,
)
'''


def write_spec(name: str, specpath: Path, icon: str | None, gui_console: bool) -> Path:
    spec = SPEC_TEMPLATE
    repl = {
        "@@SRC@@": str(ROOT / "src"),
        "@@ENTRY_CLI@@": str(ENTRY_CLI),
        "@@ENTRY_GUI@@": str(ENTRY_GUI),
        "@@COLLECT_ALL@@": repr(collect_all_list()),
        "@@HIDDEN@@": repr(HIDDEN),
        "@@EXCLUDES@@": repr(EXCLUDES),
        "@@METADATA@@": repr(METADATA),
        "@@ICON@@": repr(str(icon) if icon else None),
        "@@GUI_CONSOLE@@": repr(bool(gui_console)),
        "@@DIR_NAME@@": repr(name),
    }
    for k, v in repl.items():
        spec = spec.replace(k, v)
    specfile = specpath / f"{APP}.spec"
    specfile.write_text(spec, encoding="utf-8")
    return specfile


def build_exe(args) -> list[Path] | None:
    head("构建可执行程序（PyInstaller）")

    ver = need_pyinstaller()
    if ver is None:
        log("[错误] 当前环境没有 PyInstaller。先同步依赖再重试：")
        log("    uv sync          # 会自动装上 dev 组里的 pyinstaller")
        return None
    log(f"PyInstaller {ver} · Python {sys.version.split()[0]} · {platform_tag()}")

    for f in (ENTRY_CLI, ENTRY_GUI):
        if not f.exists():
            log(f"[错误] 找不到入口脚本 {f}")
            return None

    if args.onefile:
        return build_onefile(args)
    return build_two(args)


def build_two(args) -> list[Path] | None:
    """一次构建，两个 exe，共享一份 _internal。"""
    name = args.name or default_name()
    workpath, specpath, _ = _prepare_dirs(name)
    specfile = write_spec(name, specpath, args.icon, gui_console=not args.windowed)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--distpath", str(DIST),
        "--workpath", str(workpath),
        "--log-level", "WARN",
        str(specfile),
    ]

    t0 = time.time()
    if run(cmd) != 0:
        log("[错误] PyInstaller 失败。")
        return None

    outdir = DIST / name
    cli_exe = outdir / f"{APP}{exe_suffix()}"
    gui_exe = outdir / f"{GUI_APP}{exe_suffix()}"
    missing = [p.name for p in (cli_exe, gui_exe) if not p.exists()]
    if missing:
        log(f"[错误] 缺少预期产物：{', '.join(missing)}（目录 {outdir}）")
        return None

    log(f"\n完成，用时 {time.time() - t0:.1f}s")
    log(f"  目录：{outdir}  （共 {human(outdir)}，两个程序共享 _internal/）")
    log(f"    命令行    {cli_exe}")
    log(f"    浏览器界面 {gui_exe}")

    if not args.no_smoke:
        smoke_test([cli_exe, gui_exe], False)
    return [cli_exe, gui_exe]


def build_onefile(args) -> list[Path] | None:
    """单文件模式：一个二进制承担 CLI + GUI（autosub gui）。"""
    log("\n[单文件模式] 只产出一个可执行文件，用子命令区分角色。")
    name = args.name or f"{APP}-{platform_tag()}"
    workpath, specpath, _ = _prepare_dirs(name)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--name", name,
        "--onefile",
        "--distpath", str(DIST),
        "--workpath", str(workpath),
        "--specpath", str(specpath),
        "--paths", str(ROOT / "src"),
        "--log-level", "WARN",
    ]
    if args.windowed:
        cmd.append("--windowed")
    if args.icon:
        cmd += ["--icon", str(args.icon)]

    for mod in collect_all_list():
        cmd += ["--collect-all", mod]
    for mod in HIDDEN:
        cmd += ["--hidden-import", mod]
    for mod in EXCLUDES:
        cmd += ["--exclude-module", mod]
    for pkg in METADATA:
        cmd += ["--copy-metadata", pkg]

    cmd.append(str(ENTRY))

    t0 = time.time()
    if run(cmd) != 0:
        log("[错误] PyInstaller 失败。")
        return None

    exe = DIST / f"{name}{exe_suffix()}"
    if not exe.exists():
        log(f"[错误] 没有找到预期产物：{exe}")
        return None

    log(f"\n完成，用时 {time.time() - t0:.1f}s · 产物 {exe}（{human(exe)}）")
    if not args.no_smoke:
        smoke_test([exe], True)
    return [exe]


def smoke_test(exes: list[Path], onefile: bool = False) -> bool:
    """构建后自检：CLI 跑 doctor，GUI 真的把服务起起来探一次接口。"""
    head("自检：确认打出来的程序真能跑")
    ok = True
    for exe in exes:
        if "gui" in exe.stem:
            ok = smoke_gui(exe) and ok
        else:
            ok = smoke_cli(exe, onefile) and ok
        log("")

    if not ok:
        log("[警告] 至少一个程序没通过自检。")
        return False

    log("自检通过：两个程序都能独立跑起来。")
    for exe in exes:
        if "gui" in exe.stem:
            log(f"  浏览器界面：双击 {exe.name}，或 {exe} --port 9000")
        else:
            log(f"  命令行    ：{exe} 视频.mp4")
    return True


def smoke_cli(exe: Path, onefile: bool = False) -> bool:
    cmd = [str(exe), "doctor"]
    log("$ " + " ".join(cmd))
    try:
        rc = subprocess.call(cmd, cwd=str(exe.parent))
    except OSError as e:
        log(f"[警告] 无法执行：{e}")
        return False
    if rc != 0:
        log(f"[警告] 退出码 {rc}，看上面的输出定位缺了什么。")
        if onefile:
            log("  单文件模式常见问题是杀软误杀或首次启动解压慢，可先试分目录模式（去掉 --onefile）。")
        return False
    return True


def smoke_gui(exe: Path) -> bool:
    """GUI 的 doctor 不在命令行里 —— 直接把 HTTP 服务起起来，探 /api/doctor。"""
    import json
    import socket
    import tempfile
    import urllib.request

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    jobs = Path(tempfile.mkdtemp(prefix="autosub-smoke-"))
    cmd = [str(exe), "--no-browser", "--host", "127.0.0.1",
           "--port", str(port), "--jobs-dir", str(jobs)]
    log("$ " + " ".join(cmd))
    try:
        proc = subprocess.Popen(cmd, cwd=str(exe.parent),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as e:
        log(f"[警告] 无法执行：{e}")
        return False

    url = f"http://127.0.0.1:{port}/api/doctor"
    info: dict | None = None
    deadline = time.time() + 45
    try:
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(url, timeout=2) as r:
                    info = json.loads(r.read().decode("utf-8"))
                break
            except Exception:
                time.sleep(0.5)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(jobs, ignore_errors=True)

    if info is None:
        log(f"[警告] 浏览器界面没能在 45 秒内起来（端口 {port}）。")
        return False

    log(f"服务已就绪：http://127.0.0.1:{port}/")
    log(f"  {info.get('app')} {info.get('version')} · 设备 {info.get('device')}"
        f" · 模型 {info.get('model')} · ffmpeg {info.get('ffmpeg_tag')}"
        f" · 可烧录 {'是' if info.get('can_burn') else '否'}"
        f" · 字体 {info.get('font')}")
    return True


def find_built() -> list[Path]:
    """在 dist/ 里找最近一次构建出来的可执行程序（给 --smoke-only 用）。"""
    tag = platform_tag()
    dirs = [p for p in DIST.glob(f"{APP}-*-{tag}") if p.is_dir()]
    dirs += [p for p in DIST.glob(f"{APP}-{tag}") if p.is_dir()]
    if not dirs:
        return []
    d = max(dirs, key=lambda p: p.stat().st_mtime)
    exes = [d / f"{APP}{exe_suffix()}", d / f"{GUI_APP}{exe_suffix()}"]
    exes = [p for p in exes if p.exists()]
    if not exes:
        one = d / f"{APP}-{tag}{exe_suffix()}"
        if one.exists():
            exes = [one]
    return exes


def human(p: Path) -> str:
    try:
        if p.is_file():
            n = p.stat().st_size
        else:
            n = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    except OSError:
        return "未知"
    x = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if x < 1024 or unit == "GB":
            return f"{x:.1f}{unit}"
        x /= 1024
    return f"{x:.1f}GB"


# --------------------------------------------------------------------------
# wheel / sdist
# --------------------------------------------------------------------------

def build_dist(args) -> bool:
    kinds = []
    if args.wheel:
        kinds.append("--wheel")
    if args.sdist:
        kinds.append("--sdist")
    head(f"构建发布包（{', '.join(kinds)}）")

    DIST.mkdir(exist_ok=True)
    before = {p.name for p in list(DIST.glob("*.whl")) + list(DIST.glob("*.tar.gz"))}

    # 首选 uv build（本来就是 uv 项目，不用额外装 build）
    uv = shutil.which("uv")
    if uv:
        cmd = [uv, "build", *kinds, "--out-dir", str(DIST)]
        log(f"用 uv build：{' '.join(kinds)}")
    else:
        cmd = [sys.executable, "-m", "build", *kinds, "--outdir", str(DIST)]
        log("没有 uv，改用 python -m build")

    if run(cmd) != 0:
        log("[错误] 构建发布包失败。")
        log("  如果提示缺少 build 模块：uv sync（dev 组里已包含 build）")
        return False

    artifacts = sorted(list(DIST.glob("*.whl")) + list(DIST.glob("*.tar.gz")),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    if not artifacts:
        log("\n[错误] dist/ 里没有 .whl / .tar.gz，构建没成功。")
        return False

    log("\n产物（dist/ 下，按时间倒序）：")
    for p in artifacts[:6]:
        mark = "新构建" if p.name not in before else "已存在"
        log(f"  {p}  ({human(p)})  [{mark}]")
    whl = next((p for p in artifacts if p.suffix == ".whl"), artifacts[0])
    log("\n安装与使用：")
    log(f'  pip install "{whl}"')
    log("  autosub 视频.mp4        # 命令行")
    log("  autosub-gui             # 浏览器界面")
    return True


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="build_exe.py",
        description="打包 autosub：可执行程序（默认两个 exe）和/或 python 包",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  uv run python build_exe.py             # 当前平台：autosub + autosub-gui\n"
            "  uv run python build_exe.py --wheel     # wheel\n"
            "  uv run python build_exe.py --onefile   # 单文件（一个 exe 两种模式）\n"
        ),
    )
    p.add_argument("--wheel", action="store_true", help="构建 wheel（.whl）")
    p.add_argument("--sdist", action="store_true", help="构建源码包（.tar.gz）")
    p.add_argument("--exe", action="store_true", help="和 --wheel/--sdist 一起用时，也构建可执行程序")
    p.add_argument("--onefile", action="store_true",
                   help="只出一个单文件 exe（autosub 视频.mp4 / autosub gui）；默认是两个 exe")
    p.add_argument("--windowed", action="store_true",
                   help="GUI 程序不显示控制台窗口（默认显示，方便看地址和日志）")
    p.add_argument("--name", default=None, help="产物目录名，默认 autosub-<平台标签>")
    p.add_argument("--icon", default=None, help="图标文件（.ico / .icns / .png）")
    p.add_argument("--no-smoke", action="store_true", help="构建后不跑 doctor 自检")
    p.add_argument("--smoke-only", action="store_true",
                   help="不重新构建，只对 dist/ 里已有的产物跑一次自检")
    p.add_argument("--keep-build", action="store_true", help="保留 build/ 中间目录")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # 平台信息先打出来，方便确认"这是本平台专用产物"
    log(f"{APP} 打包 · 目标平台：{platform_tag()} · Python {sys.version.split()[0]}")

    if args.smoke_only:
        exes = find_built()
        if not exes:
            log("dist/ 里没找到已构建的可执行程序。先跑一次 uv run python build_exe.py。")
            return 1
        log("对已有产物跑自检：" + "、".join(p.name for p in exes))
        return 0 if smoke_test(exes, args.onefile) else 1

    ok = True
    packages_only = (args.wheel or args.sdist) and not args.exe

    if packages_only:
        ok = build_dist(args)
    else:
        exes = build_exe(args)
        ok = bool(exes)
        if (args.wheel or args.sdist) and exes:
            ok = build_dist(args) and ok

    if not args.keep_build and WORK.exists():
        shutil.rmtree(WORK, ignore_errors=True)

    if args.wheel or args.sdist:
        log("\n提示：wheel 是纯 Python 包（各平台通用，装了才有依赖）；")
        log("      可执行程序是平台专用的（Windows 出的 .exe 只能在 Windows 跑）。")
    if not packages_only:
        log("可执行程序只在本平台可用；换平台要在那台机器上重新执行一次本脚本。")

    log("\n全部完成。" if ok else "\n有步骤失败，见上面的输出。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
