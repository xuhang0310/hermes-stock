"""本地演示：持仓页买入（增加）/ 卖出（删除）完整流程，每步截图。

跑法：python scripts/demo_trade.py
前提：后端 :8000 + vite dev :5173 在跑。截图存 demo_shots/。
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5173"
API = "http://127.0.0.1:8000"
USER, PWD = "codex_test_admin", "codex-test-password"
SHOTS = Path(__file__).resolve().parent.parent / "demo_shots"
SHOTS.mkdir(exist_ok=True)


def login_token() -> str:
    body = json.dumps({"username": USER, "password": PWD}).encode()
    req = urllib.request.Request(f"{API}/api/auth/login", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())["access_token"]


def shot(page, name: str) -> None:
    p = SHOTS / f"{name}.png"
    page.screenshot(path=str(p), full_page=True)
    print(f"  截图 -> {p.name}")


def main() -> int:
    token = login_token()
    print("登录拿到 token")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()

        page.goto(f"{BASE}/", wait_until="domcontentloaded")
        page.evaluate(f"localStorage.setItem('hermes_stock_token', '{token}')")
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(800)

        # 进持仓页
        page.locator('.nav-list button', has_text="持仓").first.click()
        page.wait_for_timeout(1200)
        shot(page, "01_positions_initial")
        print("[1] 进入持仓页，初始状态")

        # 点买入按钮
        buy_btn = page.locator('button.wb-trade-btn.buy').first
        buy_btn.wait_for(state="visible", timeout=5000)
        buy_btn.click()
        page.wait_for_timeout(700)
        shot(page, "02_buy_modal_opened")
        print("[2] 点击'买入'按钮，弹窗打开")

        # 填表
        page.locator('.wb-modal input[type="number"]').first.fill("100")
        acct = page.locator('.wb-modal select').first
        opts = [o for o in acct.locator('option').all_inner_texts() if "请选择" not in o]
        acct.select_option(label=opts[0])
        page.wait_for_timeout(300)
        shot(page, "03_buy_form_filled")
        print(f"[3] 填数量 100 + 选账户 '{opts[0]}'")

        # 确认买入
        page.get_by_role("button", name="确认买入").click()
        page.wait_for_timeout(2000)
        shot(page, "04_after_buy")
        print("[4] 确认买入，持仓已增加")

        # 点卖出
        sell_btn = page.locator('button.wb-trade-btn.sell').first
        sell_btn.wait_for(state="visible", timeout=5000)
        sell_btn.click()
        page.wait_for_timeout(700)
        shot(page, "05_sell_modal_opened")
        print("[5] 点击'卖出'按钮，弹窗打开")

        # 填卖出数量（全部）
        page.locator('.wb-modal input[type="number"]').first.fill("999999")
        acct2 = page.locator('.wb-modal select').first
        opts2 = [o for o in acct2.locator('option').all_inner_texts() if "请选择" not in o]
        acct2.select_option(label=opts2[0])
        page.wait_for_timeout(300)
        shot(page, "06_sell_form_filled")
        print("[6] 填卖出数量（全部清仓）")

        page.get_by_role("button", name="确认卖出").click()
        page.wait_for_timeout(2000)
        shot(page, "07_after_sell")
        print("[7] 确认卖出，持仓已减少/删除")

        print(f"\n截图全部存到: {SHOTS}")
        browser.close()
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"[ERROR] {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(2)
