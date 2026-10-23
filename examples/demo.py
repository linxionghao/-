#!/usr/bin/env python
"""可运行的示例：加载默认配置，跑一遍流水线并打印结果。

用法:
    python examples/demo.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quality_trace import __version__  # noqa: E402
from quality_trace.core.pipeline import run  # noqa: E402
from quality_trace.utils.config import load_config  # noqa: E402


def main():
    config_path = ROOT / "config" / "default.yaml"
    config = load_config(config_path)

    print("=" * 52)
    print("quality-trace v" + __version__ + " 示例")
    print("配置文件: " + str(config_path.relative_to(ROOT)))
    print("=" * 52)

    summary = run(config, verbose=True)
    for line in summary["checks"]:
        print("  - " + line)

    print("-" * 52)
    print("结果: " + summary["status"])
    return 0 if summary["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
