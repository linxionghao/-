"""命令行入口。

只做三件事：解析参数、加载配置、调用核心流水线并输出结果。
业务逻辑一律放在 core/ 里，此处不得出现。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__
from .core.pipeline import run
from .utils.config import load_config

DEFAULT_CONFIG = Path("config") / "default.yaml"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quality-trace",
        description="制造业质量追溯与缺陷归因工具",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s " + __version__,
    )
    parser.add_argument(
        "-c",
        "--config",
        default=str(DEFAULT_CONFIG),
        help="配置文件路径（默认 config/default.yaml）",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="输出详细过程",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = load_config(Path(args.config))
    except FileNotFoundError:
        print(f"[error] 找不到配置文件: {args.config}", file=sys.stderr)
        return 2

    summary = run(config, verbose=args.verbose)

    print(f"quality-trace v{__version__}")
    print(f"配置: {args.config}")
    for line in summary.get("checks", []):
        print(f"  - {line}")
    print(f"结果: {summary.get('status', 'unknown')}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
