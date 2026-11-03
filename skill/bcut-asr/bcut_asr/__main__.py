import sys
from argparse import ArgumentParser

from . import (
    DEFAULT_POLL_INTERVAL,
    DEFAULT_TASK_TIMEOUT,
    INFILE_FMT,
    OUTFILE_FMT,
    __version__,
    ffmpeg_render,  # noqa: F401  兼容旧的 `from bcut_asr.__main__ import ffmpeg_render`
    run_everywhere,
)


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(
        prog="bcut-asr",
        description="必剪语音识别\n",
        epilog=f"支持输入音频格式: {', '.join(INFILE_FMT)}  支持自动调用ffmpeg提取视频伴音",
    )
    parser.add_argument(
        "-V", "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "-f",
        "--format",
        nargs="?",
        default=None,
        choices=OUTFILE_FMT,
        help="输出字幕格式, 默认按输出文件名后缀推断, 无后缀时为 srt",
    )
    parser.add_argument(
        "-i",
        "--interval",
        nargs="?",
        type=float,
        default=DEFAULT_POLL_INTERVAL,
        metavar="3.0",
        help="任务状态轮询间隔(秒), 过快会被必剪风控",
    )
    parser.add_argument(
        "-t",
        "--timeout",
        nargs="?",
        type=float,
        default=DEFAULT_TASK_TIMEOUT,
        metavar="3600",
        help="单个识别任务最长等待时间(秒)",
    )
    parser.add_argument("input", help="输入媒体文件")
    parser.add_argument(
        "output",
        nargs="?",
        help="输出字幕文件, 省略则与输入文件同名; '-' 表示输出到 stdout",
    )
    return parser


parser = build_parser()


def main(argv=None) -> int:
    args = parser.parse_args(argv)
    return run_everywhere(args)


if __name__ == "__main__":
    sys.exit(main())
