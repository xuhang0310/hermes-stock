"""E2E 验证持仓页"总买入"入口（add 模式：symbol/数量/价格必填）。

跑法：python scripts/test_buy_add_e2e.py
前提：后端 :8000 + vite dev :5173 在跑。
"""
from __future__ import annotations

import json
import sys
import urllib.request

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5173"
API = "http://127.0.0.1:8000"
USER, PWD = "codex_test_admin", "codex-test-password"
SYM = "600519.SH"


def http(method, path, token, body=None):
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(f"{API}{path}", data=data, method=method,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def main() -> int:
    token = http("POST", "/api/auth/login", "", {"username": USER, "password": PWD})["access_token"]
    # 清掉既有
    try: http("POST", "/api/positions", token, {"symbol": SYM, "action": "close"})
    except Exception: pass
    print("拿到 token，已清理既有持仓")

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_context(viewport={"width": 1440, "height": 900}).new_page()
        pg.goto(f"{BASE}/", wait_until="domcontentloaded")
        pg.evaluate(f"localStorage.setItem('hermes_stock_token','{token}')")
        pg.reload(wait_until="networkidle")
        pg.wait_for_timeout(800)
        pg.locator('.nav-list button', has_text="持仓").first.click()
        pg.wait_for_timeout(1000)

        # 点 header "买入"按钮（add 模式入口，区别于行内 buy）
        pg.get_by_role("button", name="买入", exact=True).first.click()
        pg.wait_for_timeout(600)
        title = pg.locator('.wb-modal h3').first
        title.wait_for(state="visible", timeout=3000)
        print(f"[1] 总买入弹窗打开: {title.inner_text()}")

        # 验证 add 模式有 symbol input
        sym_input = pg.locator('.wb-modal input').nth(0)
        assert sym_input.is_visible(), "add 模式没有股票代码输入框"
        print("[2] 股票代码输入框存在（add 模式）")

        # 填表
        sym_input.fill(SYM)
        pg.locator('.wb-modal input').nth(1).fill("贵州茅台")  # 名称
        # 账户 select
        acct = pg.locator('.wb-modal select').first
        opts = [o for o in acct.locator('option').all_inner_texts() if "请选择" not in o]
        acct.select_option(label=opts[0])
        # 数量
        pg.locator('.wb-modal input[type="number"]').nth(0).fill("100")
        # 价格
        pg.locator('.wb-modal input[type="number"]').nth(1).fill("1600")
        print("[3] 填完 账户/代码/名称/100股/1600元")

        # 拦截 payload
        payload = {}
        def on_req(req):
            if req.url.endswith('/api/positions') and req.method == "POST":
                payload.update(json.loads(req.post_data or "{}"))
        pg.on("request", on_req)

        pg.get_by_role("button", name="确认买入").click()
        pg.wait_for_timeout(2000)
        assert payload, "确认买入没发请求"
        assert payload.get("action") == "buy"
        assert payload.get("symbol") == SYM, f"symbol 不对: {payload}"
        assert payload.get("position_quantity") == 100
        assert payload.get("cost_price") == 1600, f"cost_price 不对: {payload}"
        print(f"[4] payload 正确: {payload}")

        # 验证不闪
        assert pg.locator('.loading-panel').count() == 0, "出现 loading 遮罩（闪烁）"
        print("[5] 无闪烁")

        # 验证持仓列表有该标的
        pg.wait_for_timeout(500)
        rows = pg.locator('tbody tr', has_text=SYM).count()
        print(f"[6] 持仓列表 {SYM} 行数: {rows}（期望 >=1）")
        assert rows >= 1

        # 校验缺字段必填
        pg.get_by_role("button", name="买入", exact=True).first.click()
        pg.wait_for_timeout(500)
        # 不填直接确认 → 应报错
        pg.get_by_role("button", name="确认买入").click()
        pg.wait_for_timeout(500)
        msg = pg.locator('.form-message').first.inner_text()
        print(f"[7] 空提交校验提示: {msg}")
        assert "代码" in msg or "账户" in msg or "数量" in msg or "价格" in msg

        print("\n========== 总买入入口 E2E 通过 ==========")
        b.close()
        return 0


if __name__ == "__main__":
    try: sys.exit(main())
    except AssertionError as e:
        print(f"\n[FAIL] {e}"); sys.exit(1)
    except Exception as e:
        print(f"\n[ERROR] {type(e).__name__}: {e}"); import traceback; traceback.print_exc(); sys.exit(2)
