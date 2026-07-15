"""E2E 验证持仓页物理删除按钮。

跑法：python scripts/test_delete_e2e.py
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


def http(method: str, path: str, token: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        f"{API}{path}", data=data, method=method,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"} if token else {"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"_status": e.code, "_body": e.read().decode()}


def main() -> int:
    token = http("POST", "/api/auth/login", "", {"username": USER, "password": PWD})["access_token"]
    print("拿到 token")

    # 先造一条测试持仓用于删除
    sym = "000002.SZ"
    http("POST", "/api/positions", token, {"symbol": sym, "name": "万科A",
        "position_quantity": 100, "cost_price": 9.0, "action": "buy"})
    print(f"造测试持仓 {sym}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()

        logs: list[str] = []
        page.on("console", lambda m: logs.append(f"[console.{m.type}] {m.text}"))
        page.on("pageerror", lambda e: logs.append(f"[pageerror] {e}"))

        # 自动接受 confirm 对话框
        confirm_texts: list[str] = []
        page.on("dialog", lambda d: (confirm_texts.append(d.message), d.accept()))

        page.goto(f"{BASE}/", wait_until="domcontentloaded")
        page.evaluate(f"localStorage.setItem('hermes_stock_token', '{token}')")
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(800)

        page.locator('.nav-list button', has_text="持仓").first.click()
        page.wait_for_timeout(1200)

        # 确认有删除按钮
        del_btns = page.locator('button.wb-trade-btn.delete')
        del_btns.first.wait_for(state="visible", timeout=5000)
        count_before = del_btns.count()
        print(f"删除按钮数: {count_before}")
        assert count_before > 0, "没有删除按钮"

        # 找到测试标的所在行的删除按钮
        # 定位含 000002.SZ 的行，点该行的删除按钮
        target_row = page.locator('tbody tr', has_text="000002.SZ").first
        target_row.scroll_into_view_if_needed()
        del_btn = target_row.locator('button.wb-trade-btn.delete')
        print(f"目标行删除按钮存在: {del_btn.count() > 0}")

        # 拦截 DELETE 请求
        delete_sent: list[dict] = []
        def on_request(req):
            if "/api/positions/" in req.url and req.method == "DELETE":
                delete_sent.append({"url": req.url, "method": req.method})
        page.on("request", on_request)

        del_btn.click()
        # 立刻检查：不应出现全页 loading 遮罩（"正在加载 A股研究台..."）
        page.wait_for_timeout(200)
        loading_panel = page.locator('.loading-panel').count()
        assert loading_panel == 0, f"删除后出现 loading 遮罩（闪烁），count={loading_panel}"
        print("[不闪烁] 删除后无 loading 遮罩")

        page.wait_for_timeout(2000)

        # 验证 confirm 弹了
        assert confirm_texts, "删除时没弹 confirm 对话框"
        print(f"confirm 文案: {confirm_texts[0]}")

        # 验证 DELETE 请求发出
        assert delete_sent, "点删除后前端没发 DELETE 请求"
        print(f"DELETE 请求: {delete_sent[0]}")

        # 验证该行从列表消失
        gone = page.locator('tbody tr', has_text="000002.SZ").count()
        print(f"删除后列表中 000002.SZ 行数: {gone}（期望 0）")
        assert gone == 0, f"删除后 000002.SZ 仍在列表（{gone} 行）"

        # API 确认持仓已删
        pos_list = http("GET", "/api/positions", token)
        still = [x for x in pos_list if x.get("symbol") == sym]
        assert not still, f"API 仍返回 {sym} 的持仓"
        print("API 确认持仓已物理删除")

        print("\n========== 删除 E2E 验证通过 ==========")
        if logs:
            print("\n--- console 日志 ---")
            for l in logs[-10:]:
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
