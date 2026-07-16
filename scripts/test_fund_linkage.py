"""验证资金模型反转 + 当日盈亏联动。后端 :8000 在跑。"""
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

def put(p, d):
    r = urllib.request.Request(f"{API}{p}", data=json.dumps(d).encode(), method="PUT", headers=h)
    with urllib.request.urlopen(r, timeout=10) as x:
        return json.loads(x.read())

# 1. 维护账户3可用现金=1000000
put("/api/positions/accounts/3/funds", {"available_cash": 1000000})
print("维护可用现金 100万")
a = [x for x in get("/api/positions/accounts") if x["id"] == 3][0]
print(f"维护后: avail={a['available_cash']} hmv={a['holding_market_value']} total={a['total_asset']} pnl={a['total_pnl']} day={a['day_pnl']}")

# 2. 买入 100@1600 茅台
post("/api/positions", {"symbol": "600519.SH", "position_quantity": 100, "cost_price": 1600, "action": "buy", "account_id": 3})
print("买入 100@1600")
a = [x for x in get("/api/positions/accounts") if x["id"] == 3][0]
print(f"买入后: avail={a['available_cash']} hmv={a['holding_market_value']} total={a['total_asset']} pnl={a['total_pnl']} day={a['day_pnl']}")
print("期望: avail=1000000(不变), hmv=121099, total=1000000+121099=1121099, day=(今收-昨收)*100")

# 3. 卖出清仓，看 total_asset 回落
post("/api/positions", {"symbol": "600519.SH", "position_quantity": 100, "action": "sell", "account_id": 3})
print("卖出清仓")
a = [x for x in get("/api/positions/accounts") if x["id"] == 3][0]
print(f"卖出后: avail={a['available_cash']} hmv={a['holding_market_value']} total={a['total_asset']} pnl={a['total_pnl']} day={a['day_pnl']}")
print("期望: avail=1000000, hmv=0, total=1000000, pnl=0, day=0")

# 4. summary 全部账户视图 None 处理
print()
s = get("/api/positions/summary")
print(f"summary: total_asset={s['total_asset']} avail={s['available_cash']} (账户4/5没维护应为 None 才对？)")
