# -*- coding: utf-8 -*-
"""autosub 核心：视频 --(faster-whisper 本地转写)--> SRT -->(ffmpeg 烧录/软字幕)--> 带字幕视频。

这一层不依赖任何 CLI/HTTP 框架，CLI 与 GUI 都只是它的两个"外壳"。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Iterable

APP = "autosub"

# 版本号的唯一真源是 pyproject.toml 的 version。这里通过包元数据读回来，
# 避免"两处手改、改漏一处"（历史上就出过 doctor 报旧版本、误以为没升级成功的情况）。
# _FALLBACK_VERSION 只在元数据不可用时兜底：直接 PYTHONPATH 跑源码、或某些冻结环境。
_FALLBACK_VERSION = "1.2.7"


def _detect_version() -> str:
    try:
        from importlib.metadata import version as _pkg_version

        v = _pkg_version(APP)
        if v:
            return v
    except Exception:
        pass
    return _FALLBACK_VERSION


VERSION = _detect_version()

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
OS_TAG = "win" if IS_WIN else ("mac" if IS_MAC else "linux")
IS_FROZEN = bool(getattr(sys, "frozen", False))

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".flv", ".webm", ".m4v", ".ts", ".mpg", ".mpeg", ".wmv"}

# 进度回调： (0.0~1.0 的比例, 阶段文字)
ProgressFn = Callable[[float, str], None]

# 日志回调：一行文字
LogFn = Callable[[str], None]


class AutosubError(RuntimeError):
    """可预期的失败（模型损坏、ffmpeg 缺失、烧录失败……），由外壳负责展示。"""


# --------------------------------------------------------------------------
# 输出：控制台 / GUI 共用一套日志出口
# --------------------------------------------------------------------------

_sink: Callable[[str, str], None] | None = None


def set_log_sink(fn: Callable[[str, str], None] | None) -> None:
    """把日志接到别处（GUI 用）。fn(level, message)，level ∈ {info, step, warn}。

    注意：全局单点。GUI 端保证同一时刻只跑一个任务，因此不会串日志。
    """
    global _sink
    _sink = fn


def _emit(level: str, msg: str) -> None:
    if _sink is not None:
        try:
            _sink(level, msg)
            return
        except Exception:
            pass
    print(msg, flush=True)


def info(msg: str) -> None:
    _emit("info", msg)


def step(msg: str) -> None:
    _emit("step", f"\n== {msg}")


def warn(msg: str) -> None:
    _emit("warn", f"[警告] {msg}")


def die(msg: str, code: int = 1) -> None:
    raise AutosubError(msg)


def safe_streams() -> None:
    """统一输出编码，避免中文在 Windows 控制台/管道里变乱码或直接抛异常。

    冻结成 exe 之后，Python 会按系统 ANSI 代码页（中文 Windows 是 936）输出，
    一旦输出被管道/编辑器按 UTF-8 解读就是一堆乱码。这里做两件事：
      1. 把控制台输出代码页切到 65001（UTF-8）；
      2. 把 stdout/stderr 也重设为 UTF-8，并让无法编码的字符降级而不是抛异常。
    """
    if IS_WIN:
        try:
            import ctypes
            k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            if k32.GetConsoleOutputCP() not in (0, 65001):
                k32.SetConsoleOutputCP(65001)
                k32.SetConsoleCP(65001)
        except Exception:
            pass

    for stream in (sys.stdout, sys.stderr):
        try:
            enc = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
            if enc in ("", "utf8") and IS_FROZEN:
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
            elif enc not in ("utf8",):
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
            else:
                stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def human_size(n: int) -> str:
    x = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if x < 1024 or unit == "GB":
            return f"{x:.1f}{unit}" if unit != "B" else f"{int(x)}B"
        x /= 1024
    return f"{x:.1f}GB"


# --------------------------------------------------------------------------
# 运行环境（模型下载相关）默认值
# --------------------------------------------------------------------------

def prepare_model_env(hf_mirror: bool = False, offline: bool = False) -> None:
    """设置 huggingface 下载相关环境变量，必须在 import huggingface_hub 之前调用。

    三个已知的坑/需求：
      * huggingface_hub 会误判"支持符号链接"，把快照目录写成 0 字节的指针文件，
        导致模型加载报 model.bin 不完整 —— 直接用复制模式绕开。
        副作用是快照里放的是真实文件副本（blobs 为空），缓存目录可以随便拷贝。
      * 国内直连 huggingface.co 很慢甚至不通，可选走 hf-mirror.com 镜像。
      * 完全离线的机器上，用 HF_HUB_OFFLINE=1 强制只用本地权重，
        缺文件时立刻报错，不会卡在网络上等超时。
    """
    if hf_mirror:
        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    if offline:
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
    if IS_WIN and not os.environ.get("AUTOSUB_ALLOW_SYMLINKS"):
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")


# --------------------------------------------------------------------------
# 字幕数据结构与时间轴整理
# --------------------------------------------------------------------------

@dataclass
class Cue:
    start: float
    end: float
    text: str


def ts_srt(t: float) -> str:
    if t < 0:
        t = 0.0
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


_ASCII_WORD = re.compile(r"[A-Za-z0-9]")
_BREAK_AFTER = "。！？!?；;"
# 折行时可以"优先断在这里"的字符（含句末标点与空格）
_SOFT_BREAK = "，,、：:；;。．.!?！？ "


def _tokens(text: str) -> list[tuple[str, bool]]:
    """切成最小单元：中文按字，西文按词。第二项表示拼回去时前面要不要空格。"""
    toks: list[tuple[str, bool]] = []
    buf = ""
    need_space = False
    for ch in text:
        if ch.isspace():
            if buf:
                toks.append((buf, need_space))
                buf = ""
            need_space = bool(toks)
            continue
        if _ASCII_WORD.match(ch):
            buf += ch
        elif ch in "-_'’" and buf:
            buf += ch
        else:
            if buf:
                toks.append((buf, need_space))
                buf = ""
            toks.append((ch, need_space))
            need_space = False
    if buf:
        toks.append((buf, need_space))
    return toks


def _render(toks: list[tuple[str, bool]]) -> str:
    out = ""
    for t, sp in toks:
        out += (" " if (sp and out) else "") + t
    return out


def _soft_cut(s: str, max_chars: int) -> int:
    """在 max_chars 附近找一个像话的断点；找不到就硬切在 max_chars。"""
    lo = max(1, int(max_chars * 0.7))
    for i in range(min(max_chars, len(s)) - 1, lo - 1, -1):
        if s[i] in _SOFT_BREAK:
            return i + 1
    return max_chars


def _greedy_lines(toks: list[tuple[str, bool]], max_chars: int) -> list[str]:
    lines: list[str] = []
    cur = ""
    for t, sp in toks:
        sep = 1 if (sp and cur) else 0
        if cur and len(cur) + sep + len(t) > max_chars:
            cut = _soft_cut(cur, max_chars)
            if 0 < cut < len(cur):
                lines.append(cur[:cut].rstrip())
                rest = cur[cut:].lstrip()
            else:
                lines.append(cur)
                rest = ""
            cur = (rest + (" " if (sp and rest) else "") + t) if rest else t
        else:
            cur += (" " if sep else "") + t
    if cur:
        lines.append(cur)
    return lines


def _pair_score(l1: str, l2: str) -> tuple[int, int]:
    brk = l1[-1] if l1 else ""
    return (0 if brk in _SOFT_BREAK else 1, abs(len(l1) - len(l2)))


def _two_line(toks: list[tuple[str, bool]], max_chars: int) -> tuple[str, str] | None:
    """在所有 token 边界里，挑一个既落在标点/空格上、又最均衡的两行切法。"""
    best: tuple[tuple[int, int], str, str] | None = None
    for i in range(1, len(toks)):
        l1 = _render(toks[:i])
        l2 = _render(toks[i:])
        if not l1 or not l2 or len(l1) > max_chars or len(l2) > max_chars:
            continue
        sc = _pair_score(l1, l2)
        if best is None or sc < best[0]:
            best = (sc, l1, l2)
    return (best[1], best[2]) if best else None


def _split_even(toks: list[tuple[str, bool]], k: int) -> list[str]:
    n = len(toks)
    out: list[str] = []
    idx = 0
    for i in range(k):
        take = (n - idx) // (k - i)
        if take <= 0:
            break
        out.append(_render(toks[idx:idx + take]))
        idx += take
    if idx < n:
        out.append(_render(toks[idx:]))
    return [o for o in out if o]


def wrap_lines(text: str, max_chars: int, max_lines: int = 2) -> str:
    """折行：优先断在标点/空格，两行时做均衡切分，避免在词中间劈开。"""
    toks = _tokens(text)
    if not toks:
        return ""

    total = sum(len(t) for t, _ in toks)
    if total <= max_chars:
        return _render(toks)

    greedy = _greedy_lines(toks, max_chars)

    if max_lines == 2 and total <= max_chars * 2:
        bal = _two_line(toks, max_chars)
        gor = (greedy[0], greedy[1]) if len(greedy) == 2 else None
        if bal and gor:
            return "\n".join(bal if _pair_score(*bal) <= _pair_score(*gor) else gor)
        if bal:
            return "\n".join(bal)

    if len(greedy) <= max_lines:
        return "\n".join(greedy)

    # 内容塞不进 max_lines 行：先加大每行容量重排，仍不行就按 token 均分
    per = -(-total // max_lines)
    for _ in range(6):
        out = _greedy_lines(toks, per)
        if len(out) <= max_lines:
            return "\n".join(out)
        per = int(per * 1.2) + 2
    return "\n".join(_split_even(toks, max_lines))


def split_by_sentence(text: str, limit: int) -> list[str]:
    """先按句末标点切，再把过长的块在标点/空格处继续切，最后合并过短的块。"""
    rough: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in _BREAK_AFTER:
            rough.append(buf.strip())
            buf = ""
    if buf.strip():
        rough.append(buf.strip())

    merged: list[str] = []
    for p in rough:
        if not p:
            continue
        if merged and len(merged[-1]) + len(p) <= limit:
            merged[-1] += p
        else:
            merged.append(p)

    out: list[str] = []
    for p in merged:
        while len(p) > limit:
            cut = _soft_cut(p, limit)
            if cut <= 0:
                cut = limit
            out.append(p[:cut].strip())
            p = p[cut:].strip()
        if p:
            out.append(p)
    return out


def _split_long(start: float, end: float, text: str, max_duration: float) -> list[Cue]:
    dur = max(end - start, 0.01)
    if dur <= max_duration or len(text) <= 1:
        return [Cue(start, end, text)]

    n = max(2, int(dur / max_duration) + (1 if dur % max_duration else 0))
    n = min(n, len(text))
    per = -(-len(text) // n)
    parts = [text[i:i + per] for i in range(0, len(text), per)] or [text]
    share = dur / len(parts)
    return [Cue(start + i * share, start + (i + 1) * share, p) for i, p in enumerate(parts)]


def build_cues(
    raw_segments: Iterable,
    max_chars: int = 16,
    max_lines: int = 2,
    max_duration: float = 6.0,
    min_duration: float = 0.8,
    wrap: bool = True,
) -> list[Cue]:
    limit = max(8, max_chars * max_lines)
    cues: list[Cue] = []

    for seg in raw_segments:
        text = re.sub(r"\s+", " ", (getattr(seg, "text", "") or "").strip())
        if not text:
            continue
        start = float(getattr(seg, "start", 0.0) or 0.0)
        end = float(getattr(seg, "end", 0.0) or 0.0)
        if end <= start:
            end = start + 1.0

        chunks = split_by_sentence(text, limit)
        if not chunks:
            continue

        if len(chunks) == 1:
            groups = [(start, end, chunks[0])]
        else:
            total = sum(len(c) for c in chunks) or 1
            groups = []
            t = start
            for c in chunks:
                d = (end - start) * len(c) / total
                groups.append((t, t + d, c))
                t += d

        for gs, ge, gt in groups:
            cues.extend(_split_long(gs, ge, gt, max_duration))

    cues.sort(key=lambda c: c.start)

    gap = 0.05
    for i, c in enumerate(cues):
        if c.end - c.start < min_duration:
            c.end = c.start + min_duration
        if i + 1 < len(cues) and c.end > cues[i + 1].start - gap:
            c.end = max(c.start + 0.2, cues[i + 1].start - gap)

    if wrap:
        for c in cues:
            c.text = wrap_lines(c.text, max_chars, max_lines)

    return cues


def write_srt(cues: list[Cue], path: Path, bom: bool = True) -> None:
    blocks = []
    for i, c in enumerate(cues, 1):
        blocks.append(f"{i}\n{ts_srt(c.start)} --> {ts_srt(c.end)}\n{c.text}\n")
    body = "\n".join(blocks)
    path.write_text(("\ufeff" + body) if bom else body, encoding="utf-8")


def srt_to_string(cues: list[Cue], bom: bool = False) -> str:
    """返回 SRT 文本（GUI 直接下载时用，不落盘）。"""
    blocks = [f"{i}\n{ts_srt(c.start)} --> {ts_srt(c.end)}\n{c.text}\n" for i, c in enumerate(cues, 1)]
    body = "\n".join(blocks)
    return ("\ufeff" + body) if bom else body


# --------------------------------------------------------------------------
# ffmpeg 定位与能力探测
# --------------------------------------------------------------------------

def _candidate_dirs() -> list[Path]:
    dirs: list[Path] = []
    # 冻结成 exe 后：打包目录 + exe 同级目录（用户常把 ffmpeg 丢在旁边）
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs.append(Path(meipass))
    try:
        dirs.append(Path(sys.executable).resolve().parent)
    except Exception:
        pass
    if not IS_FROZEN:
        dirs.append(Path(__file__).resolve().parent)
    return dirs


def find_ffmpeg(explicit: str | None = None) -> tuple[str | None, str]:
    """按 手动指定 → 环境变量 → exe 同级目录 → PATH → imageio-ffmpeg 的顺序找 ffmpeg。"""
    exe_names = ["ffmpeg.exe", "ffmpeg"] if IS_WIN else ["ffmpeg"]

    if explicit:
        p = Path(explicit).expanduser()
        if p.exists():
            return str(p), "手动指定"
        found = shutil.which(explicit)
        if found:
            return found, "手动指定"
        return None, f"手动指定的路径不存在：{explicit}"

    env_ff = os.environ.get("AUTOSUB_FFMPEG")
    if env_ff:
        if Path(env_ff).exists():
            return env_ff, "环境变量 AUTOSUB_FFMPEG"
        found = shutil.which(env_ff)
        if found:
            return found, "环境变量 AUTOSUB_FFMPEG"

    for d in _candidate_dirs():
        for name in exe_names:
            cand = d / name
            try:
                if cand.is_file():
                    return str(cand), "程序同级目录"
            except OSError:
                continue

    found = shutil.which("ffmpeg")
    if found:
        return found, "PATH"

    try:
        import imageio_ffmpeg  # type: ignore
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).exists():
            return exe, "imageio-ffmpeg"
    except Exception:
        pass

    return None, "未找到"


def ffmpeg_probe(ffmpeg: str | None) -> dict:
    if not ffmpeg:
        return {"version": None, "libass": False, "subtitles_filter": False, "fontconfig": False}
    try:
        ver = subprocess.run([ffmpeg, "-hide_banner", "-version"],
                             capture_output=True, text=True, timeout=30,
                             errors="replace")
        full = ver.stdout or ""
        head = full.splitlines()
        version = head[0].strip() if head else ""
    except Exception:
        full, version = "", ""

    filters = ""
    try:
        f = subprocess.run([ffmpeg, "-hide_banner", "-filters"],
                           capture_output=True, text=True, timeout=60,
                           errors="replace")
        filters = (f.stdout or "")
    except Exception:
        pass

    # 注意：filter 列表的标志列宽度随 ffmpeg 版本变化（可能 2 个字符，也可能 3 个），
    # 所以这里不能用固定长度匹配。同时也要认 ass 滤镜。
    has_sub = bool(re.search(r"^\s*[TSC.]+\s+(subtitles|ass)\s+\S+->", filters, re.M))

    # libass / fontconfig 出现在 -version 的 configuration 行，早先只取首行会漏判。
    return {
        "version": version,
        "libass": "--enable-libass" in full,
        "fontconfig": "--enable-fontconfig" in full,
        "subtitles_filter": has_sub,
    }


# --------------------------------------------------------------------------
# 字体（跨平台）
# --------------------------------------------------------------------------

_FONT_CANDIDATES: list[tuple[str, str]] = [
    ("win", r"C:\Windows\Fonts\msyh.ttc"),
    ("win", r"C:\Windows\Fonts\msyh.ttf"),
    ("win", r"C:\Windows\Fonts\msyhbd.ttc"),
    ("win", r"C:\Windows\Fonts\simhei.ttf"),
    ("win", r"C:\Windows\Fonts\simsun.ttc"),
    ("win", r"C:\Windows\Fonts\Deng.ttf"),
    ("mac", "/System/Library/Fonts/PingFang.ttc"),
    ("mac", "/System/Library/Fonts/Hiragino Sans GB.ttc"),
    ("mac", "/System/Library/Fonts/STHeiti Medium.ttc"),
    ("mac", "/Library/Fonts/Arial Unicode.ttf"),
    ("linux", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    ("linux", "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf"),
    ("linux", "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
    ("linux", "/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Regular.otf"),
    ("linux", "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
    ("linux", "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
    ("linux", "/usr/share/fonts/arphic/uming.ttc"),
]

_FONT_FALLBACK_NAME = {
    "msyh.ttc": "Microsoft YaHei", "msyh.ttf": "Microsoft YaHei", "msyhbd.ttc": "Microsoft YaHei",
    "simhei.ttf": "SimHei", "simsun.ttc": "SimSun", "deng.ttf": "DengXian",
    "pingfang.ttc": "PingFang SC", "hiragino sans gb.ttc": "Hiragino Sans GB",
    "stheiti medium.ttc": "Heiti SC", "arial unicode.ttf": "Arial Unicode MS",
    "notosanscjk-regular.ttc": "Noto Sans CJK SC", "notosanscjksc-regular.otf": "Noto Sans CJK SC",
    "sourcehansanssc-regular.otf": "Source Han Sans SC",
    "wqy-zenhei.ttc": "WenQuanYi Zen Hei", "wqy-microhei.ttc": "WenQuanYi Micro Hei",
    "uming.ttc": "AR PL UMing CN",
}

_LINUX_SCAN_DIRS = ["/usr/share/fonts", "/usr/local/share/fonts", str(Path.home() / ".fonts")]
_LINUX_FONT_HINTS = ("notosanscjk", "sourcehansans", "wqy", "droidsansfallback", "uming", "ukai", "arpluming")


def _u16(b: bytes, o: int) -> int:
    return int.from_bytes(b[o:o + 2], "big")


def _u32(b: bytes, o: int) -> int:
    return int.from_bytes(b[o:o + 4], "big")


def read_font_family(path: str | Path) -> str | None:
    """从 ttf/otf/ttc 里读出字体家族名，不依赖任何第三方库。"""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    if len(data) < 12:
        return None

    off = 0
    if data[:4] == b"ttcf":
        if len(data) < 16:
            return None
        off = _u32(data, 12)
    if off + 12 > len(data):
        return None

    num_tables = _u16(data, off + 4)
    name_off = name_len = None
    for i in range(num_tables):
        rec = off + 12 + i * 16
        if rec + 16 > len(data):
            break
        if data[rec:rec + 4] == b"name":
            name_off = _u32(data, rec + 8)
            name_len = _u32(data, rec + 12)
            break
    if name_off is None or name_len is None or name_off + 6 > len(data):
        return None
    if _u16(data, name_off) != 0:
        return None

    count = _u16(data, name_off + 2)
    str_base = name_off + _u16(data, name_off + 4)

    best: tuple[tuple[int, int], str] | None = None
    for i in range(count):
        rec = name_off + 6 + i * 12
        if rec + 12 > len(data):
            break
        pid = _u16(data, rec)
        nid = _u16(data, rec + 6)
        ln = _u16(data, rec + 8)
        ofs = _u16(data, rec + 10)
        if nid not in (1, 16):
            continue
        raw = data[str_base + ofs: str_base + ofs + ln]
        try:
            if pid == 3:
                s = raw.decode("utf-16-be", "ignore")
            elif pid == 1:
                s = raw.decode("latin-1", "ignore")
            else:
                continue
        except Exception:
            continue
        s = s.strip()
        if not s:
            continue
        score = (0 if nid == 16 else 1, 0 if pid == 3 else 1)
        if best is None or score < best[0]:
            best = (score, s)
    return best[1] if best else None


def _scan_linux_fonts() -> list[str]:
    hits: list[str] = []
    for root in _LINUX_SCAN_DIRS:
        rp = Path(root)
        if not rp.is_dir():
            continue
        try:
            for p in rp.rglob("*"):
                if not p.is_file():
                    continue
                if p.suffix.lower() not in (".ttf", ".otf", ".ttc", ".otc"):
                    continue
                name = p.name.lower().replace(" ", "")
                if any(h in name for h in _LINUX_FONT_HINTS):
                    hits.append(str(p))
                if len(hits) >= 8:
                    return hits
        except (OSError, PermissionError):
            continue
    return hits


def detect_cjk_font() -> tuple[str | None, str | None]:
    """返回 (字体文件路径, 字体家族名)。"""
    ordered: list[str] = []
    for tag, p in _FONT_CANDIDATES:
        if tag == OS_TAG:
            ordered.append(p)

    if IS_LINUX:
        ordered.extend(_scan_linux_fonts())
    if IS_MAC:
        ordered.extend([
            "/System/Library/Fonts/Supplemental/Songti.ttc",
            "/Library/Fonts/NotoSansCJK-Regular.ttc",
        ])
    if IS_WIN:
        try:
            import glob as _glob
            for extra in _glob.glob(os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "msyh*.tt*")):
                ordered.append(extra)
        except Exception:
            pass

    for p in ordered:
        if not Path(p).exists():
            continue
        family = read_font_family(p)
        if not family:
            family = _FONT_FALLBACK_NAME.get(Path(p).name.lower())
        if family:
            return p, family
    return None, None


# --------------------------------------------------------------------------
# 模型与转写
# --------------------------------------------------------------------------

MODEL_CHOICES = ["auto", "tiny", "base", "small", "medium", "large-v3", "large-v3-turbo", "distil-large-v3"]
COMPUTE_CHOICES = ["auto", "float16", "int8", "int8_float16", "float32"]
MODE_CHOICES = ["burn", "soft", "srt"]

#: 模型名 → HuggingFace 仓库的兜底表。运行时优先用 faster-whisper 自带的映射
#: （不同版本可能不一致），拿不到才退回这里。注意 large-v3-turbo 与 distil-* 不在 Systran 下。
_FALLBACK_REPOS = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v3": "Systran/faster-whisper-large-v3",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "distil-large-v3": "Systran/faster-distil-whisper-large-v3",
}

#: 各模型权重的近似体积（MB），只用于展示，实际以下载为准
MODEL_SIZES_MB = {
    "tiny": 75,
    "base": 141,
    "small": 464,
    "medium": 1462,
    "large-v3": 2950,
    "large-v3-turbo": 1546,
    "distil-large-v3": 1444,
}

#: 本地模型目录里必须有的文件
_LOCAL_MODEL_FILES = ("model.bin", "config.json")


def named_models() -> list[str]:
    """MODEL_CHOICES 里真正对应 HuggingFace 仓库的名字（去掉 auto）。"""
    return [m for m in MODEL_CHOICES if m != "auto"]


def repo_id_for(model: str) -> str | None:
    """把模型名换成 HuggingFace 仓库 ID；已经是 org/name 形式的原样返回。"""
    try:
        from faster_whisper.utils import _MODELS  # type: ignore
        if model in _MODELS:
            return _MODELS[model]
    except Exception:
        pass
    if model in _FALLBACK_REPOS:
        return _FALLBACK_REPOS[model]
    return model if "/" in model else None


def hf_cache_root() -> Path:
    """HuggingFace 缓存根目录（与 huggingface_hub 的默认规则保持一致）。"""
    explicit = os.environ.get("HF_HUB_CACHE")
    if explicit:
        return Path(explicit).expanduser()
    home = os.environ.get("HF_HOME")
    if home:
        base = Path(home).expanduser()
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")) / "huggingface"
    return base / "hub"


def model_cache_dir(model: str) -> Path | None:
    """某个模型在 HF 缓存里的目录；非具名模型（本地目录）返回 None。"""
    repo = repo_id_for(model)
    if not repo:
        return None
    return hf_cache_root() / ("models--" + repo.replace("/", "--"))


def dir_size(path: Path) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def model_cache_status(model: str) -> tuple[str, int, Path | None]:
    """返回 (状态, 占用字节, 缓存目录)。

    状态取值：已下载 / 不完整 / 未下载 / 非具名模型
    """
    d = model_cache_dir(model)
    if d is None:
        return "非具名模型", 0, None
    if not d.exists():
        return "未下载", 0, d

    size = dir_size(d)
    if (d / "blobs").exists() and any((d / "blobs").glob("*.incomplete")):
        return "不完整", size, d
    snaps = d / "snapshots"
    has_weights = any(p.is_file() for p in snaps.glob("*/model.bin")) if snaps.exists() else False
    return ("已下载" if has_weights else "不完整"), size, d


def local_model_dir(model: str) -> Path | None:
    """如果传进来的"模型名"其实是个存在的目录，返回它的绝对路径。"""
    try:
        p = Path(model).expanduser()
        if p.is_dir():
            return p.resolve()
    except OSError:
        pass
    return None


def check_local_model(path: Path) -> None:
    """粗查一下目录像不像 faster-whisper 模型，不像就给明确提示。"""
    missing = [f for f in _LOCAL_MODEL_FILES if not (path / f).exists()]
    if missing:
        die(
            f"本地模型目录缺少文件：{', '.join(missing)}\n"
            f"  目录：{path}\n"
            "  一个可用的模型目录应包含 model.bin / config.json / tokenizer.json（或 vocabulary.txt）。\n"
            "  重新下载：autosub download-model large-v3 -o \"" + str(path) + "\""
        )


def snapshot_download_model(
    model: str,
    target_dir: str | None = None,
    hf_mirror: bool = False,
    on_log: LogFn | None = None,
) -> Path:
    """预下载模型权重（不跑转写）。

    * target_dir 为空 → 下到 HuggingFace 缓存，之后用 `-m 模型名` 引用；
    * target_dir 给了 → 下成纯文件目录，之后用 `-m 目录路径` 引用，也可以直接拷到离线机器。
    """
    repo = repo_id_for(model) or model
    prepare_model_env(hf_mirror)
    if os.environ.get("HF_HUB_OFFLINE") not in (None, "", "0", "false", "False"):
        die(
            "当前处于离线模式（HF_HUB_OFFLINE 已设置），无法下载。\n"
            "  请先清掉该环境变量（set HF_HUB_OFFLINE= 或 unset），再执行下载。"
        )
    try:
        from huggingface_hub import snapshot_download  # type: ignore
    except ImportError:
        die("缺少依赖 huggingface-hub。请先执行：uv sync")

    log = on_log or info
    log(f"  仓库：{repo}")
    if target_dir:
        dest = Path(target_dir).expanduser().resolve()
        dest.mkdir(parents=True, exist_ok=True)
        log(f"  目标：{dest}（纯文件目录，可用 -m 直接指向它）")
        kw = {"local_dir": str(dest)}
    else:
        dest = model_cache_dir(model) or hf_cache_root()
        log(f"  目标：{dest}（HuggingFace 缓存，之后用 -m {model}）")
        kw = {}
    if hf_mirror:
        log("  镜像：hf-mirror.com")

    log("  开始下载（已下载过的文件会自动跳过）…")
    t0 = time.time()
    try:
        path = snapshot_download(repo_id=repo, **kw)
    except Exception as e:
        die(f"下载失败：{e}\n"
            "  网络不通时：加 --hf-mirror 走国内镜像，或换台能上网的机器下好再拷过来。")
    out = Path(path)
    log(f"  完成 → {out}  ({human_size(dir_size(out))})，用时 {time.time() - t0:.1f}s")
    return out



_cuda_warned = False


def _add_nvidia_dll_dirs() -> None:
    """pip 安装的 nvidia-cublas-cu12 / nvidia-cudnn-cu12 wheel 自带 DLL，
    但不在系统 PATH 上，手动把它们的 bin 目录加进 DLL 搜索路径（仅 Windows 有效）。"""
    if os.name != "nt":
        return
    try:
        import ctranslate2  # type: ignore
        site = Path(ctranslate2.__file__).resolve().parent.parent
    except Exception:
        return
    for sub in ("cublas", "cudnn"):
        d = site / "nvidia" / sub / "bin"
        if d.is_dir():
            try:
                os.add_dll_directory(str(d))
            except OSError:
                pass


def cuda_status() -> tuple[int, bool, str]:
    """返回 (检测到的 CUDA 设备数, CUDA 运行库是否齐备, 缺失的 DLL 名)。

    有显卡 ≠ 能用 CUDA：ctranslate2 还需要 cublas/cudnn 运行库（驱动只提供 nvcuda.dll），
    缺库时直到真正转写才会报错，所以必须在选设备前用 ctypes 预先探测。
    """
    try:
        import ctranslate2  # type: ignore
        n = int(ctranslate2.get_cuda_device_count())
    except Exception:
        return (0, False, "")
    usable, missing = True, ""
    if n > 0 and os.name == "nt":
        _add_nvidia_dll_dirs()
        import ctypes
        for name in ("cublas64_12.dll", "cudnn64_9.dll"):
            try:
                ctypes.WinDLL(name)
            except OSError:
                usable, missing = False, name
                break
    return (n, usable, missing)


def cuda_device_count() -> int:
    global _cuda_warned
    n, usable, missing = cuda_status()
    if n > 0 and not usable:
        if not _cuda_warned:
            _cuda_warned = True
            warn(f"检测到 NVIDIA 显卡，但缺少 CUDA 运行库（{missing}），将使用 CPU。"
                 "如需 GPU 加速：uv pip install nvidia-cublas-cu12 nvidia-cudnn-cu12")
        return 0
    return n


def resolve_device(requested: str) -> str:
    if requested == "cpu":
        return "cpu"
    n, usable, missing = cuda_status()
    if requested == "cuda":
        if n == 0:
            die("指定了 device=cuda，但没有检测到可用的 CUDA 设备。若只是想本机跑，请改用 cpu。")
        if not usable:
            die(f"检测到 NVIDIA 显卡，但缺少 CUDA 运行库（{missing}）。\n"
                "  两种解决办法：\n"
                "    1) 安装运行库后重试：uv pip install nvidia-cublas-cu12 nvidia-cudnn-cu12\n"
                "    2) 改用 --device auto 或 --device cpu")
        return "cuda"
    return "cuda" if (n > 0 and usable) else "cpu"


def resolve_compute_type(requested: str, device: str) -> str:
    if requested != "auto":
        return requested
    return "float16" if device == "cuda" else "int8"


def resolve_model(requested: str, device: str) -> str:
    if requested != "auto":
        return requested
    return "large-v3" if device == "cuda" else "small"


def _ensure_av_compat() -> None:
    """PyAV 从 19 起 av.open() 不再接受 metadata_errors，而 faster-whisper 1.2.x 仍在传。
    这里补一层兼容，避免用户被"版本组合"卡住。"""
    try:
        import av  # type: ignore
    except Exception:
        return
    if getattr(av.open, "_autosub_compat", False):
        return

    orig = av.open

    def _open(file, mode="r", **kwargs):
        if "metadata_errors" in kwargs:
            try:
                return orig(file, mode=mode, **kwargs)
            except TypeError as e:
                if "metadata_errors" not in str(e):
                    raise
                kwargs.pop("metadata_errors", None)
        return orig(file, mode=mode, **kwargs)

    _open._autosub_compat = True  # type: ignore[attr-defined]
    av.open = _open  # type: ignore[assignment]


def transcribe(
    video: Path,
    model_name: str,
    device: str,
    compute_type: str,
    language: str | None,
    vad: bool,
    initial_prompt: str | None,
    on_progress: ProgressFn | None = None,
    hf_mirror: bool = False,
    offline: bool = False,
) -> tuple[list, float]:
    """返回 (segments, 音频总时长秒)。"""
    prepare_model_env(hf_mirror, offline)
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError:
        die("缺少依赖 faster-whisper。请先执行：uv sync（或 pip install faster-whisper）")

    _ensure_av_compat()

    local = local_model_dir(model_name)
    if local is not None:
        check_local_model(local)
        info(f"  模型（本地目录）{local} / device={device} / compute_type={compute_type}")
    else:
        info(f"  模型 {model_name} / device={device} / compute_type={compute_type}")
        state, size, cdir = model_cache_status(model_name)
        if state == "已下载":
            info(f"  权重已在本地：{cdir}  ({human_size(size)})")
        elif offline or os.environ.get("HF_HUB_OFFLINE") not in (None, "", "0", "false", "False"):
            warn(f"  本地没有 {model_name} 的权重，但当前是离线模式（HF_HUB_OFFLINE=1），会直接失败。")
            warn(f"  请先预下载：autosub download-model {model_name}")
        else:
            if state == "不完整":
                warn(f"  本地权重不完整（{cdir}），会尝试续传。")
            info("  首次使用会自动下载模型权重（只下一次），请耐心等待…")
            if MODEL_SIZES_MB.get(model_name):
                info(f"  预计下载约 {MODEL_SIZES_MB[model_name] / 1024:.2f} GB"
                     + ("，国内网络建议加 --hf-mirror" if not hf_mirror else ""))

    t0 = time.time()
    try:
        model = WhisperModel(str(local) if local else model_name,
                             device=device, compute_type=compute_type)
    except Exception as e:
        msg = str(e)
        if device == "cuda" and ("cublas" in msg.lower() or "cudnn" in msg.lower() or "library" in msg.lower()):
            warn("CUDA 运行库缺失，自动回退到 CPU（int8）。")
            device, compute_type = "cpu", "int8"
            model = WhisperModel(str(local) if local else model_name,
                                 device=device, compute_type=compute_type)
        elif local is None and ("incomplete" in msg or "model.bin" in msg or "not found" in msg.lower()):
            repo = repo_id_for(model_name) or model_name
            cdir = model_cache_dir(model_name)
            lines = [
                f"模型权重不可用：{model_name}（仓库 {repo}）",
                "  常见原因是下载中断，留下的文件不完整。删掉缓存再重下即可：",
            ]
            if cdir:
                lines.append(f"    Windows     : rmdir /s /q \"{cdir}\"")
                lines.append(f"    macOS/Linux : rm -rf \"{cdir}\"")
            lines.append(f"  或者用本程序重新下载：autosub download-model {model_name}")
            lines.append("  国内网络建议走镜像（--hf-mirror，或 GUI 里勾选「国内网络」）")
            lines.append("  Windows 上如再次出现 0 字节快照，再加 HF_HUB_DISABLE_SYMLINKS=1")
            die("\n".join(lines))
        else:
            die(f"加载模型失败：{e}")
    info(f"  模型就绪，用时 {time.time() - t0:.1f}s")

    kw = dict(
        language=language,
        vad_filter=vad,
        initial_prompt=initial_prompt,
        beam_size=5,
        condition_on_previous_text=False,
    )
    if vad:
        kw["vad_parameters"] = {"min_silence_duration_ms": 500}

    segments, meta = model.transcribe(str(video), **kw)
    total = float(getattr(meta, "duration", 0) or 0)

    collected = []
    for seg in segments:
        collected.append(seg)
        frac = min(seg.end / total, 1.0) if total > 0 else 0.0
        tail = re.sub(r"\s+", " ", seg.text.strip())[:46]
        if on_progress is not None:
            on_progress(frac, tail)
        else:
            bar = f"{frac * 100:5.1f}%" if total > 0 else "  ..."
            sys.stdout.write(f"\r  {bar}  {ts_srt(seg.end)[:8]}  {tail:<48}")
            sys.stdout.flush()
    if on_progress is None:
        sys.stdout.write("\r" + " " * 78 + "\r")
        sys.stdout.flush()

    lang = getattr(meta, "language", None)
    prob = getattr(meta, "language_probability", None)
    if lang:
        info(f"  识别语言：{lang}" + (f"（置信度 {prob:.0%}）" if isinstance(prob, float) else ""))
    return collected, total


# --------------------------------------------------------------------------
# ffmpeg 执行
# --------------------------------------------------------------------------

def _escape_filter_path(p: str) -> str:
    s = str(p).replace("\\", "/")
    for ch in (":", "'", ",", "[", "]", ";"):
        s = s.replace(ch, "\\" + ch)
    return s


def run_ffmpeg(cmd: list[str], cwd: Path | None = None) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None,
                              capture_output=True, text=True, errors="replace")
    except FileNotFoundError:
        return 127, "ffmpeg 可执行文件无法启动"
    except Exception as e:
        return 1, str(e)
    return proc.returncode, (proc.stderr or "") + (proc.stdout or "")


def run_ffmpeg_stream(cmd: list[str], cwd: Path | None,
                      total_duration: float, on_progress: ProgressFn,
                      base: float, span: float) -> tuple[int, str]:
    """跑 ffmpeg 并解析 -progress 输出，把烧录进度映射到 [base, base+span]。"""
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", bufsize=1,
        )
    except FileNotFoundError:
        return 127, "ffmpeg 可执行文件无法启动"
    except Exception as e:
        return 1, str(e)

    lines: list[str] = []
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.strip()
        if not line:
            continue
        if line.startswith(("out_time_us=", "out_time_ms=")) and total_duration > 0:
            try:
                v = int(line.split("=", 1)[1]) / 1_000_000.0
            except ValueError:
                v = 0.0
            on_progress(base + span * min(max(v / total_duration, 0.0), 0.999), "烧录字幕")
        elif line == "progress=end":
            on_progress(base + span, "烧录字幕")
        else:
            lines.append(line)
    return proc.wait(), "\n".join(lines[-40:])


def burn_in(ffmpeg: str, video: Path, srt_plain: Path, out: Path, style: str,
            font_file: str | None, video_codec: str, preset: str, crf: int,
            keep_temp: bool, workdir: Path,
            total_duration: float = 0.0, on_progress: ProgressFn | None = None,
            base: float = 0.0, span: float = 1.0) -> None:
    """把字幕烧进画面。为避免 Windows 路径里的冒号/反斜杠在滤镜里出问题，
    统一把字幕和字体复制进临时目录，用相对路径喂给滤镜。"""
    shutil.copy2(srt_plain, workdir / "sub.srt")
    fontdir_arg = ""
    if font_file and Path(font_file).exists():
        fdir = workdir / "fonts"
        fdir.mkdir(exist_ok=True)
        shutil.copy2(font_file, fdir / Path(font_file).name)
        fontdir_arg = "fontsdir=fonts:"

    filter_expr = f"subtitles=sub.srt:{fontdir_arg}force_style='{style}'"
    try:
        # 没有任何字幕条目时，subtitles 滤镜会直接报 "Unable to open"，这里退化成空滤镜
        if "-->" not in srt_plain.read_text(encoding="utf-8-sig", errors="ignore"):
            filter_expr = "null"
    except OSError:
        pass

    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video.resolve()),
        "-vf", filter_expr,
        "-c:v", video_codec, "-preset", preset, "-crf", str(crf),
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(out.resolve()),
    ]

    if on_progress is not None:
        cmd_prog = cmd[:1] + ["-progress", "pipe:1", "-nostats"] + cmd[1:]
        code, err = run_ffmpeg_stream(cmd_prog, workdir, total_duration, on_progress, base, span)
    else:
        code, err = run_ffmpeg(cmd, cwd=workdir)

    if code != 0:
        # 相对路径不被某些构建支持时，退回转义后的绝对路径再试一次
        abs_filter = f"subtitles={_escape_filter_path(srt_plain)}:force_style='{style}'"
        if font_file and Path(font_file).exists():
            abs_filter = (f"subtitles={_escape_filter_path(srt_plain)}:"
                          f"fontsdir={_escape_filter_path(str(Path(font_file).parent))}:"
                          f"force_style='{style}'")
        cmd[cmd.index("-vf") + 1] = abs_filter
        if on_progress is not None:
            cmd2 = cmd[:1] + ["-progress", "pipe:1", "-nostats"] + cmd[1:]
            code2, err2 = run_ffmpeg_stream(cmd2, None, total_duration, on_progress, base, span)
        else:
            code2, err2 = run_ffmpeg(cmd, cwd=None)
        if code2 != 0:
            warn("ffmpeg 烧录失败，以下是最后 12 行输出：")
            for line in (err2 or err).strip().splitlines()[-12:]:
                print("    " + line)
            die("烧录失败。可以保留临时文件后手动排查。")
        else:
            warn("相对路径方式不被支持，已自动改用绝对路径并成功。")


def soft_sub(ffmpeg: str, video: Path, srt_plain: Path, out: Path) -> None:
    ext = out.suffix.lower()
    sub_codec = "mov_text" if ext in (".mp4", ".mov", ".m4v") else "srt"
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video), "-i", str(srt_plain),
        "-map", "0", "-map", "1",
        "-c", "copy", "-c:s", sub_codec,
        "-metadata:s:s:0", "language=chi",
        str(out),
    ]
    code, err = run_ffmpeg(cmd)
    if code != 0:
        warn("软字幕封装失败，以下是最后 12 行输出：")
        for line in err.strip().splitlines()[-12:]:
            print("    " + line)
        die("软字幕封装失败。")


# --------------------------------------------------------------------------
# 参数与任务
# --------------------------------------------------------------------------

@dataclass
class Settings:
    """一次处理任务的全部参数。CLI 与 GUI 各自填这个结构。"""
    outdir: str | None = None
    mode: str = "burn"                   # burn | soft | srt

    model: str = "auto"
    device: str = "auto"
    compute_type: str = "auto"
    language: str | None = None
    initial_prompt: str | None = None
    vad: bool = True
    hf_mirror: bool = False
    offline: bool = False               # 只用本地权重（HF_HUB_OFFLINE=1）

    max_chars: int = 16
    max_lines: int = 2
    max_duration: float = 6.0
    min_duration: float = 0.8
    wrap: bool = True

    font_file: str | None = None
    font_name: str | None = None
    font_size: int = 20
    font_color: str = "FFFFFF"
    outline: int = 2
    margin_v: int = 28

    video_codec: str = "libx264"
    preset: str = "veryfast"
    crf: int = 21
    keep_temp: bool = False
    ffmpeg: str | None = None

    # 兼容旧 CLI 命名
    @property
    def no_wrap(self) -> bool:
        return not self.wrap

    @property
    def no_vad(self) -> bool:
        return not self.vad

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class JobResult:
    ok: bool
    video: str
    srt: str | None = None
    output: str | None = None
    message: str = ""
    seconds: float = 0.0
    cues: int = 0
    duration: float = 0.0
    extra: dict = field(default_factory=dict)


def build_style(font_name: str, font_size: int, font_color: str, outline: int, margin_v: int) -> str:
    c = font_color.lstrip("#").upper()
    if len(c) == 6:
        r, g, b = c[0:2], c[2:4], c[4:6]
        primary = f"&H00{b}{g}{r}"
    else:
        primary = "&H00FFFFFF"
    return ",".join([
        f"FontName={font_name}",
        f"FontSize={font_size}",
        f"PrimaryColour={primary}",
        "OutlineColour=&H00000000",
        "BorderStyle=1",
        f"Outline={outline}",
        "Shadow=0",
        f"MarginV={margin_v}",
        "Alignment=2",
    ])


def pick_output(video: Path, outdir: Path, mode: str) -> Path:
    stem = video.stem
    if mode == "burn":
        return outdir / f"{stem}_subtitled.mp4"
    if mode == "soft":
        ext = video.suffix.lower() if video.suffix.lower() in (".mp4", ".mov", ".mkv", ".m4v") else ".mp4"
        return outdir / f"{stem}_subbed{ext}"
    return outdir / f"{stem}.srt"


def has_audio_stream(video: Path) -> bool:
    """有没有音轨。没有音轨时 PyAV 会在取音频流时抛 IndexError，必须先拦下来给友好提示。"""
    try:
        import av  # type: ignore
        with av.open(str(video)) as c:
            return any(s.type == "audio" for s in c.streams)
    except Exception:
        return True     # 探测不出来就别拦，交给转写阶段报具体错


def run_job(video: Path, s: Settings, ffmpeg: str | None, probe: dict,
            on_progress: ProgressFn | None = None) -> JobResult:
    """完整跑一条：转写 → 字幕整理 → 烧录/软字幕。异常都转成 AutosubError 或 JobResult(ok=False)。"""
    t_all = time.time()
    video = Path(video)

    def pg(frac: float, text: str) -> None:
        if on_progress:
            on_progress(max(0.0, min(frac, 1.0)), text)

    if not video.exists():
        return JobResult(ok=False, video=str(video), message=f"文件不存在：{video}")

    if not has_audio_stream(video):
        return JobResult(ok=False, video=str(video),
                         message="这个视频里没有音频轨道，无法转写字幕（纯画面/静音视频不支持）")

    outdir = Path(s.outdir).expanduser() if s.outdir else video.parent
    outdir.mkdir(parents=True, exist_ok=True)
    srt_path = outdir / f"{video.stem}.srt"

    # ---- [1/3] 转写 ----------------------------------------------------
    step(f"[1/3] 转写  {video.name}")
    device = resolve_device(s.device)
    compute = resolve_compute_type(s.compute_type, device)
    model_name = resolve_model(s.model, device)

    t0 = time.time()
    raw, duration = transcribe(
        video, model_name, device, compute,
        s.language, s.vad, s.initial_prompt,
        on_progress=(lambda f, t: pg(f * 0.72, t)) if on_progress else None,
        hf_mirror=s.hf_mirror,
        offline=s.offline,
    )
    if not raw:
        warn("没有识别到任何语音内容，将生成空字幕文件。")
    info(f"  转写完成：{len(raw)} 段，用时 {time.time() - t0:.1f}s")

    # ---- [2/3] 字幕整理 ------------------------------------------------
    pg(0.75, "整理字幕")
    step(f"[2/3] 整理字幕  line≤{s.max_chars}字 × {s.max_lines}行  单条≤{s.max_duration}s")
    cues = build_cues(raw, s.max_chars, s.max_lines, s.max_duration, s.min_duration, wrap=s.wrap)
    write_srt(cues, srt_path, bom=True)
    info(f"  整理后 {len(cues)} 条 → {srt_path}  ({human_size(srt_path.stat().st_size)})")

    if s.mode == "srt":
        step("[3/3] 只出字幕，跳过视频封装")
        pg(1.0, "完成")
        return JobResult(ok=True, video=str(video), srt=str(srt_path),
                         message="已生成字幕文件", seconds=round(time.time() - t_all, 1),
                         cues=len(cues), duration=duration)

    if not cues:
        # 空 SRT 会让 ffmpeg 的 subtitles 滤镜报 "Unable to open"，不如提前说清楚
        warn("没有识别到语音内容，跳过视频封装（已输出空的字幕文件）。")
        pg(1.0, "完成")
        return JobResult(ok=False, video=str(video), srt=str(srt_path),
                         message="没有识别到语音内容，只生成了空字幕文件；可换更大的模型或指定语言再试",
                         seconds=round(time.time() - t_all, 1), cues=0, duration=duration)

    # ---- [3/3] 视频 ----------------------------------------------------
    pg(0.78, "准备烧录")
    step(f"[3/3] {'烧录字幕' if s.mode == 'burn' else '封装软字幕'}")
    if not ffmpeg:
        warn("未找到 ffmpeg，无法生成视频。字幕文件已正常输出。")
        warn("安装方式：")
        warn("  Windows : winget install Gyan.FFmpeg    （或 pip install imageio-ffmpeg）")
        warn("  macOS   : brew install ffmpeg")
        warn("  Linux   : sudo apt install ffmpeg")
        return JobResult(ok=False, video=str(video), srt=str(srt_path),
                         message="未找到 ffmpeg，只生成了字幕文件",
                         seconds=round(time.time() - t_all, 1), cues=len(cues), duration=duration)

    if s.mode == "burn" and not probe.get("subtitles_filter"):
        warn("当前 ffmpeg 缺少 subtitles 滤镜（没有编译 libass），无法烧录。")
        warn("可以改用软字幕模式，或换一个 full build 的 ffmpeg。")
        return JobResult(ok=False, video=str(video), srt=str(srt_path),
                         message="当前 ffmpeg 不支持烧录（缺 libass），只生成了字幕文件",
                         seconds=round(time.time() - t_all, 1), cues=len(cues), duration=duration)

    out = pick_output(video, outdir, s.mode)

    workdir = Path(tempfile.mkdtemp(prefix="autosub_"))
    try:
        srt_plain = workdir / "plain.srt"
        write_srt(cues, srt_plain, bom=False)

        if s.mode == "burn":
            font_file, font_name = None, s.font_name
            if s.font_file:
                ff_path = Path(s.font_file).expanduser()
                if not ff_path.exists():
                    die(f"指定的字体不存在：{ff_path}")
                font_file = str(ff_path)
                font_name = s.font_name or read_font_family(ff_path) or ff_path.stem
            else:
                p, fam = detect_cjk_font()
                font_file, font_name = p, (s.font_name or fam)
            if not font_name:
                font_name = "sans-serif"
                warn("没有找到中文字体，使用默认字体。中文可能显示为方块，请指定字体文件。")
            info(f"  字体：{font_name}" + (f"  ({font_file})" if font_file else ""))

            style = build_style(font_name, s.font_size, s.font_color, s.outline, s.margin_v)
            info(f"  编码：{s.video_codec} preset={s.preset} crf={s.crf}")
            burn_in(ffmpeg, video, srt_plain, out, style, font_file,
                    s.video_codec, s.preset, s.crf, s.keep_temp, workdir,
                    total_duration=duration or 0.0,
                    on_progress=on_progress, base=0.80, span=0.19)
        else:
            soft_sub(ffmpeg, video, srt_plain, out)
            pg(0.99, "封装完成")
    finally:
        if not s.keep_temp:
            shutil.rmtree(workdir, ignore_errors=True)

    if not out.exists():
        return JobResult(ok=False, video=str(video), srt=str(srt_path),
                         message="输出文件没有生成",
                         seconds=round(time.time() - t_all, 1), cues=len(cues), duration=duration)

    pg(1.0, "完成")
    info(f"  完成 → {out}  ({human_size(out.stat().st_size)})")
    return JobResult(ok=True, video=str(video), srt=str(srt_path), output=str(out),
                     message="完成", seconds=round(time.time() - t_all, 1),
                     cues=len(cues), duration=duration)


def process_many(videos: list[Path], s: Settings, ffmpeg: str | None, probe: dict) -> int:
    """CLI 批量入口：返回失败数量。"""
    failed = 0
    for i, v in enumerate(videos, 1):
        if len(videos) > 1:
            info("\n" + "=" * 60)
            info(f"({i}/{len(videos)}) {v.name}")
            info("=" * 60)
        try:
            res = run_job(v, s, ffmpeg, probe)
            if not res.ok:
                failed += 1
                warn(res.message)
        except AutosubError as e:
            warn(str(e))
            failed += 1
        except KeyboardInterrupt:
            raise
        except Exception as e:  # 兜底，避免一条坏文件中断整批
            warn(f"处理 {v.name} 时出错：{e}")
            failed += 1
    return failed
