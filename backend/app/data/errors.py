from __future__ import annotations


class PositionNotFoundError(Exception):
    """业务记录不存在（持仓/账户），路由层映射为 404。

    直接继承 Exception（而非 ValueError），使 404/400 的映射由类型结构保证，
    不依赖调用方 except 子句的书写顺序。DAO 层直接抛它，service 原样透传。
    """