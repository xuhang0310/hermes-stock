"""验证查询改写模块：解析容错 / 去重 / 身份降级 / 开关。免 DB、免真实 LLM（打桩），本地直跑。

    python scripts/test_query_rewrite.py
"""
import os
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

os.environ["STOCK_QUERY_REWRITE_ENABLED"] = "true"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.ai import query_rewrite as qr  # noqa: E402
from backend.app.ai.query_rewrite import QueryPlan, _dedup, _parse_json, rewrite_and_expand  # noqa: E402

ok = 0


def check(label: str, cond: bool) -> None:
    global ok
    assert cond, f"✗ {label}"
    ok += 1
    print(f"  ✓ {label}")


print("_parse_json 容错：")
check("纯 JSON", _parse_json('{"rewritten": "a", "expansions": ["b"]}')["rewritten"] == "a")
check("```json 包裹", _parse_json('```json\n{"rewritten": "x"}\n```')["rewritten"] == "x")
check("前后带解释", _parse_json('好的，结果如下：{"rewritten": "y"} 完毕')["rewritten"] == "y")
check("非法 JSON → 空 dict", _parse_json("not json at all") == {})
check("数组而非对象 → 空 dict", _parse_json("[1,2,3]") == {})

print("_dedup：")
check("小写去重保序去空", _dedup(["茅台", "茅台", "  ", "MAOTAI", "maotai", "五粮液"]) == ["茅台", "MAOTAI", "五粮液"])

print("QueryPlan.identity：")
p = QueryPlan.identity("  贵州茅台  ")
check("身份计划=单查询、原句", p.queries == ["贵州茅台"] and p.rewritten == "贵州茅台")
check("身份计划 expanded=False", p.expanded is False)
check("空查询→空 queries", QueryPlan.identity("   ").queries == [])

print("rewrite_and_expand 降级路径（不打真实 LLM）：")
# 开关关闭 → 身份
os.environ_backup = qr.settings.query_rewrite_enabled
import backend.app.core.settings as S  # noqa: E402
object.__setattr__(S.settings, "query_rewrite_enabled", False)
plan_off = rewrite_and_expand("茅台还能拿吗")
check("开关关闭→身份计划", plan_off.queries == ["茅台还能拿吗"] and not plan_off.expanded)
object.__setattr__(S.settings, "query_rewrite_enabled", True)
check("空查询→身份", rewrite_and_expand("").queries == [])

# LLM 异常 → 降级（打桩 chat_completion 抛错）
orig = qr.chat_completion
qr.chat_completion = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
try:
    plan_err = rewrite_and_expand("宁德时代基本面如何")
    check("LLM 失败→降级为原查询单条", plan_err.queries == ["宁德时代基本面如何"] and not plan_err.expanded)
finally:
    qr.chat_completion = orig

# LLM 正常返回多角度 → 扩展（打桩返回构造 JSON）
qr.chat_completion = lambda *a, **k: {
    "choices": [{"message": {"content": '{"rewritten": "贵州茅台 2024 估值与基本面", "expansions": ["贵州茅台 白酒行业景气度", "贵州茅台 股价 支撑压力位"]}'}}]
}
try:
    plan_ok = rewrite_and_expand("茅台还能拿吗")
    check("正常→改写句生效", plan_ok.rewritten == "贵州茅台 2024 估值与基本面")
    check("正常→扩展为多查询且含原句兜底", plan_ok.expanded and "茅台还能拿吗" in plan_ok.queries)
    check("查询集去重且≤上限", len(plan_ok.queries) == len(set(q.lower() for q in plan_ok.queries)) <= qr._MAX_QUERIES)
finally:
    qr.chat_completion = orig

print(f"\n查询改写验证通过 ✓（{ok} 项断言）")
