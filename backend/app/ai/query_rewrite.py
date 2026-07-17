"""查询改写与多路扩展（RAG P2 底座）：把用户口语化 query 变成检索友好的多角度查询集。

借鉴 deepresearch 的 RewriteQueryTransformer + MultiQueryExpander：一次便宜 LLM 调用产出
``{rewritten, expansions}``，供联网搜索 / 知识库检索并行召回后合并，提高召回质量。

设计：
- 单一职责：只"产查询"，不搜索、不检索——调用方拿 QueryPlan 各自去召回。
- 低耦合：只依赖 llm_client + active_llm_config + settings，对外仅暴露 rewrite_and_expand 与 QueryPlan。
- 降级即身份（Null-Object 思想）：开关关闭 / LLM 失败 / 解析失败一律返回"原查询单条"，
  调用方零感知地回退到改造前的单查询行为，绝不因改写失败而阻断检索。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from backend.app.ai.llm_client import chat_completion
from backend.app.core.applog import get_logger
from backend.app.core.llm_config import active_llm_config
from backend.app.core.settings import settings

logger = get_logger("query_rewrite")

_MAX_QUERIES = 4  # 查询集硬上限（含改写句），防 LLM 吐一堆把下游搜索/嵌入放大过头

_PROMPT = """你是检索查询优化器。把用户的原始问题改写成更适合搜索引擎/向量库检索的查询，并扩展出几条角度不同的查询以提高召回。

规则：
- rewritten：把口语、指代（它/这个/现在）消解成独立、含实体的检索句。
- expansions：2-3 条与 rewritten 角度不同的查询（不同子主题/同义表述/相关维度），不要互相重复。
- 保留关键实体（股票名/代码/行业/指标）。
- 只输出 JSON，不要任何解释或 markdown 包裹。

原始问题：{query}{history}

输出 JSON：{{"rewritten": "改写后的检索句", "expansions": ["角度一", "角度二"]}}"""


@dataclass
class QueryPlan:
    """一次查询改写的结果。queries 是去重后的最终检索查询集（≥1 条，末条永远含原查询兜底）。"""

    original: str
    rewritten: str
    queries: list[str]

    @property
    def expanded(self) -> bool:
        """是否真正做了扩展（>1 条）；调用方可据此决定是否走多路合并路径。"""
        return len(self.queries) > 1

    @classmethod
    def identity(cls, query: str) -> "QueryPlan":
        """身份计划：不改写、单查询。降级路径与改造前行为完全一致。"""
        q = (query or "").strip()
        return cls(original=q, rewritten=q, queries=[q] if q else [])


def rewrite_and_expand(query: str, *, history: str | None = None, model_config_id: int | None = None) -> QueryPlan:
    """把原始 query 改写并扩展为多角度查询集；任何异常/关闭开关都降级为身份计划。"""
    q = (query or "").strip()
    if not q or not settings.query_rewrite_enabled:
        return QueryPlan.identity(q)
    try:
        llm_config = active_llm_config(model_config_id)
        prompt = _PROMPT.format(query=q, history=f"\n近期对话：{history.strip()}" if history else "")
        resp = chat_completion(
            [{"role": "user", "content": prompt}],
            llm_config,
            tools=None,
            tool_choice=None,
            temperature=0.3,
            timeout=20.0,
        )
        text = resp["choices"][0]["message"]["content"]
        data = _parse_json(text)
        rewritten = (str(data.get("rewritten") or "").strip()) or q
        expansions = [str(x).strip() for x in (data.get("expansions") or []) if str(x).strip()]
        queries = _dedup([rewritten, *expansions, q])[:_MAX_QUERIES]
        return QueryPlan(original=q, rewritten=rewritten, queries=queries or [q])
    except Exception as exc:  # noqa: BLE001 —— 改写是增强项，失败必须静默降级，不能阻断检索
        logger.warning("查询改写失败，降级为原查询: %s", str(exc)[:200])
        return QueryPlan.identity(q)


def _parse_json(text: str) -> dict:
    """容错解析：模型可能用 ```json 包裹或前后带解释，抽取第一个 {...} 块。"""
    snippet = (text or "").strip()
    match = re.search(r"\{.*\}", snippet, re.DOTALL)
    if not match:
        return {}
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _dedup(items: list[str]) -> list[str]:
    """按小写去重、保序、去空。"""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        value = item.strip()
        key = value.lower()
        if value and key not in seen:
            seen.add(key)
            out.append(value)
    return out
