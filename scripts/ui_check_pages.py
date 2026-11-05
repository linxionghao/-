# -*- coding: utf-8 -*-
"""聊天页与主管后台的真实浏览器巡检（Playwright + 本机 Chrome）。

覆盖：
1. 质量工程师问答页：加载 → 提问 → SSE 流式回答/降级 → 引用卡片渲染；
2. 主管后台：登录 → 仪表盘 → 知识库 → 提示词 → 对话审计 → 归因审批 → 归因详情。

用法：python scripts/ui_check_pages.py [端口]
产物：docs/screenshots/*.png
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / "docs" / "screenshots"


def _shot(page, name: str, full: bool = False) -> None:
    page.screenshot(path=str(SHOTS / name), full_page=full)
    print(f"  · 截图 {name}")


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else "8000"
    base = f"http://127.0.0.1:{port}"
    SHOTS.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)

        # ---------------- 聊天页 ----------------
        print("[1/2] 质量工程师问答页")
        page = browser.new_page(viewport={"width": 1180, "height": 900})
        page.on("pageerror", lambda e: errors.append(f"chat: {e}"))
        page.goto(f"{base}/", wait_until="networkidle")
        page.fill("#question", "螺纹检验的抽样方案是什么？")
        page.click("#send")
        # 等回答区出现内容（无 LLM Key 时是降级提示 + 引用）
        page.wait_for_selector("#messages .msg", timeout=20000)
        page.wait_for_timeout(2500)
        _shot(page, "chat-answer.png")
        body = page.inner_text("#messages")
        assert "螺纹" in body or "降级" in body or "依据" in body, "回答区没有渲染内容"
        print("  ✓ 提问与回答区渲染正常")

        # ---------------- 主管后台 ----------------
        print("[2/2] 质量主管后台")
        admin = browser.new_page(viewport={"width": 1280, "height": 900})
        admin.on("pageerror", lambda e: errors.append(f"admin: {e}"))
        admin.goto(f"{base}/admin/login", wait_until="networkidle")
        admin.fill("input[name=username]", "manager01")
        admin.fill("input[name=password]", "manager01")
        admin.click("button[type=submit]")
        admin.wait_for_url("**/admin/", timeout=10000)
        assert "仪表盘" in admin.content()
        _shot(admin, "admin-dashboard.png")

        for path, name, keyword in [
            ("/admin/documents", "admin-documents.png", "知识库"),
            ("/admin/prompts", "admin-prompts.png", "提示词"),
            ("/admin/audit", "admin-audit.png", "审计"),
            ("/admin/attributions", "admin-attributions.png", "归因"),
        ]:
            admin.goto(base + path, wait_until="networkidle")
            assert keyword in admin.content(), f"{path} 页面未渲染"
            _shot(admin, name, full=True)
            print(f"  ✓ {path} 渲染正常")

        # 归因详情（取列表第一个链接）
        link = admin.query_selector("a[href*='/admin/attributions/']")
        if link:
            admin.goto(base + link.get_attribute("href"), wait_until="networkidle")
            _shot(admin, "admin-attribution-detail.png", full=True)
            print("  ✓ 归因详情渲染正常")

        browser.close()

    if errors:
        print("✗ 页面 JS 异常：")
        for item in errors:
            print("   ", item)
        return 1
    print("全部页面巡检通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
