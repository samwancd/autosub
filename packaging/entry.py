# -*- coding: utf-8 -*-
"""PyInstaller 的多态入口（只给 --onefile 单文件模式用）。

默认的分目录模式会分别用 entry_cli.py 和 entry_gui.py 打出两个可执行文件；
单文件模式下只能打一个 exe，就靠这个入口同时承担两种角色：

    autosub <视频> ...     → 命令行
    autosub gui            → 浏览器界面
    autosub doctor         → 环境自检
"""

import multiprocessing
import sys


def main() -> int:
    # 冻结后如果代码里有用到多进程，这行能避免子进程反复拉起主程序
    multiprocessing.freeze_support()

    from autosub.cli import main as cli_main

    try:
        return cli_main()
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
