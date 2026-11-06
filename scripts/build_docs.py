# -*- coding: utf-8 -*-
"""一键重建交付文档（test/ 与 docs/ 下的 docx）。

为什么要有这个脚本
------------------
docx 是从代码与实测证据生成的产物，不是手改的文件。散着敲几条命令的问题
是"改了 md 忘了重转 docx"，两份内容就对不上了。这里把顺序固定下来：

    1. gen_test_evidence.py  真实跑 41 条场景用例 → test/_evidence.json + 配图
    2. gen_test_docs.py      生成 5 份场景过程文档 + 测试方案 docx
    3. md2docx.py            转 MVP部署地址.md、上线交付运行手册.md

用法：
    D:\\python\\python.exe scripts/build_docs.py            # 全量
    D:\\python\\python.exe scripts/build_docs.py --skip-evidence   # 复用现有证据
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable

#: (md, docx, 页眉)：可直接用 md 作唯一内容源的文档
MD_DOCS = [
    (
        "test/MVP部署地址.md",
        "test/MVP部署地址.docx",
        "星衡精密 · 质量追溯与缺陷归因系统 | MVP 部署地址说明",
    ),
    (
        "docs/9、上线交付运行手册.md",
        "docs/9、上线交付运行手册.docx",
        "星衡精密 · 质量追溯与缺陷归因系统 | 上线交付运行手册",
    ),
]


def run(args: list) -> None:
    print("\n$", " ".join(str(a) for a in args))
    r = subprocess.run([str(a) for a in args], cwd=str(ROOT))
    if r.returncode != 0:
        raise SystemExit("命令失败：%s" % args)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="重建交付文档")
    ap.add_argument("--skip-evidence", action="store_true",
                    help="跳过场景用例重跑，复用 test/_evidence.json")
    args = ap.parse_args(argv)

    if not args.skip_evidence:
        run([PY, "scripts/gen_test_evidence.py"])

    # 场景文档与测试方案 docx
    run([PY, "scripts/gen_test_docs.py"])

    # md → docx
    for md, dst, header in MD_DOCS:
        run([PY, "scripts/md2docx.py", md, dst, "--header", header])

    print("\n交付文档已重建：")
    for p in sorted((ROOT / "test").glob("*.docx")) + sorted((ROOT / "docs").glob("上线*.docx")):
        print("  %-40s %6.1f KB" % (p.relative_to(ROOT), p.stat().st_size / 1024))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
