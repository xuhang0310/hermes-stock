"""E2E 验证持仓页买入/卖出按钮交互（playwright 驱动真实 chromium）。

跑法：python scripts/test_trade_e2e.py
前提：后端 :8000 + vite dev :5173 都在跑。
"""
from __future__ import annotations

import json
import sys
import urllib.request

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5173"
API = "http://127.0.0.1:8000"
USER, PWD = "codex_test_admin", "codex-test-password"


def login_token() -> str:
    body = json.dumps({"username": USER, "password": PWD}).encode()
    req = urllib.request.Request(f"{API}/api/auth/login", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())["access_token"]


def main() -> int:
    token = login_token()
    print("拿到 token")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

        logs: list[str] = []
        page.on("console", lambda msg: logs.append(f"[console.{msg.type}] {msg.text}"))
        page.on("pageerror", lambda exc: logs.append(f"[pageerror] {exc}"))
        page.on("requestfailed", lambda req: logs.append(f"[reqfail] {req.url} {req.failure}"))

        # 注入 token 后进站点
        page.goto(f"{BASE}/", wait_until="domcontentloaded")
        page.evaluate(f"localStorage.setItem('hermes_stock_token', '{token}')")
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(800)
        print("已登录进站")

        # 进持仓页：点侧边栏"持仓"
        nav_btns = page.locator('.nav-list button').all_inner_texts()
        print(f"导航项: {nav_btns}")
        page.locator('.nav-list button', has_text="持仓").first.click()
        page.wait_for_timeout(1000)
        print(f"当前 URL: {page.url}")

        # 确认有持仓行 + 买入按钮
        buy_btn = page.locator('button.wb-trade-btn.buy').first
        if buy_btn.count() == 0:
            print("持仓列表为空，没有买入按钮可测。先在后端造一条持仓。")
            # 造一条
            body = json.dumps({"symbol": "000725.SZ", "name": "京东方A",
                               "position_quantity": 100, "cost_price": 4.5,
                               "action": "buy"}).encode()
            req = urllib.request.Request(f"{API}/api/positions", data=body,
                                         method="POST",
                                         headers={"Content-Type": "application/json",
                                                  "Authorization": f"Bearer {token}"})
            urllib.request.urlopen(req, timeout=10)
            page.reload(wait_until="networkidle")
            page.wait_for_timeout(800)
            buy_btn = page.locator('button.wb-trade-btn.buy').first

        buy_btn.wait_for(state="visible", timeout=5000)

        # 关键断言 1：点买入按钮不跳页
        url_before = page.url
        buy_btn.click()
        page.wait_for_timeout(700)
        url_after = page.url
        assert url_before == url_after, f"点买入按钮跳页了！前:{url_before} 后:{url_after}"
        print("[1] 点买入按钮不跳页[OK]")

        # 关键断言 2：弹窗打开
        modal_title = page.locator('.wb-modal h3').first
        modal_title.wait_for(state="visible", timeout=3000)
        title_text = modal_title.inner_text()
        assert "买入" in title_text, f"弹窗标题不是买入: {title_text}"
        print(f"[2] 买入弹窗已打开: {title_text}[OK]")

        # 填数量 + 选账户
        page.locator('.wb-modal input[type="number"]').first.fill("100")
        acct_select = page.locator('.wb-modal select').first
        options = acct_select.locator('option').all_inner_texts()
        real_opts = [o for o in options if "请选择" not in o]
        assert real_opts, "没有可选的证券账户"
        acct_select.select_option(label=real_opts[0])
        print(f"[3] 填数量 100 + 选账户 '{real_opts[0]}'[OK]")

        # 拦截 API 调用确认前端发的 payload
        api_payload: dict = {}
        def on_request(req):
            if req.url.endswith('/api/positions') and req.method == "POST":
                api_payload.update(json.loads(req.post_data or "{}"))
        page.on("request", on_request)

        page.get_by_role("button", name="确认买入").click()
        page.wait_for_timeout(2000)

        assert api_payload, "确认买入后前端没发 POST /api/positions"
        assert api_payload.get("action") == "buy", f"action 不对: {api_payload}"
        assert api_payload.get("position_quantity") == 100, f"qty 不对: {api_payload}"
        assert api_payload.get("symbol"), "没带 symbol"
        print(f"[4] 前端发的 payload 正确: {api_payload}[OK]")

        # 弹窗关闭
        modal_gone = page.locator('.wb-modal h3').count() == 0
        assert modal_gone, "确认买入后弹窗未关闭"
        print("[5] 买入成功，弹窗已关闭[OK]")

        # 测卖出
        sell_btn = page.locator('button.wb-trade-btn.sell').first
        sell_btn.wait_for(state="visible", timeout=5000)
        sell_btn.click()
        page.wait_for_timeout(700)
        t2 = page.locator('.wb-modal h3').first
        t2.wait_for(state="visible", timeout=3000)
        assert "卖出" in t2.inner_text()
        print(f"[6] 卖出弹窗打开: {t2.inner_text()}[OK]")

        page.locator('.wb-modal input[type="number"]').first.fill("50")
        acct2 = page.locator('.wb-modal select').first
        opts2 = [o for o in acct2.locator('option').all_inner_texts() if "请选择" not in o]
        acct2.select_option(label=opts2[0])
        page.get_by_role("button", name="确认卖出").click()
        page.wait_for_timeout(2000)
        print("[7] 卖出提交完成[OK]")

        print("\n========== 全部 E2E 验证通过 ==========")
        if logs:
            print("\n--- 最近的 console 日志 ---")
            for l in logs[-15:]:
                print(l)
        browser.close()
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"\n[FAIL] {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n[ERROR] {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(2)
