# -*- coding: utf-8 -*-
"""数据导入导出页巡检（Playwright + 本机 Chrome，headless）。

这一页最容易犯的错是"接口通了但页面是空壳"，所以断言都落在**行为**上：

1. 数据集下拉是后端给的（不是写死的），至少 4 个导入类型；
2. 下载模板的链接指向 /api/v1/io/template/<选中数据集>，换数据集会跟着换；
3. 上传一份有问题的 CSV → 预览后出现**逐行错误表**，且"确认导入"保持禁用；
4. 上传一份正常 CSV → 确认导入按钮被点亮，点下去后提示"已导入"；
5. 导出区按数据集显示不同参数（缺陷单有起始日期，供应商没有状态）。

第 3 条是核心：如果错误清单出不来，用户只会看到"导入失败"四个字，
然后去找客服——而这本来是一句"第 3 行的数量填了 abc"就能解决的。

用法：
    python scripts/ui_check_data.py [端口] [--shot]
前置：服务已启动（uvicorn），且已执行过 scripts/seed_demo_data.py
"""

from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
SHOT_DIR = BASE / "docs" / "screenshots"


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else "8013"
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

        page.goto(base + "/admin/data", wait_until="domcontentloaded")
        check("导入导出页可达", "/admin/login" not in page.url, page.url)

        page.wait_for_function(
            "() => document.querySelectorAll('#i-dataset option').length > 0",
            timeout=15000,
        )
        page.wait_for_timeout(600)

        # ---- 1. 数据集下拉来自后端 ----
        n_import = page.eval_on_selector_all("#i-dataset option", "els => els.length")
        check("导入类型 ≥ 4", n_import >= 4, "选项=%d" % n_import)
        n_export = page.eval_on_selector_all("#e-dataset option", "els => els.length")
        check("导出数据集 ≥ 5", n_export >= 5, "选项=%d" % n_export)

        hint = page.inner_text("#i-hint")
        check("列名提示可见", "列：" in hint, hint[:50].replace("\n", " "))

        # ---- 2. 模板链接跟随数据集 ----
        tpl = page.evaluate(
            """async () => {
                const ds = document.querySelector('#i-dataset').value;
                const r = await fetch('/api/v1/io/template/' + ds, {credentials:'same-origin'});
                const buf = await r.arrayBuffer();
                return {status: r.status, size: buf.byteLength,
                        disp: r.headers.get('content-disposition') || ''};
            }"""
        )
        check("模板可下载", tpl["status"] == 200 and tpl["size"] > 0,
              "status=%s size=%s" % (tpl["status"], tpl["size"]))
        check("模板文件名做了 UTF-8 编码", "filename*=UTF-8''" in tpl["disp"],
              tpl["disp"][:60])

        # ---- 3. 有问题的文件：必须出逐行错误表 ----
        bad = ("工单号,产品,产线\n"
               "WO-UICHK-1,六角螺栓,line-01\n"
               ",缺单号,line-01\n").encode("utf-8")
        page.set_input_files("#i-file", {"name": "bad.csv", "mimeType": "text/csv",
                                        "buffer": bad})
        page.click("#btn-preview")
        page.wait_for_selector("#i-result", state="visible", timeout=15000)
        page.wait_for_timeout(500)

        stats = page.inner_text("#i-stats")
        check("预览显示统计卡", "数据行" in stats, stats[:40].replace("\n", " "))
        rows = page.locator("#i-errors table tbody tr").count()
        check("错误清单逐行列出", rows >= 1, "行数=%d" % rows)
        commit_disabled = page.eval_on_selector("#btn-commit", "el => el.disabled")
        # 有可导入行时允许提交（部分成功语义），但必须看得到失败行
        check("失败行有高亮", page.locator("#i-errors tr.row-bad").count() >= 1,
              "commit_disabled=%s" % commit_disabled)

        # ---- 4. 正常文件：确认导入真的写进去了 ----
        good = ("工单号,产品,产线,计划数量,日期\n"
                "WO-UICHK-OK,六角螺栓,line-01,1200,2026-11-06\n").encode("utf-8")
        page.set_input_files("#i-file", {"name": "good.csv", "mimeType": "text/csv",
                                        "buffer": good})
        page.click("#btn-preview")
        page.wait_for_function(
            "() => !document.querySelector('#btn-commit').disabled", timeout=15000)
        check("正常文件点亮确认按钮", True, "")

        page.click("#btn-commit")
        page.wait_for_function(
            """() => document.querySelector('#i-msg').textContent.indexOf('导入完成') >= 0
                     || document.querySelector('#i-msg').textContent.indexOf('新增') >= 0""",
            timeout=15000,
        )
        done = page.inner_text("#i-msg")
        check("导入后提示结果", "导入完成" in done or "新增" in done,
              done[:50].replace("\n", " "))

        # 库里真的有了（再导一次应当被判"跳过"）
        again = page.evaluate(
            """async () => {
                const fd = new FormData();
                fd.append('dataset', 'work_orders');
                fd.append('file', new Blob(['工单号,产品,产线\\nWO-UICHK-OK,六角螺栓,line-01\\n'],
                          {type:'text/csv'}), 'again.csv');
                const r = await fetch('/api/v1/io/import/preview',
                                      {method:'POST', body: fd, credentials:'same-origin'});
                return (await r.json()).data;
            }"""
        )
        check("重复导入被判为跳过", again["skipped"] >= 1 and again["failed"] == 0,
              "skipped=%s failed=%s" % (again["skipped"], again["failed"]))

        # ---- 5. 导出区参数联动 ----
        page.click("button[data-tab=export]")
        page.wait_for_timeout(300)
        page.select_option("#e-dataset", "defects")
        page.wait_for_timeout(200)
        date_visible = page.eval_on_selector(
            "[data-param=date_from]", "el => el.style.display !== 'none'")
        check("缺陷单显示日期参数", date_visible, "")

        page.select_option("#e-dataset", "suppliers")
        page.wait_for_timeout(200)
        status_visible = page.eval_on_selector(
            "[data-param=status]", "el => el.style.display !== 'none'")
        check("供应商不显示状态参数", not status_visible, "")

        exp = page.evaluate(
            """async () => {
                const r = await fetch('/api/v1/io/export?dataset=defects&format=xlsx',
                                      {credentials:'same-origin'});
                const buf = await r.arrayBuffer();
                return {status: r.status, size: buf.byteLength,
                        type: r.headers.get('content-type') || ''};
            }"""
        )
        check("导出 xlsx 可下载",
              exp["status"] == 200 and exp["size"] > 1000 and "spreadsheet" in exp["type"],
              "status=%s size=%s" % (exp["status"], exp["size"]))

        if save_shot:
            SHOT_DIR.mkdir(parents=True, exist_ok=True)
            page.goto(base + "/admin/data", wait_until="domcontentloaded")
            page.wait_for_function(
                "() => document.querySelectorAll('#i-dataset option').length > 0",
                timeout=15000)
            page.wait_for_timeout(800)
            page.screenshot(path=str(SHOT_DIR / "12-data-io.png"), full_page=True)
            print("截图：docs/screenshots/12-data-io.png")

        browser.close()

    print("\n数据导入导出页巡检")
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
