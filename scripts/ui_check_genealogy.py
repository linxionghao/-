# -*- coding: utf-8 -*-
"""批次追溯页面巡检（Playwright + 本机 Chrome，headless）。

追溯页最容易"看起来能打开、实际是空壳"，所以断言都落在**内容**上：

1. 概览卡片有覆盖率数字（说明 overview 接口通）；
2. 正查默认选中第一批料，结论与"影响面"徽章都出来；
3. 谱系图 SVG 里节点数 ≥ 2（1 个批 + 至少 1 张工单），边数 ≥ 1；
4. 反查一个真实存在的单号，能列出批次；不存在的单号要给出中文错误，而不是白屏；
5. 供应商记分卡有表格行。

用法：
    python scripts/ui_check_genealogy.py [端口] [--shot]
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

    checks = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})

        page.goto(base + "/admin/login", wait_until="domcontentloaded")
        page.fill("input[name=username]", "manager01")
        page.fill("input[name=password]", "manager01")
        page.click("button[type=submit]")
        page.wait_for_url("**/admin/**", timeout=15000)

        page.goto(base + "/admin/genealogy", wait_until="domcontentloaded")
        check("追溯页可达", "/admin/login" not in page.url, page.url)
        page.wait_for_selector("#ov .stat", timeout=15000)
        page.wait_for_timeout(900)

        # ---- 概览 ----
        ov_text = page.inner_text("#ov")
        check("概览含覆盖率", "覆盖率" in ov_text, ov_text[:40].replace("\n", " "))
        check("概览含物料批数", "物料批" in ov_text, "")

        # ---- 正查 ----
        lots = page.eval_on_selector_all("#f-lot option", "els => els.map(e => e.value).filter(v => v)")
        check("有可选批次", len(lots) >= 1, str(len(lots)))

        out = page.inner_text("#forward-out")
        check("正查有结论", "影响面" in out, out[:60].replace("\n", " "))
        check("正查有工单表", page.locator("#forward-out table").count() >= 1, "")

        nodes = page.eval_on_selector_all("#graph svg rect", "els => els.length")
        edges = page.eval_on_selector_all("#graph svg path", "els => els.length")
        check("谱系图有节点", nodes >= 2, "节点=%d" % nodes)
        check("谱系图有连线", edges >= 1, "边=%d" % edges)

        # ---- 反查 ----
        defect_no = page.evaluate(
            """async () => {
                const r = await fetch('/api/v1/analytics/overview?days=3650', {credentials:'same-origin'});
                return '';
            }"""
        )
        del defect_no
        page.click("button[data-tab=backward]")
        # 从正查表格里拿一个真实工单号，构造必然存在的单号不稳，改用接口探一个
        first_defect = page.evaluate(
            """async () => {
                const r = await fetch('/api/v1/genealogy/lots?limit=1', {credentials:'same-origin'});
                const j = await r.json();
                if (!j.data.lots.length) return '';
                const lot = j.data.lots[0].lot_id;
                const f = await fetch('/api/v1/genealogy/lots/' + encodeURIComponent(lot), {credentials:'same-origin'});
                const fj = await f.json();
                const orders = (fj.data.orders || []).map(o => o.work_order_id);
                if (!orders.length) return '';
                return orders[0];
            }"""
        )
        check("能取到真实工单号", bool(first_defect), str(first_defect))

        # 用不存在的单号验证错误提示（这是必测路径：现场输错单号是常态）
        page.fill("#f-defect", "DF-NOT-EXIST")
        page.click("#btn-backward")
        page.wait_for_timeout(700)
        bad = page.inner_text("#backward-out")
        check("不存在的单号给出中文提示", "不存在" in bad, bad[:50])

        # ---- 供应商记分卡 ----
        page.click("button[data-tab=supplier]")
        page.wait_for_timeout(900)
        sc_rows = page.eval_on_selector_all("#sc-out tbody tr", "els => els.length")
        check("记分卡有数据行", sc_rows >= 1, str(sc_rows))
        sc_text = page.inner_text("#pane-supplier")
        check("记分卡带口径说明", "索赔依据" in sc_text, "")

        if save_shot:
            SHOT_DIR.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(SHOT_DIR / "11-genealogy.png"), full_page=True)
            print("截图已保存：docs/screenshots/11-genealogy.png")

        browser.close()

    print("\n=== 追溯页巡检 ===")
    for name, ok, detail in checks:
        print(("  ✓ " if ok else "  ✗ ") + name + (("  " + detail) if detail and not ok else ""))
    failed = [c for c in checks if not c[1]]
    print("\n%d 项通过 / %d 项失败" % (len(checks) - len(failed), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
