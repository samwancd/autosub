# -*- coding: utf-8 -*-
"""autosub 命令行外壳。

    autosub video.mp4                # 出 video.srt + video_subtitled.mp4
    autosub video.mp4 --srt-only     # 只要字幕文件
    autosub video.mp4 --soft         # 软字幕轨（播放器可开关）
    autosub gui                      # 启动浏览器界面（等价于 autosub-gui）
    autosub doctor                   # 环境自检
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .core import (
    APP,
    VERSION,
    MODE_CHOICES,
    MODEL_CHOICES,
    MODEL_SIZES_MB,
    COMPUTE_CHOICES,
    VIDEO_EXTS,
    AutosubError,
    Settings,
    cuda_device_count,
    cuda_status,
    detect_cjk_font,
    ffmpeg_probe,
    find_ffmpeg,
    hf_cache_root,
    human_size,
    info,
    local_model_dir,
    model_cache_status,
    named_models,
    process_many,
    repo_id_for,
    resolve_compute_type,
    resolve_device,
    resolve_model,
    safe_streams,
    snapshot_download_model,
    warn,
)

GUI_ALIASES = {"gui", "ui", "web", "serve", "server"}
DOWNLOAD_ALIASES = {"download-model", "download", "pull", "fetch-model"}


def model_arg(value: str) -> str:
    """-m 的取值：模型名，或一个含 model.bin 的本地模型目录。"""
    if value in MODEL_CHOICES:
        return value
    p = local_model_dir(value)
    if p is not None:
        if not (p / "model.bin").exists():
            raise argparse.ArgumentTypeError(
                f"目录 {p} 里没有 model.bin，不像是 faster-whisper 模型目录"
            )
        return str(p)
    raise argparse.ArgumentTypeError(
        "只能是 " + " / ".join(named_models()) + "（或 auto），"
        "也可以给一个本地模型目录路径（用 autosub download-model 下的那种）"
    )


def print_model_table(only_status: bool = True) -> None:
    info(f"模型缓存目录：{hf_cache_root()}")
    if not only_status:
        info("  （加 HF_HOME / HF_HUB_CACHE 环境变量可以改到别的盘）")
    info("  已下载 / 不完整 / 未下载：")
    for name in named_models():
        state, size, _ = model_cache_status(name)
        mark = {"已下载": "[OK]", "不完整": "[! ]", "未下载": "[  ]"}.get(state, "[? ]")
        size_str = human_size(size) if size else "-"
        repo = repo_id_for(name) or ""
        info(f"    {mark} {name:<16} 约 {MODEL_SIZES_MB.get(name, 0):>5}MB  {state:<6} "
             f"本地 {size_str:>8}  {repo}")
    info("  预下载：autosub download-model <模型名>            下到缓存，之后用 -m 名字")
    info("          autosub download-model <模型名> -o D:\\mdl  下成纯文件目录，可拷到离线机器")


def cmd_doctor(args) -> int:
    info(f"{APP} {VERSION} · 环境自检")
    info("-" * 56)
    info(f"操作系统      : {sys.platform}")
    info(f"Python        : {sys.version.split()[0]}  ({sys.executable})")

    try:
        import faster_whisper  # type: ignore
        fw = getattr(faster_whisper, "__version__", "未知")
    except Exception:
        fw = None
    info(f"faster-whisper: {fw or '未安装  → uv sync'}")

    try:
        import ctranslate2  # type: ignore
        n_raw, usable, missing = cuda_status()
        ct = f"{getattr(ctranslate2, '__version__', '未知')}  CUDA 设备 {n_raw} 个"
        if n_raw > 0 and not usable:
            ct += f"  （缺 {missing}，GPU 不可用 → uv pip install nvidia-cublas-cu12 nvidia-cudnn-cu12）"
    except Exception:
        ct = "未安装"
    info(f"ctranslate2   : {ct}")

    ff, src = find_ffmpeg(getattr(args, "ffmpeg", None))
    info(f"ffmpeg        : {ff or '未找到'}" + (f"  （来源：{src}）" if ff else ""))
    probe = ffmpeg_probe(ff)
    if ff:
        info(f"  版本        : {probe['version']}")
        info(f"  libass      : {'是' if probe['libass'] else '否  → 无法烧录，只能出 SRT'}")
        info(f"  fontconfig  : {'是' if probe['fontconfig'] else '否（用 fontsdir 兜底）'}")
        info(f"  subtitles 滤镜: {'可用' if probe['subtitles_filter'] else '不可用'}")

    fp, fam = detect_cjk_font()
    info(f"中文字体      : {fp or '未找到'}" + (f"  → {fam}" if fam else ""))

    # 只读探测 CUDA 数量，不做严格校验（doctor 不该因为没显卡就报错）
    n_cuda = cuda_device_count()
    device = "cuda" if n_cuda > 0 else "cpu"
    info("-" * 56)
    info(f"自动选择      : device={device}  compute_type={resolve_compute_type('auto', device)}"
         f"  model={resolve_model('auto', device)}")
    if device == "cpu":
        info("提示：没检测到 CUDA，CPU 跑 large 会很慢；默认已选 small + int8。")

    info("-" * 56)
    print_model_table()

    info("")
    info("结论：")
    info("  只要 SRT         : " + ("可以" if fw else "需要先装 faster-whisper"))
    info("  烧录成字幕视频   : " + ("可以" if (ff and fw and probe["subtitles_filter"]) else "条件不足，见上面"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=APP,
        description="跨平台视频自动加字幕：本地 Whisper 转写 + ffmpeg 烧录/软字幕",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  autosub trip.mp4                    # SRT + 烧录版视频\n"
            "  autosub trip.mp4 --srt-only         # 只出 SRT（不需要 ffmpeg）\n"
            "  autosub trip.mp4 --soft             # 只嵌软字幕\n"
            "  autosub trip.mp4 -m large-v3 --language zh\n"
            "  autosub trip.mp4 -m D:\\models\\large-v3 --offline   # 用本地权重，不联网\n"
            "  autosub *.mp4 -o ./subs             # 批量\n"
            "  autosub download-model large-v3     # 预先下载模型权重\n"
            "  autosub gui                         # 浏览器界面\n"
            "  autosub doctor                      # 环境自检 + 模型缓存状态\n"
        ),
    )
    p.add_argument("-V", "--version", action="version", version=f"{APP} {VERSION}")
    p.add_argument("video", nargs="*", help="视频文件（可多个），或 doctor / gui")

    g = p.add_argument_group("转写")
    g.add_argument("-m", "--model", default="auto", type=model_arg,
                   help="Whisper 模型名（默认 auto：有显卡 large-v3，否则 small），"
                        "或一个本地模型目录路径")
    g.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])
    g.add_argument("--compute-type", default="auto", choices=COMPUTE_CHOICES)
    g.add_argument("--language", default=None,
                   help="强制语言，如 zh / en；不填则自动识别")
    g.add_argument("--initial-prompt", default=None,
                   help="提示词，用来喂专有名词（地名、人名、术语），能明显提升准确率")
    g.add_argument("--no-vad", action="store_true", help="关闭静音切分（默认开启）")
    g.add_argument("--hf-mirror", action="store_true",
                   help="用 hf-mirror.com 下载模型（国内网络推荐）")
    g.add_argument("--offline", action="store_true",
                   help="只用本地已下载的权重，绝不联网（缺文件直接报错）")

    g = p.add_argument_group("字幕排版")
    g.add_argument("--max-chars", type=int, default=16, help="每行最多字符数，默认 16")
    g.add_argument("--max-lines", type=int, default=2, help="每条字幕最多行数，默认 2")
    g.add_argument("--max-duration", type=float, default=6.0, help="单条最长秒数，默认 6.0")
    g.add_argument("--min-duration", type=float, default=0.8, help="单条最短秒数，默认 0.8")
    g.add_argument("--no-wrap", action="store_true", help="不自动折行，保留原样")

    g = p.add_argument_group("输出")
    g.add_argument("-o", "--outdir", default=None, help="输出目录，默认与视频同目录")
    m = g.add_mutually_exclusive_group()
    m.add_argument("--burn", dest="mode", action="store_const", const="burn",
                   help="烧录进画面（默认）")
    m.add_argument("--soft", dest="mode", action="store_const", const="soft",
                   help="嵌入软字幕轨（播放器可开关）")
    m.add_argument("--srt-only", dest="mode", action="store_const", const="srt",
                   help="只输出 SRT 字幕文件")
    g.set_defaults(mode="burn")
    g.add_argument("--keep-temp", action="store_true", help="保留临时目录，便于排查")

    g = p.add_argument_group("烧录样式（仅 --burn 生效）")
    g.add_argument("--font-file", default=None, help="指定字体文件（ttf/otf/ttc）")
    g.add_argument("--font-name", default=None, help="指定字体家族名，覆盖自动检测")
    g.add_argument("--font-size", type=int, default=20, help="字号，默认 20")
    g.add_argument("--font-color", default="FFFFFF", help="字体颜色，如 FFFFFF / FFD700")
    g.add_argument("--outline", type=int, default=2, help="描边粗细，默认 2")
    g.add_argument("--margin-v", type=int, default=28, help="距底部像素，默认 28")
    g.add_argument("--video-codec", default="libx264",
                   help="视频编码器，默认 libx264；N 卡可用 h264_nvenc，Mac 可用 h264_videotoolbox")
    g.add_argument("--preset", default="veryfast", help="x264 速度档，默认 veryfast")
    g.add_argument("--crf", type=int, default=21, help="画质，数字越小越清晰，默认 21")

    p.add_argument("--ffmpeg", default=None, help="手动指定 ffmpeg 路径")
    p.add_argument("-q", "--quiet", action="store_true", help="减少输出")
    return p


def settings_from_args(args) -> Settings:
    return Settings(
        outdir=args.outdir,
        mode=args.mode,
        model=args.model,
        device=args.device,
        compute_type=args.compute_type,
        language=args.language,
        initial_prompt=args.initial_prompt,
        vad=not args.no_vad,
        hf_mirror=args.hf_mirror,
        offline=args.offline,
        max_chars=args.max_chars,
        max_lines=args.max_lines,
        max_duration=args.max_duration,
        min_duration=args.min_duration,
        wrap=not args.no_wrap,
        font_file=args.font_file,
        font_name=args.font_name,
        font_size=args.font_size,
        font_color=args.font_color,
        outline=args.outline,
        margin_v=args.margin_v,
        video_codec=args.video_codec,
        preset=args.preset,
        crf=args.crf,
        keep_temp=args.keep_temp,
        ffmpeg=args.ffmpeg,
    )


def cmd_download_model(argv: list[str]) -> int:
    """autosub download-model <模型名> [-o 目录] [--list]"""
    p = argparse.ArgumentParser(
        prog=f"{APP} download-model",
        description="预先下载 Whisper 模型权重（不跑转写）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  autosub download-model large-v3            # 下到 HF 缓存，之后 -m large-v3 直接用\n"
            "  autosub download-model large-v3 -o D:\\mdl  # 下成纯文件目录，可整个拷到离线机器\n"
            "  autosub download-model --list              # 看各模型体积与本地缓存状态\n"
            "\n离线部署：把模型目录拷到目标机器的 %USERPROFILE%\\.cache\\huggingface\\hub\\ 下，\n"
            "或设 HF_HOME 指向拷过去的目录，再加 --offline 运行即可。\n"
        ),
    )
    p.add_argument("model", nargs="?", help="模型名（tiny/base/small/medium/large-v3/...）")
    p.add_argument("-o", "--outdir", default=None,
                   help="下载到指定目录（纯文件形式，可用 -m 直接指向它）")
    p.add_argument("--hf-mirror", action="store_true", help="用 hf-mirror.com（国内网络推荐）")
    p.add_argument("--list", action="store_true", help="列出所有模型及其本地缓存状态")
    args = p.parse_args(argv)

    if args.list or not args.model:
        info(f"{APP} {VERSION} · 模型列表")
        print_model_table()
        return 0

    name = args.model
    info(f"{APP} {VERSION} · 预下载模型 {name}")
    if name not in MODEL_CHOICES and "/" not in name:
        warn(f"「{name}」不是内置模型名，将按 HuggingFace 仓库名处理。")
    out = snapshot_download_model(name, args.outdir, args.hf_mirror)
    info("")
    info("接下来可以这样用：")
    if args.outdir:
        info(f"  {APP} 视频.mp4 -m \"{out}\"")
        info("  拷到离线机器后，把整个目录带过去，用同样的 -m 路径即可（也可加 --offline）。")
    else:
        info(f"  {APP} 视频.mp4 -m {name}")
        info("  想拷给别的机器：把上面那个缓存目录整个复制过去，放到对方的 HF 缓存里。")
    return 0


def main(argv: list[str] | None = None) -> int:
    safe_streams()
    argv = list(sys.argv[1:] if argv is None else argv)

    # autosub gui [--port 8765 ...]
    if argv and argv[0].lower() in GUI_ALIASES:
        from .gui import main as gui_main
        return gui_main(argv[1:])

    # autosub download-model <名字> [-o 目录]
    if argv and argv[0].lower() in DOWNLOAD_ALIASES:
        try:
            return cmd_download_model(argv[1:])
        except AutosubError as e:
            warn(str(e))
            return 1

    args = build_parser().parse_args(argv)

    if args.video and args.video[0].lower() == "doctor":
        return cmd_doctor(args)
    if not args.video:
        build_parser().print_help()
        return 0

    videos: list[Path] = []
    for v in args.video:
        p = Path(v).expanduser()
        if not p.exists():
            warn(f"跳过，文件不存在：{p}")
            continue
        if p.is_dir():
            found = sorted([q for q in p.glob("*") if q.suffix.lower() in VIDEO_EXTS])
            info(f"目录 {p} 中找到 {len(found)} 个视频文件")
            videos.extend(found)
            continue
        if p.suffix.lower() not in VIDEO_EXTS:
            warn(f"注意：{p.name} 后缀不常见，仍按视频尝试处理。")
        videos.append(p)

    if not videos:
        warn("没有可处理的视频。")
        return 1

    info(f"{APP} {VERSION} · 视频自动加字幕")
    device = resolve_device(args.device)
    info(f"环境：{sys.platform} · Python {sys.version.split()[0]} · "
         f"device={device} · model={resolve_model(args.model, device)}")

    ffmpeg, ff_src = find_ffmpeg(args.ffmpeg)
    probe = ffmpeg_probe(ffmpeg)
    if args.mode != "srt":
        if not ffmpeg:
            warn("未检测到 ffmpeg，只能输出 SRT 字幕文件。")
        elif args.mode == "burn" and not probe.get("subtitles_filter"):
            warn(f"ffmpeg（{ff_src}）缺少 subtitles 滤镜，烧录会失败，建议改用 --soft。")
        else:
            info(f"ffmpeg：{ff_src} · libass={'有' if probe.get('libass') else '无'}")

    s = settings_from_args(args)
    t0 = time.time()
    try:
        failed = process_many(videos, s, ffmpeg, probe)
    except AutosubError as e:
        warn(str(e))
        return 1

    info("\n" + "-" * 60)
    info(f"全部结束：{len(videos) - failed} 成功 / {failed} 失败，总计 {time.time() - t0:.1f}s")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        sys.exit(130)
