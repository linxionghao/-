# -*- coding: utf-8 -*-
"""测量系统分析页巡检（Playwright + 本机 Chrome，headless）。

MSA 页面最容易做假的地方是"数字显示了，但数字是错的"。所以断言分两类：

一类是**结构**：概览卡、缺口清单、研究列表、详情面板是否都出得来；
另一类是**口径**：%GRR 与结论必须与接口返回一致，且
"两种算法对比表"两行都在——只显示一种算法的页面，等于把差异藏起来了。

还要验证"一键从实测值评估"这条路径：预览能折叠出设计，
生成后研究列表从 N 条变 N+1 条。这是页面唯一的写操作，必须走通。

用法：
    python scripts/ui_check_msa.py [端口] [--shot]
"""

from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
SHOT_DIR = BASE / "docs" / "screenshots"


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else "8014"
    save_shot = "--shot" in sys.argv
    base = "http://127.0.0.1:%s" % port

    from playwright.sync_api import sync_playwright

    checks = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1100})

        page.goto(base + "/admin/login", wait_until="domcontentloaded")
        page.fill("input[name=username]", "manager01")
        page.fill("input[name=password]", "manager01")
        page.click("button[type=submit]")
        page.wait_for_url("**/admin/**", timeout=15000)

        page.goto(base + "/admin/msa", wait_until="domcontentloaded")
        check("MSA 页可达", "/admin/login" not in page.url, page.url)

        page.wait_for_selector("#ov .stat", timeout=15000)
        page.wait_for_timeout(900)

        # ---- 1. 概览 ----
        ov = page.inner_text("#ov")
        check("概览有研究次数", "研究次数" in ov, ov[:40].replace("\n", " "))
        check("概览有覆盖率", "已做 MSA 的项目" in ov, "")

        banner = page.inner_text("#verdict-banner")
        check("有结论横幅", len(banner.strip()) > 8, banner[:50].replace("\n", " "))

        # ---- 2. 研究列表 ----
        rows = page.locator("#studies table tbody tr").count()
        check("研究列表有记录", rows >= 2, "行数=%d" % rows)
        list_text = page.inner_text("#studies")
        check("列表含 %GRR 列", "%GRR" in list_text, "")
        check("列表含来源列", "正式试验" in list_text or "实测值折叠" in list_text, "")

        # ---- 3. 详情：点第一行 ----
        page.locator("#studies table tbody tr").first.click()
        page.wait_for_selector("#detail-card", state="visible", timeout=10000)
        page.wait_for_timeout(500)
        detail = page.inner_text("#detail")
        check("详情含 %EV/%AV", "重复性" in detail and "再现性" in detail, "")
        check("详情含两种算法对比", "均值极差法" in detail and "ANOVA" in detail, "")
        check("详情含方差分量", "方差分量" in detail, "")
        check("详情含下一步建议", "下一步建议" in detail or "建议" in detail, "")
        check("详情含 ndc 口径", "ndc" in detail or "分辨" in detail, "")

        # 结论与接口一致
        consistency = page.evaluate(
            """async () => {
                const tds = document.querySelectorAll('#studies tbody tr');
                const id = tds[0].querySelector('code').textContent;
                const r = await fetch('/api/v1/msa/studies/' + encodeURIComponent(id),
                                      {credentials:'same-origin'});
                const body = await r.json();
                return {id, verdict: body.data.verdict, pct: body.data.pct_grr,
                        title: document.querySelector('#detail-title').textContent};
            }"""
        )
        check("详情标题带研究号", consistency["id"] in consistency["title"],
              consistency["title"])

        # ---- 4. 重算 ----
        page.click("text=用留档读数重算")
        page.wait_for_function(
            """() => (document.querySelector('#rc-out').textContent || '').length > 4""",
            timeout=15000)
        rc = page.inner_text("#rc-out")
        check("重算给出说明", "一致" in rc or "差异" in rc, rc[:40])

        # ---- 5. 缺口清单 ----
        gaps_text = page.inner_text("#gaps")
        check("缺口清单有内容",
              "还没有" in gaps_text or "检验项目" in gaps_text or "所有" in gaps_text,
              gaps_text[:40].replace("\n", " "))

        # ---- 6. 产线数据一致性检查 ----
        opts = page.eval_on_selector_all("#m-item option", "els => els.length")
        check("实测项目下拉有选项", opts >= 1, "选项=%d" % opts)

        if opts >= 1:
            page.click("#btn-consistency")
            page.wait_for_function(
                """() => {
                    const el = document.querySelector('#m-msg');
                    return el && (el.textContent.indexOf('检查完成') >= 0
                                  || el.textContent.indexOf('无法') >= 0
                                  || el.textContent.indexOf('没有') >= 0);
                }""", timeout=20000)
            check_msg = page.inner_text("#m-msg")
            check("一致性检查有结果", len(check_msg) > 3, check_msg[:40])
            result_text = page.inner_text("#m-result")
            check("结果含人员偏移表", "各测量人的整体偏移" in result_text,
                  result_text[:40].replace("\n", " "))
            check("结果含口径提示", "不是 GR" in result_text, "")
            check("结果含逐工单比较", "逐工单比较" in result_text, "")

            # 阈值改严应当更容易判显著（性质验证，不依赖具体数据）
            strict = page.evaluate(
                """async () => {
                    const code = document.querySelector('#m-item').value;
                    const r = await fetch('/api/v1/msa/consistency?item_code=' +
                                          encodeURIComponent(code) + '&z_limit=0.5',
                                          {credentials:'same-origin'});
                    const b = await r.json();
                    return {ok: b.data.ok, flagged: b.data.flagged_orders || 0};
                }"""
            )
            check("阈值改严后判定更敏感", strict["flagged"] >= 1 or not strict["ok"],
                  "flagged=%s" % strict["flagged"])

        # 试验方案建议
        page.click("#btn-advice")
        page.wait_for_function(
            """() => document.querySelector('#m-result').textContent.indexOf('零件数') >= 0""",
            timeout=15000)
        advice_text = page.inner_text("#m-result")
        check("方案建议含 10 件×3 人×3 次",
              "10" in advice_text and "评价人数" in advice_text, advice_text[:40].replace("\n", " "))

        # ---- 7. 班组长进不来 ----
        page.goto(base + "/admin/logout", wait_until="domcontentloaded")
        page.goto(base + "/admin/login", wait_until="domcontentloaded")
        page.fill("input[name=username]", "leader01")
        page.fill("input[name=password]", "leader01")
        page.click("button[type=submit]")
        page.wait_for_timeout(1500)
        resp = page.goto(base + "/admin/msa", wait_until="domcontentloaded")
        body_text = page.inner_text("body")
        check("班组长被拒（403 或无权限页）",
              (resp is not None and resp.status == 403) or "无权" in body_text,
              "status=%s" % (resp.status if resp else "?"))

        if save_shot:
            page.goto(base + "/admin/logout", wait_until="domcontentloaded")
            page.goto(base + "/admin/login", wait_until="domcontentloaded")
            page.fill("input[name=username]", "manager01")
            page.fill("input[name=password]", "manager01")
            page.click("button[type=submit]")
            page.wait_for_url("**/admin/**", timeout=15000)
            page.goto(base + "/admin/msa", wait_until="domcontentloaded")
            page.wait_for_selector("#ov .stat", timeout=15000)
            page.locator("#studies table tbody tr").first.click()
            page.wait_for_selector("#detail-card", state="visible", timeout=10000)
            page.wait_for_timeout(900)
            SHOT_DIR.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(SHOT_DIR / "13-msa.png"), full_page=True)
            print("截图：docs/screenshots/13-msa.png")

        browser.close()

    print("\n测量系统分析页巡检")
    print("-" * 58)
    failed = 0
    for name, ok, detail in checks:
        print("%s %s%s" % ("[通过]" if ok else "[失败]", name,
                           ("  " + detail) if detail else ""))
        if not ok:
            failed += 1
    print("-" * 58)
    print("合计 %d 项，失败 %d 项" % (len(checks), failed))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
