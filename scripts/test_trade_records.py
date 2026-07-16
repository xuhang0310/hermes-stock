"""验证交易流水表：买→1条buy流水，卖→1条sell流水，查询，删除。后端 :8000 在跑。"""
import json, urllib.request

API = "http://127.0.0.1:8000"
body = json.dumps({"username": "codex_test_admin", "password": "codex-test-password"}).encode()
req = urllib.request.Request(f"{API}/api/auth/login", data=body, headers={"Content-Type": "application/json"})
token = json.loads(urllib.request.urlopen(req, timeout=10).read())["access_token"]
h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def post(p, d):
    r = urllib.request.Request(f"{API}{p}", data=json.dumps(d).encode(), method="POST", headers=h)
    with urllib.request.urlopen(r, timeout=10) as x:
        return json.loads(x.read())


def get(p):
    r = urllib.request.Request(f"{API}{p}", headers=h)
    with urllib.request.urlopen(r, timeout=10) as x:
        return json.loads(x.read())


def delete(p):
    r = urllib.request.Request(f"{API}{p}", method="DELETE", headers=h)
    with urllib.request.urlopen(r, timeout=10) as x:
        return json.loads(x.read())


ACC = 3
SYM = "600519.SH"

# 1. 买入 100@1600 → 1 条 buy 流水
post("/api/positions", {"symbol": SYM, "position_quantity": 100, "cost_price": 1600, "action": "buy", "account_id": ACC})
print("买入 100@1600")
trades = get(f"/api/positions/trades?account_id={ACC}&limit=5")
t = trades[0]
print(f"  流水最新: action={t['action']} qty={t['quantity']} price={t['price']} amount={t['amount']} symbol={t['symbol']}")
assert t["action"] == "buy" and t["quantity"] == 100 and t["price"] == 1600 and t["amount"] == 160000, "buy 流水字段不符"

# 2. 卖出 100 → 1 条 sell 流水
post("/api/positions", {"symbol": SYM, "position_quantity": 100, "action": "sell", "account_id": ACC})
print("卖出 100")
trades = get(f"/api/positions/trades?account_id={ACC}&limit=5")
print(f"  流水最新2条: {[(x['action'], x['quantity']) for x in trades[:2]]}")
assert trades[0]["action"] == "sell" and trades[0]["quantity"] == 100, "sell 流水不符"

# 3. 删最新 sell 流水（不回滚持仓）
sell_id = trades[0]["id"]
r = delete(f"/api/positions/trades/{sell_id}")
print(f"删除流水 {sell_id}: {r}")
trades = get(f"/api/positions/trades?account_id={ACC}&limit=5")
print(f"删除后流水: {len(trades)} 条, 最新 action={trades[0]['action'] if trades else 'none'}")
assert all(x["id"] != sell_id for x in trades), "删除后流水仍含已删 id"

print("\n交易流水表验证通过 ✓")
