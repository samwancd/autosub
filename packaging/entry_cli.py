# -*- coding: utf-8 -*-
"""CLI 专用入口（打包成 autosub / autosub.exe）。

    autosub video.mp4            → 出 video.srt + 带字幕的视频
    autosub video.mp4 --soft     → 软字幕轨
    autosub video.mp4 --srt-only → 只出字幕文件
    autosub doctor               → 环境自检

子命令 `gui` 仍然保留，方便同一个二进制既能当命令行又能开界面。
"""

import multiprocessing
import sys


def main() -> int:
    # 冻结后如果有子进程，这行能避免无限自我拉起
    multiprocessing.freeze_support()

    from autosub.cli import main as cli_main

    try:
        return cli_main()
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
