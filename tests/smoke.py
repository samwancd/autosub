# -*- coding: utf-8 -*-
"""autosub 自检 / 冒烟测试（离线可跑，不需要真实视频）

用法::

    uv run python tests/smoke.py                    # 默认：静态检查 + 合成视频 + GUI 全链路
    uv run python tests/smoke.py --video 某.mp4      # 额外跑一遍真实视频的"成功路径"
    uv run python tests/smoke.py --exe-dir dist/autosub-1.2.7-win-x86_64   # 额外校验打包产物

退出码 0 = 全部通过；非 0 = 有失败项。

覆盖的是**历史上真出过问题的点**（对应 GOTCHAS.md）：
  * 版本号两处不同步 / wheel 装的是旧版          → 第 1、9 节
  * 前端改了 HTML 忘了改 JS                     → 第 2 节
  * 有显卡但缺 CUDA 运行库却选了 CUDA            → 第 3 节
  * 空 SRT 让 ffmpeg 的 subtitles 滤镜直接失败   → 第 5 节
  * 无音轨视频崩成"内部错误：tuple index..."      → 第 6 节
  * 两个 GUI 服务悄悄共用同一端口                → 第 8 节

注意：项目根目录的 ``test/`` 是开发者本机的 **venv**，不是测试代码；测试代码在 ``tests/``。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

import autosub.core as core  # noqa: E402

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []
SKIPPED: list[str] = []

BAD_MARKERS = ("内部错误", "IndexError", "tuple index", "Traceback", "KeyError", "AttributeError")


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------

def ok(name: str, detail: str = "") -> None:
    PASSED.append(name)
    print(f"  [通过] {name}" + (f"   {detail}" if detail else ""), flush=True)


def bad(name: str, detail: str = "") -> None:
    FAILED.append((name, detail))
    print(f"  [失败] {name}   {detail}", flush=True)


def skip(name: str, why: str = "") -> None:
    SKIPPED.append(name)
    print(f"  [跳过] {name}   {why}", flush=True)


def head(title: str) -> None:
    print(f"\n== {title}", flush=True)


def friendly(msg: str) -> bool:
    return not any(m in (msg or "") for m in BAD_MARKERS)


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def make_video(path: Path, with_audio: bool = True, seconds: float = 3.0, fps: int = 25) -> Path:
    """合成一段测试视频：固定色块画面 +（可选）300Hz 正弦音轨。"""
    import av
    import numpy as np

    frames = int(seconds * fps)
    c = av.open(str(path), "w")
    vs = c.add_stream("libx264", rate=fps)
    vs.width, vs.height, vs.pix_fmt = 320, 240, "yuv420p"

    a = None
    if with_audio:
        a = c.add_stream("aac", rate=16000)
        a.layout = "mono"
        sr = 16000
        t = np.arange(int(sr * seconds)) / sr
        pcm = (0.2 * np.sin(2 * np.pi * 300 * t) * 32767).astype(np.int16)
        af = av.AudioFrame.from_ndarray(pcm.reshape(1, -1), format="s16", layout="mono")
        af.sample_rate = sr
        for pkt in a.encode(af):
            c.mux(pkt)
        for pkt in a.encode():
            c.mux(pkt)

    for i in range(frames):
        arr = np.zeros((240, 320, 3), dtype=np.uint8)
        arr[:] = (30 + i, 60, 120)
        frame = av.VideoFrame.from_ndarray(arr, format="rgb24")
        for pkt in vs.encode(frame):
            c.mux(pkt)
    for pkt in vs.encode():
        c.mux(pkt)
    c.close()
    return path


def probe_duration(path: Path) -> float:
    try:
        import av
        with av.open(str(path)) as c:
            return float(c.duration or 0) / float(av.time_base)
    except Exception:
        return 0.0


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def http_json(base: str, path: str, data: bytes | None = None, method: str | None = None,
              ctype: str = "application/json", timeout: float = 60.0):
    req = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", ctype)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def start_gui(port: int, jobs_dir: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.Popen(
        [sys.executable, "-m", "autosub.gui", "--no-browser", "--host", "127.0.0.1",
         "--port", str(port), "--jobs-dir", str(jobs_dir)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)


def wait_doctor(base: str, timeout: float = 40.0) -> dict:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            return http_json(base, "/api/doctor", timeout=5)
        except Exception as e:  # noqa: BLE001
            last = str(e)
            time.sleep(0.4)
    raise RuntimeError(f"服务没起来（{base}）：{last}")


def stop(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def submit_job(base: str, video: Path, model: str = "tiny", mode: str = "burn",
               language: str | None = "zh") -> str:
    payload = {"name": video.name, "mode": mode, "model": model, "max_chars": 16,
               "font_size": 20, "margin_v": 28}
    if language:
        payload["language"] = language
    jid = http_json(base, "/api/jobs", data=json.dumps(payload).encode(), method="POST")["id"]
    http_json(base, f"/api/jobs/{jid}/file?name={quote(video.name)}",
              data=video.read_bytes(), method="PUT", ctype="application/octet-stream")
    http_json(base, f"/api/jobs/{jid}/start", data=b"", method="POST")
    return jid


def wait_job(base: str, jid: str, timeout: float = 300.0) -> dict:
    deadline = time.time() + timeout
    job: dict = {}
    while time.time() < deadline:
        job = http_json(base, f"/api/jobs/{jid}")
        if job.get("state") in ("done", "error"):
            return job
        time.sleep(1.0)
    return job


def model_unavailable(msg: str) -> bool:
    return any(k in (msg or "") for k in ("模型权重不可用", "权重不完整", "下载", "网络", "connect", "timed out"))


# --------------------------------------------------------------------------
# 1. 版本号
# --------------------------------------------------------------------------

def sec_version() -> None:
    head("1. 版本号（唯一真源 = pyproject.toml）")
    py = re.search(r'^version = "(.+?)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M)
    py_ver = py.group(1) if py else "?"
    src_ver = core.VERSION
    detail = f"pyproject={py_ver}  core.VERSION={src_ver}  兜底常量={core._FALLBACK_VERSION}"
    if py_ver == src_ver:
        ok("版本号一致", detail)
    else:
        bad("版本号不一致", detail)

    if core._FALLBACK_VERSION != py_ver:
        skip("兜底版本号与 pyproject 同步", f"兜底={core._FALLBACK_VERSION}（元数据可用时不影响，但建议同步）")
    else:
        ok("兜底版本号也已同步", core._FALLBACK_VERSION)


# --------------------------------------------------------------------------
# 2. 静态检查
# --------------------------------------------------------------------------

def sec_static() -> None:
    head("2. 静态检查（语法 + 前端 id 引用）")
    import py_compile

    files = ["src/autosub/core.py", "src/autosub/cli.py", "src/autosub/gui.py",
             "src/autosub/webui.py", "build_exe.py"]
    broken = []
    for f in files:
        try:
            py_compile.compile(str(ROOT / f), doraise=True)
        except Exception as e:  # noqa: BLE001
            broken.append(f"{f}: {e}")
    if broken:
        bad("语法编译", "; ".join(broken))
    else:
        ok("语法编译", f"{len(files)} 个文件")

    from autosub.webui import PAGE
    used = set(re.findall(r'\$\("([A-Za-z0-9_]+)"\)', PAGE)) | \
        set(re.findall(r'getElementById\("([A-Za-z0-9_]+)"\)', PAGE))
    defined = set(re.findall(r'id="([A-Za-z0-9_]+)"', PAGE))
    missing = used - defined
    if missing:
        bad("前端 id 引用完整", f"JS 引用了不存在的 id: {sorted(missing)}")
    else:
        ok("前端 id 引用完整", f"{len(used)} 个引用")

    for key, why in (("id=\"lb\"", "点击放大灯箱"), ("id=\"ver\"", "顶部版本徽标"),
                     ("<th>模型</th>", "任务列表模型列"), ("<th>耗时</th>", "任务列表耗时列")):
        if key in PAGE:
            ok(f"界面元素：{why}")
        else:
            bad(f"界面元素：{why}", f"页面里找不到 {key}")


# --------------------------------------------------------------------------
# 3. 依赖与设备决策
# --------------------------------------------------------------------------

def sec_env() -> None:
    head("3. 依赖与设备决策")
    import importlib

    for mod in ("faster_whisper", "av", "ctranslate2", "huggingface_hub", "numpy"):
        try:
            m = importlib.import_module(mod)
            ok(f"可导入 {mod}", f"v{getattr(m, '__version__', '?')}")
        except Exception as e:  # noqa: BLE001
            bad(f"可导入 {mod}", str(e))

    n, usable, missing = core.cuda_status()
    ok("CUDA 探测", f"设备 {n} 个，运行库{'齐备' if usable else '缺失（' + (missing or '无') + '）'}")
    if n > 0 and not usable:
        # GOTCHAS C1：有卡但缺 cublas/cudnn 时必须落 CPU，否则会在真正转写时才崩
        if core.resolve_device("auto") == "cpu":
            ok("有卡缺运行库 → auto 落 CPU", "不会在转写阶段才报 cublas 错误")
        else:
            bad("有卡缺运行库 → auto 应落 CPU", f"实际得到 {core.resolve_device('auto')}")
    else:
        skip("有卡缺运行库 → 落 CPU 的不变量", "本机没有触发该条件")

    # GOTCHAS B4：模型名 → 仓库名不是一对一
    mapping = {m: core.repo_id_for(m) for m in core.named_models()}
    if all(v for v in mapping.values()):
        ok("模型仓库映射齐全", " / ".join(f"{k}→{v.split('/')[-1]}" for k, v in list(mapping.items())[:3]) + " …")
    else:
        bad("模型仓库映射", f"有模型解析不出仓库：{[k for k, v in mapping.items() if not v]}")

    ff, tag = core.find_ffmpeg(None)
    probe = core.ffmpeg_probe(ff)
    if ff:
        ok("找到 ffmpeg", f"{tag} · libass={probe['libass']} subtitles={probe['subtitles_filter']}")
    else:
        bad("找到 ffmpeg", "未找到，烧录相关检查会跳过")
    font_file, family = core.detect_cjk_font()
    if font_file:
        ok("找到中文字体", f"{family} · {Path(font_file).name}")
    else:
        bad("找到中文字体", "未找到，画面里中文会变方块")


# --------------------------------------------------------------------------
# 4. 音轨探测
# --------------------------------------------------------------------------

def sec_audio_probe(tmp: Path) -> tuple[Path, Path]:
    head("4. 音轨探测（无音轨要能识别出来）")
    with_audio = make_video(tmp / "with_audio.mp4", with_audio=True)
    no_audio = make_video(tmp / "no_audio.mp4", with_audio=False)

    if core.has_audio_stream(with_audio):
        ok("带音轨视频被正确识别")
    else:
        bad("带音轨视频被正确识别", "has_audio_stream 返回 False")
    if not core.has_audio_stream(no_audio):
        ok("无音轨视频被正确识别")
    else:
        bad("无音轨视频被正确识别", "has_audio_stream 返回 True")
    return with_audio, no_audio


# --------------------------------------------------------------------------
# 5. 烧录步骤直测（不需要模型，最可能坏的一步）
# --------------------------------------------------------------------------

def sec_burn(tmp: Path, video: Path) -> None:
    head("5. 烧录步骤直测（手工 SRT，不依赖模型）")
    ff, tag = core.find_ffmpeg(None)
    if not ff:
        skip("烧录直测", "没有 ffmpeg")
        return
    probe = core.ffmpeg_probe(ff)
    if not probe.get("subtitles_filter"):
        skip("烧录直测", "当前 ffmpeg 缺 subtitles 滤镜（没有 libass），改用软字幕模式才可测")
        return

    font_file, family = core.detect_cjk_font()
    style = core.build_style(family or "sans-serif", 20, "FFFFFF", 2, 28)

    def burn(srt: Path, out: Path, name: str) -> None:
        wd = Path(tempfile.mkdtemp(prefix="smoke-burn-", dir=str(tmp)))
        try:
            core.burn_in(ff, video, srt, out, style, font_file, "libx264", "veryfast", 26,
                         False, wd, total_duration=3.0)
        finally:
            pass

    srt_ok = tmp / "two_cues.srt"
    core.write_srt([core.Cue(0.2, 1.4, "第一行中文字幕"), core.Cue(1.6, 2.8, "second line")],
                   srt_ok, bom=False)
    out_ok = tmp / "burned.mp4"
    try:
        burn(srt_ok, out_ok, "正常字幕")
        dur = probe_duration(out_ok)
        if out_ok.exists() and out_ok.stat().st_size > 0 and dur > 0:
            ok("烧录正常字幕", f"{core.human_size(out_ok.stat().st_size)} · 时长 {dur:.1f}s")
        else:
            bad("烧录正常字幕", f"产物异常：存在={out_ok.exists()} 时长={dur}")
    except Exception as e:  # noqa: BLE001
        bad("烧录正常字幕", f"{type(e).__name__}: {e}")

    # GOTCHAS D2：空 SRT 会让 subtitles 滤镜报 Unable to open，必须退化成空滤镜而不是失败
    srt_empty = tmp / "empty.srt"
    core.write_srt([], srt_empty, bom=False)
    out_empty = tmp / "burned_empty.mp4"
    try:
        burn(srt_empty, out_empty, "空字幕")
        if out_empty.exists() and out_empty.stat().st_size > 0:
            ok("空字幕不崩（退化处理）", core.human_size(out_empty.stat().st_size))
        else:
            bad("空字幕不崩（退化处理）", "没产出视频")
    except Exception as e:  # noqa: BLE001
        bad("空字幕不崩（退化处理）", f"{type(e).__name__}: {e}")


# --------------------------------------------------------------------------
# 6. run_job 全流程（不崩、给友好提示）
# --------------------------------------------------------------------------

def sec_run_job(tmp: Path, no_audio: Path, tone: Path) -> None:
    head("6. run_job 全流程（无音轨 / 无语音都必须给友好提示）")
    ff, _ = core.find_ffmpeg(None)
    probe = core.ffmpeg_probe(ff)
    outdir = tmp / "runjob"
    outdir.mkdir(exist_ok=True)

    # 6a. 无音轨：必须返回友好中文，而不是 "内部错误：tuple index out of range"
    s = core.Settings(model="tiny", device="cpu", mode="burn", outdir=str(outdir))
    try:
        r = core.run_job(no_audio, s, ff, probe)
        if (not r.ok) and "音频轨道" in r.message and friendly(r.message):
            ok("无音轨视频 → 友好提示", r.message)
        else:
            bad("无音轨视频 → 友好提示", f"ok={r.ok} message={r.message!r}")
    except Exception as e:  # noqa: BLE001
        bad("无音轨视频 → 友好提示", f"抛异常 {type(e).__name__}: {e}")

    # 6b. 有音轨但没有人声：要么友好地说明"没识别到语音"，要么正常出片，但绝不能崩
    logs: list[str] = []
    core.set_log_sink(lambda level, msg: logs.append(msg))
    try:
        s2 = core.Settings(model="tiny", device="cpu", mode="burn", outdir=str(outdir), language="zh")
        r = core.run_job(tone, s2, ff, probe)
        core.set_log_sink(None)
        if not friendly(r.message):
            bad("无语音视频不崩", f"message={r.message!r}")
            print("\n".join(f"      | {l}" for l in logs[-12:]), flush=True)
        elif r.cues == 0 and not r.ok:
            ok("无语音视频 → 友好提示", r.message)
        elif r.ok:
            ok("无语音视频 → 正常出片", f"{r.cues} 条字幕 · {r.message}")
        else:
            ok("无语音视频 → 未崩溃", f"ok={r.ok} {r.message}")
        # 字幕文件无论如何都应该落盘
        if Path(r.srt).exists():
            ok("字幕文件落盘", f"{Path(r.srt).name} ({core.human_size(Path(r.srt).stat().st_size)})")
        else:
            bad("字幕文件落盘", f"没找到 {r.srt}")
    except core.AutosubError as e:
        core.set_log_sink(None)
        skip("无语音视频不崩", f"模型不可用：{e}")
    except Exception as e:  # noqa: BLE001
        core.set_log_sink(None)
        bad("无语音视频不崩", f"抛异常 {type(e).__name__}: {e}")


# --------------------------------------------------------------------------
# 7. GUI 全链路
# --------------------------------------------------------------------------

def sec_gui(tmp: Path, video: Path, want_done: bool) -> None:
    head("7. GUI 全链路（起服务 → 提交 → 轮询 → 校验字段）")
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    proc = start_gui(port, tmp / "gui-jobs")
    try:
        d = wait_doctor(base)
        fields = ["app", "version", "device", "model", "compute_type", "ffmpeg", "font",
                  "jobs_dir", "can_burn", "frozen"]
        lack = [f for f in fields if f not in d]
        if lack:
            bad("GET /api/doctor 字段齐全", f"缺 {lack}")
        else:
            ok("GET /api/doctor 字段齐全",
               f"v{d['version']} 设备={d['device']} 模型={d['model']} ffmpeg={d['ffmpeg_tag']}")

        if d["version"] == core.VERSION:
            ok("服务自报版本与代码一致", d["version"])
        else:
            bad("服务自报版本与代码一致", f"服务={d['version']} 代码={core.VERSION}")

        if "ver" in urllib.request.urlopen(base + "/", timeout=10).read().decode("utf-8"):
            ok("页面正常返回（含版本徽标容器）")
        else:
            bad("页面正常返回", "首页里没有版本徽标容器")

        jid = submit_job(base, video)
        job = wait_job(base, jid)
        state, msg = job.get("state"), job.get("message", "")

        if want_done:
            if state == "done":
                ok("任务跑到完成", f"{job['cues']} 条字幕 · 耗时字段 {job.get('elapsed')}s")
            elif model_unavailable(msg):
                skip("任务跑到完成", f"模型不可用：{msg[:60]}")
            else:
                bad("任务跑到完成", f"state={state} message={msg[:120]}")
        else:
            if state in ("done", "error") and friendly(msg):
                ok("任务有终态且提示友好", f"state={state} · {msg[:50]}")
            elif model_unavailable(msg):
                skip("任务有终态", f"模型不可用：{msg[:60]}")
            else:
                bad("任务有终态且提示友好", f"state={state} message={msg!r}")

        if not friendly(msg):
            bad("错误提示不含异常痕迹", f"message={msg!r}")

        # 新增的两列必须有值（1.2.3 起）
        if job.get("model"):
            ok("任务列表「模型」列", str(job["model"]))
        else:
            bad("任务列表「模型」列", "字段为空")
        rows = http_json(base, "/api/jobs")
        if rows and rows[0].get("elapsed") is not None:
            ok("任务列表「耗时」列", f"{rows[0]['elapsed']}s")
        else:
            bad("任务列表「耗时」列", f"字段缺失：{rows[0] if rows else None}")

        if want_done and state == "done":
            if job.get("has_srt") and job.get("has_video"):
                dur = probe_duration(Path(d["jobs_dir"]) / (job.get("video_name") or ""))
                ok("两类产物齐全", f"SRT + {core.human_size(job.get('video_size') or 0)}"
                                   + (f" · 时长 {dur:.1f}s" if dur else ""))
            else:
                bad("两类产物齐全", f"srt={job.get('has_srt')} video={job.get('has_video')}")
            if job.get("cues", 0) > 0:
                ok("识别出了字幕", f"{job['cues']} 条")
            else:
                bad("识别出了字幕", "0 条（真实视频不该为空）")
    except Exception as e:  # noqa: BLE001
        bad("GUI 全链路", f"{type(e).__name__}: {e}")
        try:
            out = proc.stdout.read() if proc.stdout else ""
            if out:
                print("      --- 服务输出 ---", flush=True)
                print("\n".join(f"      | {l}" for l in out.splitlines()[-15:]), flush=True)
        except Exception:
            pass
    finally:
        stop(proc)


# --------------------------------------------------------------------------
# 8. 端口独占（GOTCHAS F2）
# --------------------------------------------------------------------------

def sec_port(tmp: Path) -> None:
    head("8. 端口独占（第二个实例不得与第一个共用端口）")
    if os.name != "nt":
        skip("端口独占", "该保护只在 Windows 上启用（类 Unix 保留 SO_REUSEADDR 用于 TIME_WAIT 快速重绑）")
        return

    port = free_port()
    dir_a, dir_b = tmp / "port-a", tmp / "port-b"
    a = start_gui(port, dir_a)
    b = None
    try:
        da = wait_doctor(f"http://127.0.0.1:{port}")
        if Path(da["jobs_dir"]) != dir_a:
            bad("第一个实例占住端口", f"jobs_dir={da['jobs_dir']}")
            return
        ok("第一个实例占住端口", str(port))

        b = start_gui(port, dir_b)          # 故意用同一个端口
        found = None
        deadline = time.time() + 20
        while time.time() < deadline:
            for p in range(port + 1, port + 6):
                try:
                    d = http_json(f"http://127.0.0.1:{p}", "/api/doctor", timeout=3)
                    if Path(d["jobs_dir"]) == dir_b:
                        found = p
                        break
                except Exception:
                    continue
            if found:
                break
            time.sleep(0.5)

        if found:
            ok("第二个实例自动顺延到别的端口", f"{port} 被占用 → 改用 {found}")
        else:
            bad("第二个实例自动顺延到别的端口", f"没能在 {port+1}~{port+5} 上找到它（可能启动失败或与第一个共用端口）")

        # 第一个实例必须还活着、还是自己的目录
        try:
            d = http_json(f"http://127.0.0.1:{port}", "/api/doctor", timeout=5)
            if Path(d["jobs_dir"]) == dir_a:
                ok("第一个实例未受影响")
            else:
                bad("第一个实例未受影响", f"jobs_dir 变成 {d['jobs_dir']}")
        except Exception as e:  # noqa: BLE001
            bad("第一个实例未受影响", f"请求失败：{e}")
    except Exception as e:  # noqa: BLE001
        bad("端口独占", f"{type(e).__name__}: {e}")
    finally:
        if b:
            stop(b)
        stop(a)


# --------------------------------------------------------------------------
# 9. 打包产物（可选）
# --------------------------------------------------------------------------

def sec_exe(exe_dir: Path, tmp: Path) -> None:
    head(f"9. 打包产物校验（{exe_dir.name}）")
    cli = exe_dir / ("autosub.exe" if os.name == "nt" else "autosub")
    gui = exe_dir / ("autosub-gui.exe" if os.name == "nt" else "autosub-gui")
    if not cli.exists() or not gui.exists():
        bad("两个可执行文件都在", f"cli={cli.exists()} gui={gui.exists()}")
        return
    ok("两个可执行文件都在", f"{cli.name} + {gui.name} + _internal/")
    if not (exe_dir / "_internal").is_dir():
        bad("_internal 依赖目录存在", "缺失，分发时必须带整个目录")

    try:
        out = subprocess.run([str(cli), "--version"], capture_output=True, text=True,
                             timeout=120, errors="replace")
        text = (out.stdout or "") + (out.stderr or "")
        if core.VERSION in text:
            ok("exe 自报版本与 pyproject 一致", text.strip().splitlines()[-1][:60])
        else:
            bad("exe 自报版本与 pyproject 一致", f"期望含 {core.VERSION}，实际输出：{text.strip()[:120]!r}")
    except Exception as e:  # noqa: BLE001
        bad("exe 自报版本与 pyproject 一致", f"{type(e).__name__}: {e}")

    port = free_port()
    proc = subprocess.Popen([str(gui), "--no-browser", "--port", str(port),
                             "--jobs-dir", str(tmp / "exe-jobs")],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            cwd=str(exe_dir))
    try:
        d = wait_doctor(f"http://127.0.0.1:{port}", timeout=90)
        if d.get("frozen") and d.get("version") == core.VERSION:
            ok("exe GUI 服务可用", f"v{d['version']} frozen=True 设备={d['device']}")
        elif d.get("version") != core.VERSION:
            bad("exe GUI 服务可用", f"版本不符：{d.get('version')} != {core.VERSION}")
        else:
            bad("exe GUI 服务可用", f"frozen={d.get('frozen')}")
    except Exception as e:  # noqa: BLE001
        bad("exe GUI 服务可用", f"{type(e).__name__}: {e}")
    finally:
        stop(proc)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="smoke", description="autosub 自检 / 冒烟测试")
    p.add_argument("--video", default=None, help="一条带人声的真实视频，用来验证成功路径")
    p.add_argument("--exe-dir", default=None, help="打包产物目录（dist/autosub-<版本>-<平台标签>）")
    p.add_argument("--skip-gui", action="store_true", help="跳过 GUI 相关检查")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    core.safe_streams()
    print(f"autosub 自检 · 版本 {core.VERSION} · {core.OS_TAG} · Python {sys.version.split()[0]}")

    tmp = Path(tempfile.mkdtemp(prefix="autosub-smoke-"))

    sec_version()
    sec_static()
    sec_env()
    tone, no_audio = sec_audio_probe(tmp)
    sec_burn(tmp, tone)
    sec_run_job(tmp, no_audio, tone)

    real_video = Path(args.video).expanduser() if args.video else None
    if real_video and not real_video.exists():
        bad("--video 指定的文件存在", str(real_video))
        real_video = None

    if not args.skip_gui:
        sec_gui(tmp, real_video or tone, want_done=bool(real_video))
        sec_port(tmp)

    if args.exe_dir:
        exe_dir = Path(args.exe_dir).expanduser()
        if exe_dir.is_dir():
            sec_exe(exe_dir, tmp)

    print("\n" + "=" * 60)
    print(f"通过 {len(PASSED)} · 失败 {len(FAILED)} · 跳过 {len(SKIPPED)}")
    if FAILED:
        print("\n失败项：")
        for name, detail in FAILED:
            print(f"  - {name}   {detail}")
    if SKIPPED:
        print("\n跳过项：" + "、".join(SKIPPED))
    print("产物目录（排查用）：" + str(tmp))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
