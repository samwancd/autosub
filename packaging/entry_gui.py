# -*- coding: utf-8 -*-
"""GUI 专用入口（打包成 autosub-gui / autosub-gui.exe）。

双击即用：起一个本地 HTTP 服务，自动打开浏览器当界面。
不需要再敲 `gui` 子命令（敲了也认）。

    autosub-gui                      → 127.0.0.1:8765，自动开浏览器
    autosub-gui --port 9000          → 换端口
    autosub-gui --no-browser         → 只启动服务，不开浏览器
    autosub-gui --host 0.0.0.0       → 同网段其它设备也能访问
"""

import multiprocessing
import sys

_SUBCOMMANDS = {"gui", "ui", "web", "serve", "server"}


def main() -> int:
    multiprocessing.freeze_support()

    argv = list(sys.argv[1:])
    # 容错：有人习惯写 `autosub-gui gui`，把冗余的子命令吃掉
    if argv and argv[0].lower() in _SUBCOMMANDS:
        argv = argv[1:]

    from autosub.gui import main as gui_main

    try:
        return gui_main(argv)
    except KeyboardInterrupt:
        print("\n已停止。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
