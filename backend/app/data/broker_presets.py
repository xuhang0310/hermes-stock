from __future__ import annotations

"""A 股常用券商预置清单，供前端账户新建时的券商选单使用。

- code: 稳定标识（图标/去重/幂等 seed 用），name: 中文全称，short: 简称。
- 末项 manual 作为「其他券商」兜底，允许用户自填 broker_name。
"""

BROKER_PRESETS: list[dict[str, str]] = [
    {"code": "fangzheng", "name": "方正证券", "short": "方正"},
    {"code": "gf", "name": "广发证券", "short": "广发"},
    {"code": "citic", "name": "中信证券", "short": "中信"},
    {"code": "huatai", "name": "华泰证券", "short": "华泰"},
    {"code": "gtja", "name": "国泰君安", "short": "国君"},
    {"code": "csc", "name": "中信建投", "short": "建投"},
    {"code": "cmschina", "name": "招商证券", "short": "招商"},
    {"code": "galaxy", "name": "银河证券", "short": "银河"},
    {"code": "eastmoney", "name": "东方财富证券", "short": "东财"},
    {"code": "pingan", "name": "平安证券", "short": "平安"},
    {"code": "guosen", "name": "国信证券", "short": "国信"},
    {"code": "haitong", "name": "海通证券", "short": "海通"},
    {"code": "swhy", "name": "申万宏源", "short": "申万"},
    {"code": "cicc", "name": "中金公司", "short": "中金"},
    {"code": "xingye", "name": "兴业证券", "short": "兴业"},
    {"code": "orient", "name": "东方证券", "short": "东方"},
    {"code": "everbright", "name": "光大证券", "short": "光大"},
    {"code": "zhongtai", "name": "中泰证券", "short": "中泰"},
    {"code": "gxsc", "name": "国金证券", "short": "国金"},
    {"code": "zheshang", "name": "浙商证券", "short": "浙商"},
    {"code": "manual", "name": "其他券商", "short": "其他"},
]

# 当前在用、需要幂等预置账户的券商 code
DEFAULT_BROKER_CODES = ["fangzheng", "gf", "citic"]

_BY_CODE = {item["code"]: item for item in BROKER_PRESETS}
_BY_NAME = {item["name"]: item for item in BROKER_PRESETS}


def broker_name_for(code: str | None) -> str | None:
    item = _BY_CODE.get(code or "")
    return item["name"] if item else None


def broker_code_for_name(name: str | None) -> str:
    """按中文券商名反查 code，匹配不上归到 manual。"""
    item = _BY_NAME.get((name or "").strip())
    return item["code"] if item else "manual"
