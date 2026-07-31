"""早盘舆情简报：采集→分析→生成报告。

被 rhythm_scheduler 的 ``topnews_brief`` 任务类型调用。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from backend.app.topnews.collectors import (
    CLSNewsCollector,
    CapitalFlowCollector,
    FuturesCollector,
    MacroNewsCollector,
    USStockCollector,
)
from backend.app.topnews.report.generator import ReportGenerator

logger = logging.getLogger("hermes.topnews")


def _run_async(coro):
    try:
        loop = asyncio.get_running_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, coro).result()
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


async def _collect_all():
    collectors = [
        USStockCollector(),
        FuturesCollector(),
        CLSNewsCollector(),
        MacroNewsCollector(),
        CapitalFlowCollector(),
    ]
    tasks = [c.collect() for c in collectors]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    all_data: dict[str, list[dict]] = {}
    for c, result in zip(collectors, results):
        if isinstance(result, Exception):
            logger.error("[%s] 采集异常: %s", c.category, result)
            all_data[c.category] = []
        else:
            all_data[c.category] = result.items
            logger.info("[%s] 采集完成, %d 条", c.category, len(result.items))
    for c in collectors:
        await c.close()
    return all_data


def run_topnews_brief(period: str = "pre_market") -> str:
    """完整执行：采集→分析→生成简报文本。"""
    all_data = _run_async(_collect_all())
    generator = ReportGenerator()
    report = generator.generate(all_data, period=period)
    return report
