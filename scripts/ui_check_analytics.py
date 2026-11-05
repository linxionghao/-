# -*- coding: utf-8 -*-
"""质量看板页面巡检（Playwright + 本机 Chrome，headless）。

检查的不只是"页面能打开"，而是四条真正会坏掉的东西：

1. **登录态**：Cookie 鉴权能否让 /admin/analytics 通过，而不是被 303 踢回登录页；
2. **数据落地**：KPI 卡片里出现真实数字（不是 "—"），说明接口与页面接上了；
3. **图真的画出来了**：SVG 里必须有 path/rect/circle，且数量合理——
   只看"元素存在"不够，空图也是存在的；
4. **控制图能切项目**：选一个检验项目后，X̄ 图与 R 图两块都要出现，
   并且红色异常点（fill=#b0343c 的 circle）至少一个——演示数据里故意留了漂移。

用法：
    python scripts/ui_check_analytics.py [端口]         # 默认 8011
    python scripts/ui_check_analytics.py 8011 --shot    # 额外保存截图到 docs/screenshots
"""

from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
SHOT_DIR = BASE / "docs" / "screenshots"


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else "8011"
    save_shot = "--shot" in sys.argv
    base = "http://127.0.0.1:%s" % port

    from playwright.sync_api import sync_playwright

    failures = []
    checks = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))
        if not ok:
            failures.append("%s %s" % (name, detail))

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})

        # ---- 1. 登录（表单提交，拿到 HttpOnly Cookie）----
        page.goto(base + "/admin/login", wait_until="domcontentloaded")
        page.fill("input[name=username]", "manager01")
        page.fill("input[name=password]", "manager01")
        page.click("button[type=submit]")
        page.wait_for_url("**/admin/**", timeout=15000)
        check("登录后跳转后台", "/admin" in page.url, page.url)

        # ---- 2. 打开看板 ----
        page.goto(base + "/admin/analytics", wait_until="domcontentloaded")
        check("看板未被踢回登录页", "/admin/login" not in page.url, page.url)
        page.wait_for_selector("#kpis .stat", timeout=15000)

        # ---- 3. KPI 卡片 ----
        nums = page.eval_on_selector_all(
            "#kpis .num", "els => els.map(e => e.textContent.trim())"
        )
        check("KPI 卡片 6 个", len(nums) == 6, str(nums))
        check("KPI 有真实数字", any(n not in ("—", "") for n in nums), str(nums))

        # ---- 4. 结论横幅 ----
        verdict = page.inner_text("#verdict")
        check("结论非空", len(verdict) > 10, verdict[:40])
        check("结论含 PPM", "PPM" in verdict or "不良" in verdict, verdict[:40])

        # ---- 5. 图真的画了 ----
        page.wait_for_timeout(600)
        for host, label in (("#chart-trend", "趋势图"), ("#chart-pareto", "帕累托")):
            # 折线图 = path + 点（circle），柱状图 = rect + 累计线（path），
            # 所以三种元素一起数，只要求"总共有画出来的东西"
            n_path = page.eval_on_selector_all(host + " svg path", "els => els.length")
            n_rect = page.eval_on_selector_all(host + " svg rect", "els => els.length")
            n_circle = page.eval_on_selector_all(host + " svg circle", "els => els.length")
            check("%s 有图形" % label, n_path + n_rect + n_circle >= 2,
                  "path=%d rect=%d circle=%d" % (n_path, n_rect, n_circle))

        rows = page.eval_on_selector_all("#pareto-table tbody tr", "els => els.length")
        check("帕累托表有行", rows >= 2, str(rows))

        # ---- 6. 切换控制图 ----
        items = page.eval_on_selector_all(
            "#f-item option", "els => els.map(e => e.value).filter(v => v)"
        )
        check("有可选检验项目", len(items) >= 1, str(items))
        if items:
            page.select_option("#f-item", items[0])
            page.wait_for_timeout(1200)
            check("控制图出现 X 图", page.locator("#spc-x svg").count() == 1, "")
            has_second = (page.locator("#spc-r svg").count()
                          + page.locator("#spc-mr svg").count())
            check("控制图出现极差/移动极差图", has_second == 1, "")
            ooc = page.eval_on_selector_all(
                "#spc-x circle", "els => els.filter(e => e.getAttribute('fill') === '#b0343c').length"
            )
            check("异常点被标红（演示数据含漂移）", ooc >= 1, "红点=%d" % ooc)
            cap_text = page.inner_text("#spc-body")
            check("能力指标有输出", "Cpk" in cap_text, cap_text[:60].replace("\n", " "))

        # ---- 7. 截图 ----
        if save_shot:
            SHOT_DIR.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(SHOT_DIR / "10-analytics.png"), full_page=True)
            print("截图已保存：docs/screenshots/10-analytics.png")

        browser.close()

    print("\n=== 看板巡检 ===")
    for name, ok, detail in checks:
        print(("  ✓ " if ok else "  ✗ ") + name + (("  " + detail) if detail and not ok else ""))
    print("\n%d 项通过 / %d 项失败" % (len(checks) - len(failures), len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
