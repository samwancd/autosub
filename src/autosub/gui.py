# -*- coding: utf-8 -*-
"""autosub 图形界面：本地起一个 HTTP 服务，用浏览器当界面。

只用标准库（http.server），不引入 Flask/FastAPI —— 这样打包成 exe 后没有任何
模板/静态文件依赖，一个二进制就能既是 CLI 又是 GUI。

接口一览（前端就靠这几个）：
    GET    /                                 页面
    GET    /api/doctor                       环境信息
    POST   /api/jobs                         {"name","mode","model",...} → {"id"}
    PUT    /api/jobs/<id>/file?name=x.mp4    原始视频字节流（直接写盘，不占内存）
    POST   /api/jobs/<id>/start              开始处理
    GET    /api/jobs/<id>                    进度/状态/日志
    GET    /api/jobs                         本次会话任务列表
    GET    /api/jobs/<id>/media?what=input|video     预览（支持 Range，可拖动进度条）
    GET    /api/jobs/<id>/download?what=srt|video    下载
    DELETE /api/jobs/<id>                    删除任务和产物
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import queue
import re
import shutil
import socket
import sys
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote

from .core import (
    APP,
    VERSION,
    VIDEO_EXTS,
    AutosubError,
    Settings,
    cuda_device_count,
    detect_cjk_font,
    ffmpeg_probe,
    find_ffmpeg,
    human_size,
    resolve_compute_type,
    resolve_device,
    resolve_model,
    run_job,
    safe_streams,
    set_log_sink,
)
from .webui import PAGE

MAX_LOG_LINES = 500
CHUNK = 1024 * 1024
_ID_RE = re.compile(r"^[0-9a-f]{8,32}$")


def default_jobs_dir() -> Path:
    return Path(os.environ.get("AUTOSUB_JOBS_DIR") or (Path.home() / ".autosub" / "jobs"))


# --------------------------------------------------------------------------
# 任务
# --------------------------------------------------------------------------

@dataclass
class Job:
    id: str
    name: str
    created: float
    dir: Path
    opts: dict = field(default_factory=dict)
    state: str = "new"          # new | queued | running | done | error
    stage: str = ""
    progress: float = 0.0
    message: str = ""
    log: list[str] = field(default_factory=list)
    src: Path | None = None
    srt: Path | None = None
    out: Path | None = None
    result_ok: bool = False
    cues: int = 0
    seconds: float = 0.0
    model_used: str = ""        # 实际使用的模型（本地目录显示目录名）
    t_start: float = 0.0        # 开始跑的时刻（排队中为 0）
    t_end: float = 0.0          # 结束时刻（跑着时为 0，前端据此显示实时耗时）

    def add_log(self, level: str, msg: str) -> None:
        self.log.append(msg)
        if len(self.log) > MAX_LOG_LINES:
            del self.log[:len(self.log) - MAX_LOG_LINES]

    def model_label(self) -> str:
        """实际用的模型：跑起来后取真实解析结果，否则回落到用户选的那一项。"""
        if self.model_used:
            return self.model_used
        m = str((self.opts or {}).get("model") or "auto")
        if os.sep in m or "/" in m:
            return f"{Path(m).name}（本地）"
        return m

    def public(self) -> dict:
        def size(p: Path | None) -> int | None:
            try:
                return p.stat().st_size if p and p.exists() else None
            except OSError:
                return None

        return {
            "id": self.id,
            "name": self.name,
            "created": self.created,
            "state": self.state,
            "stage": self.stage,
            "progress": round(self.progress, 4),
            "message": self.message,
            "log": self.log[-160:],
            "result_ok": self.result_ok,
            "cues": self.cues,
            "seconds": self.seconds,
            "model": self.model_label(),
            "elapsed": (round((self.t_end or time.time()) - self.t_start, 1)
                        if self.t_start else None),
            "has_srt": bool(self.srt and self.srt.exists()),
            "has_video": bool(self.out and self.out.exists()),
            "srt_name": self.srt.name if self.srt and self.srt.exists() else None,
            "video_name": self.out.name if self.out and self.out.exists() else None,
            "video_size": size(self.out),
            "src_size": size(self.src),
        }


class JobStore:
    """任务表 + 单工作线程。同一时刻只跑一个任务（Whisper 本来就吃满资源）。"""

    def __init__(self, jobs_dir: Path, defaults: Settings):
        self.dir = jobs_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.defaults = defaults
        self._lock = threading.RLock()
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._worker = threading.Thread(target=self._loop, name="autosub-worker", daemon=True)
        self._worker.start()

    # -- 查询 -----------------------------------------------------------
    def get(self, jid: str) -> Job | None:
        with self._lock:
            return self._jobs.get(jid)

    def list(self) -> list[Job]:
        with self._lock:
            return [self._jobs[i] for i in reversed(self._order) if i in self._jobs]

    # -- 写 -------------------------------------------------------------
    def create(self, name: str, opts: dict) -> Job:
        jid = uuid.uuid4().hex[:16]
        safe = re.sub(r"[^\w.\-]+", "_", Path(name or "video").name)[:80] or "video"
        jdir = self.dir / f"{time.strftime('%Y%m%d-%H%M%S')}_{jid[:6]}"
        jdir.mkdir(parents=True, exist_ok=True)
        job = Job(id=jid, name=safe, created=time.time(), dir=jdir, opts=dict(opts or {}))
        with self._lock:
            self._jobs[jid] = job
            self._order.append(jid)
        return job

    def attach_source(self, job: Job, filename: str) -> Path:
        """保留原始文件名（只做安全字符过滤），这样产物就是 demo.srt / demo_subtitled.mp4，
        用户下载下来一眼能对上自己的视频，而不是一堆 input.*。"""
        base = re.sub(r"[^\w.\- ]+", "_", Path(filename or job.name).name).strip(" .") or "video"
        if Path(base).suffix.lower() not in VIDEO_EXTS:
            base = Path(base).stem + ".mp4"
        dst = job.dir / base[:120]
        job.src = dst
        job.name = dst.name
        job.state = "queued"
        return dst

    def start(self, job: Job) -> None:
        job.state = "queued"
        job.stage = "排队中"
        self._queue.put(job.id)

    def delete(self, jid: str) -> bool:
        with self._lock:
            job = self._jobs.pop(jid, None)
            if job and jid in self._order:
                self._order.remove(jid)
        if not job:
            return False
        shutil.rmtree(job.dir, ignore_errors=True)
        return True

    # -- 工作线程 -------------------------------------------------------
    def _loop(self) -> None:
        while True:
            jid = self._queue.get()
            if jid is None:
                return
            job = self.get(jid)
            if job is None or job.src is None or not job.src.exists():
                if job:
                    job.state = "error"
                    job.message = "没有收到视频文件"
                continue
            try:
                self._run(job)
            except Exception as e:  # 绝不让线程挂掉
                job.state = "error"
                job.message = f"内部错误：{e}"
                job.add_log("warn", f"[错误] {e}")

    def _run(self, job: Job) -> None:
        s = self._settings_for(job)
        job.state = "running"
        job.stage = "准备中"
        job.progress = 0.0
        job.t_start = time.time()
        job.t_end = 0.0
        try:
            # 把真正要用的模型名先算出来（与 core.run_job 里的解析规则一致），供任务列表展示
            m = str(s.model or "auto")
            job.model_used = (f"{Path(m).name}（本地）" if (os.sep in m or "/" in m)
                              else resolve_model(m, resolve_device(s.device)))
        except Exception:
            job.model_used = ""
        set_log_sink(job.add_log)
        try:
            ffmpeg, ff_src = find_ffmpeg(s.ffmpeg)
            probe = ffmpeg_probe(ffmpeg)
            if ffmpeg:
                job.add_log("info", f"ffmpeg：{ffmpeg}（{ff_src}）")

            def pg(frac: float, text: str) -> None:
                job.progress = max(job.progress, min(max(frac, 0.0), 1.0))
                if text:
                    job.stage = text.strip()[:60]

            res = run_job(job.src, s, ffmpeg, probe, on_progress=pg)
            job.cues = res.cues
            job.seconds = res.seconds
            job.result_ok = res.ok
            job.message = res.message
            if res.srt:
                job.srt = Path(res.srt)
            if res.output:
                job.out = Path(res.output)
            job.state = "done" if res.ok else "error"
            if job.state == "done":
                job.progress = 1.0
            job.stage = "完成" if res.ok else "失败"
        except AutosubError as e:
            job.state = "error"
            job.stage = "失败"
            job.message = str(e)
            job.add_log("warn", f"[错误] {e}")
        finally:
            job.t_end = time.time()
            set_log_sink(None)

    def _settings_for(self, job: Job) -> Settings:
        base = self.defaults.as_dict()
        for k, v in (job.opts or {}).items():
            if k in base and v is not None:
                base[k] = v
        # 只在没给边界值时才启用 outdir：产物统一落在任务目录，方便浏览与删除
        s = Settings(**{k: v for k, v in base.items() if k in Settings.__dataclass_fields__})
        s.outdir = str(job.dir)
        s.ffmpeg = self.defaults.ffmpeg
        return s


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = f"{APP}/{VERSION}"
    protocol_version = "HTTP/1.1"

    # ---- 小工具 --------------------------------------------------------
    def _send_json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, status: int = 200, ctype: str = "text/plain; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, inline: bool, download_name: str | None = None) -> None:
        if not path or not path.is_file():
            self._send_json({"error": "文件不存在"}, 404)
            return

        total = path.stat().st_size
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        start, end = 0, total - 1
        status = 200

        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
            if m:
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else total - 1
                elif m.group(2):        # bytes=-500 尾部
                    start = max(0, total - int(m.group(2)))
                end = min(end, total - 1)
                if start > end or start >= total:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{total}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        if not inline:
            name = download_name or path.name
            self.send_header("Content-Disposition",
                             "attachment; filename=\"%s\"; filename*=UTF-8''%s"
                             % (name.encode("ascii", "ignore").decode() or "download", quote(name)))
        self.end_headers()

        if self.command == "HEAD":
            return
        with path.open("rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                buf = f.read(min(CHUNK, remaining))
                if not buf:
                    break
                try:
                    self.wfile.write(buf)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remaining -= len(buf)

    # ---- 路由 ----------------------------------------------------------
    def do_GET(self) -> None:
        u = urlparse(self.path)
        q = parse_qs(u.query)
        path = u.path

        if path in ("/", "/index.html"):
            self._send_text(PAGE, ctype="text/html; charset=utf-8")
            return

        if path == "/api/doctor":
            self._send_json(self.server.doctor_info())  # type: ignore[attr-defined]
            return

        if path == "/api/jobs":
            self._send_json([j.public() for j in self.server.store.list()])  # type: ignore[attr-defined]
            return

        m = re.match(r"^/api/jobs/([0-9a-f]{8,32})$", path)
        if m:
            job = self.server.store.get(m.group(1))  # type: ignore[attr-defined]
            if not job:
                self._send_json({"error": "任务不存在"}, 404)
                return
            self._send_json(job.public())
            return

        m = re.match(r"^/api/jobs/([0-9a-f]{8,32})/(media|download)$", path)
        if m:
            job = self.server.store.get(m.group(1))  # type: ignore[attr-defined]
            if not job:
                self._send_json({"error": "任务不存在"}, 404)
                return
            what = (q.get("what") or ["video"])[0]
            if what == "srt":
                target = job.srt
                name = f"{Path(job.src.name).stem if job.src else 'subtitle'}.srt"
            elif what == "input":
                target = job.src
                name = job.name
            else:
                target = job.out
                name = job.out.name if job.out else "subtitled.mp4"
            inline = m.group(2) == "media" and what != "srt"
            if what == "srt" and m.group(2) == "media":
                inline = True       # 字幕以文本形式在浏览器里直接看
            self._send_file(target, inline, name)
            return

        self._send_json({"error": "not found"}, 404)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_POST(self) -> None:
        u = urlparse(self.path)
        path = u.path

        if path == "/api/jobs":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                self._send_json({"error": "请求体不是合法 JSON"}, 400)
                return
            name = str(payload.pop("name", "video.mp4"))
            job = self.server.store.create(name, payload)  # type: ignore[attr-defined]
            self._send_json({"id": job.id, "name": job.name})
            return

        m = re.match(r"^/api/jobs/([0-9a-f]{8,32})/start$", path)
        if m:
            job = self.server.store.get(m.group(1))  # type: ignore[attr-defined]
            if not job:
                self._send_json({"error": "任务不存在"}, 404)
                return
            if not job.src or not job.src.exists():
                self._send_json({"error": "还没有上传视频"}, 400)
                return
            self.server.store.start(job)  # type: ignore[attr-defined]
            self._send_json({"ok": True})
            return

        self._send_json({"error": "not found"}, 404)

    def do_PUT(self) -> None:
        """把请求体原样写盘。用裸 body 而不是 multipart，是为了几十 GB 的视频也不爆内存。"""
        u = urlparse(self.path)
        q = parse_qs(u.query)
        m = re.match(r"^/api/jobs/([0-9a-f]{8,32})/file$", u.path)
        if not m:
            self._send_json({"error": "not found"}, 404)
            return
        job = self.server.store.get(m.group(1))  # type: ignore[attr-defined]
        if not job:
            self._send_json({"error": "任务不存在"}, 404)
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send_json({"error": "缺少 Content-Length"}, 400)
            return
        if length <= 0:
            self._send_json({"error": "空文件"}, 400)
            return

        name = (q.get("name") or [job.name])[0]
        dst = self.server.store.attach_source(job, name)  # type: ignore[attr-defined]

        written = 0
        try:
            with dst.open("wb") as f:
                while written < length:
                    buf = self.rfile.read(min(CHUNK, length - written))
                    if not buf:
                        break
                    f.write(buf)
                    written += len(buf)
        except Exception as e:
            self._send_json({"error": f"写入失败：{e}"}, 500)
            return

        if written != length:
            self._send_json({"error": f"上传不完整（{written}/{length} 字节）"}, 400)
            return

        job.add_log("info", f"收到视频：{name}（{human_size(written)}）")
        self._send_json({"ok": True, "size": written, "path": str(dst)})

    def do_DELETE(self) -> None:
        u = urlparse(self.path)
        m = re.match(r"^/api/jobs/([0-9a-f]{8,32})$", u.path)
        if not m:
            self._send_json({"error": "not found"}, 404)
            return
        ok = self.server.store.delete(m.group(1))  # type: ignore[attr-defined]
        self._send_json({"ok": ok})

    # ---- 日志收敛 ------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:
        if getattr(self.server, "verbose", False):  # type: ignore[attr-defined]
            sys.stderr.write("[http] %s - %s\n" % (self.address_string(), fmt % args))

    def log_error(self, fmt: str, *args) -> None:
        if getattr(self.server, "verbose", False):  # type: ignore[attr-defined]
            sys.stderr.write("[http] %s\n" % (fmt % args))


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # Windows 上 SO_REUSEADDR 允许第二个服务悄悄绑定同一端口（请求随机分流，极难排查），
    # 所以仅类 Unix 平台启用（那边是为了 TIME_WAIT 快速重绑）。
    allow_reuse_address = os.name != "nt"

    def __init__(self, addr, handler, store: JobStore, defaults: Settings, verbose: bool = False):
        super().__init__(addr, handler)
        self.store = store
        self.defaults = defaults
        self.verbose = verbose
        self._doctor_cache: dict | None = None

    def doctor_info(self) -> dict:
        if self._doctor_cache is not None:
            return self._doctor_cache

        try:
            import faster_whisper  # type: ignore
            ready = True
            fw_ver = getattr(faster_whisper, "__version__", "?")
        except Exception:
            ready, fw_ver = False, None

        n_cuda = cuda_device_count()
        device = "cuda" if n_cuda else "cpu"
        ffmpeg, tag = find_ffmpeg(self.defaults.ffmpeg)
        probe = ffmpeg_probe(ffmpeg)
        font_file, font_family = detect_cjk_font()

        self._doctor_cache = {
            "app": APP,
            "version": VERSION,
            "os": sys.platform,
            "python": sys.version.split()[0],
            "ready": ready,
            "faster_whisper": fw_ver,
            "cuda": n_cuda,
            "device": device,
            "model": resolve_model(self.defaults.model, device),
            "compute_type": resolve_compute_type(self.defaults.compute_type, device),
            "ffmpeg": ffmpeg,
            "ffmpeg_tag": tag,
            "ffmpeg_version": probe.get("version"),
            "libass": bool(probe.get("libass")),
            "subtitles_filter": bool(probe.get("subtitles_filter")),
            "can_burn": bool(ffmpeg and probe.get("subtitles_filter")),
            "font": font_family,
            "font_file": font_file,
            "jobs_dir": str(self.store.dir),
            "frozen": bool(getattr(sys, "frozen", False)),
        }
        return self._doctor_cache


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------

def _free_port(host: str, port: int, tries: int = 20) -> int:
    # 探测时同样不能带 SO_REUSEADDR（Windows 下它会掩住“端口已被监听”的事实）
    reuse = os.name != "nt"
    for i in range(tries):
        p = port + i
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if reuse:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, p))
                return p
            except OSError:
                continue
    raise AutosubError(f"端口 {port} 起连续 {tries} 个都被占用，请用 --port 指定其它端口。")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=f"{APP} gui",
        description="autosub 浏览器界面：上传视频 → 出字幕文件和带字幕的视频",
    )
    p.add_argument("--host", default="127.0.0.1",
                   help="监听地址，默认 127.0.0.1（仅本机）。改成 0.0.0.0 会让同网段其他设备也能访问")
    p.add_argument("--port", type=int, default=8765, help="监听端口，默认 8765（被占用时自动顺延）")
    p.add_argument("--no-browser", action="store_true", help="不要自动打开浏览器")
    p.add_argument("--jobs-dir", default=None, help="任务与产物目录，默认 ~/.autosub/jobs")
    p.add_argument("--verbose", action="store_true", help="打印每条 HTTP 请求")

    g = p.add_argument_group("默认参数（页面上还能单独改）")
    g.add_argument("-m", "--model", default="auto")
    g.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])
    g.add_argument("--compute-type", default="auto")
    g.add_argument("--language", default=None)
    g.add_argument("--initial-prompt", default=None)
    g.add_argument("--mode", default="burn", choices=["burn", "soft", "srt"])
    g.add_argument("--font-file", default=None)
    g.add_argument("--font-name", default=None)
    g.add_argument("--font-size", type=int, default=20)
    g.add_argument("--font-color", default="FFFFFF")
    g.add_argument("--margin-v", type=int, default=28)
    g.add_argument("--max-chars", type=int, default=16)
    g.add_argument("--video-codec", default="libx264")
    g.add_argument("--preset", default="veryfast")
    g.add_argument("--crf", type=int, default=21)
    g.add_argument("--ffmpeg", default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    safe_streams()
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))

    defaults = Settings(
        model=args.model, device=args.device, compute_type=args.compute_type,
        language=args.language, initial_prompt=args.initial_prompt, mode=args.mode,
        font_file=args.font_file, font_name=args.font_name, font_size=args.font_size,
        font_color=args.font_color, margin_v=args.margin_v, max_chars=args.max_chars,
        video_codec=args.video_codec, preset=args.preset, crf=args.crf, ffmpeg=args.ffmpeg,
    )

    jobs_dir = Path(args.jobs_dir).expanduser() if args.jobs_dir else default_jobs_dir()
    store = JobStore(jobs_dir, defaults)

    host = args.host
    port = _free_port(host, args.port)
    if port != args.port:
        print(f"[提示] 端口 {args.port} 被占用，改用 {port}", flush=True)

    httpd = Server((host, port), Handler, store, defaults, verbose=args.verbose)
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/"

    print(f"{APP} {VERSION} · 浏览器界面已启动", flush=True)
    print(f"  地址    : {url}", flush=True)
    print(f"  产物目录: {jobs_dir}", flush=True)
    if host not in ("127.0.0.1", "localhost"):
        print(f"  注意    : 已监听 {host}，同网段的其它设备也能打开这个地址", flush=True)
    print("  按 Ctrl+C 停止", flush=True)

    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。", flush=True)
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
