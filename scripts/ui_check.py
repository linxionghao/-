# -*- coding: utf-8 -*-
"""移动端页面真实浏览器验证（Playwright + 本机 Chrome）。

为什么需要这个脚本：接口冒烟只能证明后端对，页面是否有 JS 报错、
步骤条能否推进、候选码按钮能否点、回执是否渲染，必须真跑一遍浏览器。

用法：
    python scripts/ui_check.py            # 需服务已在 8000 端口运行
    python scripts/ui_check.py 8011       # 指定端口

产物：docs/screenshots/mobile-{1,2,3}.png（README 与验收留证）
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SHOT_DIR = ROOT / "docs" / "screenshots"


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else "8000"
    base = f"http://127.0.0.1:{port}"
    SHOT_DIR.mkdir(parents=True, exist_ok=True)

    console_errors: list[str] = []
    page_errors: list[str] = []

    with sync_playwright() as p:
        # 用本机已装 Chrome，避免额外下载浏览器
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.on("console", lambda m: console_errors.append(m.text)
                if m.type == "error" else None)
        page.on("pageerror", lambda e: page_errors.append(str(e)))

        # ---- 1) 打开页面：应显示登录遮罩 ----
        page.goto(f"{base}/m", wait_until="networkidle")
        assert page.is_visible("#login-mask"), "登录遮罩未显示"
        page.screenshot(path=str(SHOT_DIR / "mobile-1-login.png"))
        print("✓ 页面加载，登录遮罩显示")

        # ---- 2) 登录 ----
        page.fill("#username", "leader01")
        page.fill("#password", "leader01")
        page.click("#btn-login")
        # 注意：wait_for_selector 默认等「可见」，而登录成功后遮罩是 display:none，
        # 必须用 wait_for_function 判断 class，否则会一直等到超时。
        page.wait_for_function(
            "() => document.querySelector('#login-mask').classList.contains('hidden')",
            timeout=8000,
        )
        page.wait_for_selector("#recent-list .list-item", timeout=8000, state="attached")
        print("✓ 班组长登录成功，最近记录已渲染")

        # ---- 3) 填单 ----
        page.fill("#work-order", "WO-20261030-001")
        page.click("#op-chips .chip:nth-child(3)")     # OP30
        page.fill("#description", "螺纹外径偏小，通规止端过不去")
        page.click("#sev-chips .chip[data-sev='严重']")
        page.click("#btn-submit")

        # ---- 4) 第 2 步：候选码 ----
        page.wait_for_selector("#step2:not(.hidden)", timeout=10000)
        page.wait_for_selector("#suggest-area .chip", timeout=8000)
        draft_no = page.inner_text("#draft-no")
        assert draft_no.startswith("DF-"), f"草稿号异常：{draft_no}"
        page.screenshot(path=str(SHOT_DIR / "mobile-2-confirm.png"), full_page=True)
        print(f"✓ 建单成功，进入确认步骤，草稿号 {draft_no}")

        # ---- 5) 点选候选码 → 回执 ----
        page.click("#suggest-area .chip")
        page.wait_for_selector("#step3:not(.hidden)", timeout=8000)
        receipt = page.inner_text("#receipt-no")
        status = page.inner_text("#receipt-status")
        page.screenshot(path=str(SHOT_DIR / "mobile-3-receipt.png"))
        assert receipt.startswith("DF-"), f"回执单号异常：{receipt}"
        print(f"✓ 确认后回执：{receipt} / {status}")

        browser.close()

    # ---- JS 错误检查（页面白屏最常见原因）----
    if page_errors:
        print("✗ 页面 JS 异常：")
        for item in page_errors:
            print("   ", item)
        return 1
    if console_errors:
        print("⚠ 控制台错误（可能来自资源加载）：")
        for item in console_errors[:5]:
            print("   ", item)

    print("全部通过：登录 → 填单 → 确认缺陷码 → 回执")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
