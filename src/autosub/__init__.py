# -*- coding: utf-8 -*-
"""autosub —— 跨平台视频自动加字幕（本地 Whisper 转写 + ffmpeg 烧录）。

对外只暴露少量入口，CLI 与 GUI 都在内部调用 :func:`autosub.core.run_job`。
"""

from .core import (  # noqa: F401
    APP,
    VERSION,
    AutosubError,
    Cue,
    JobResult,
    Settings,
    build_cues,
    detect_cjk_font,
    find_ffmpeg,
    ffmpeg_probe,
    run_job,
    set_log_sink,
    transcribe,
    wrap_lines,
    write_srt,
)

__all__ = [
    "APP", "VERSION", "AutosubError", "Cue", "JobResult", "Settings",
    "build_cues", "detect_cjk_font", "find_ffmpeg", "ffmpeg_probe",
    "run_job", "set_log_sink", "transcribe", "wrap_lines", "write_srt",
]
