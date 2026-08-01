"""完整版多因子组合信号引擎 - 从 Vibe-Trading 完整移植

核心功能：
  1. 从因子注册表加载多个因子
  2. 横截面 Z-score 标准化
  3. 加权组合（等权或自定义权重）
  4. TopN 选股（做多前N只，做空后N只）
  5. 容错机制（因子失败自动跳过，权重重新分配）

设计哲学：
  - 因子和策略完全解耦
  - 因子即插即用
  - 策略通过配置组合因子
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def _zscore_cross_section(df: pd.DataFrame) -> pd.DataFrame:
    """横截面 Z-score 标准化（每天独立标准化）

    Args:
        df: 宽表数据 (index=date, columns=instruments)

    Returns:
        标准化后的宽表，形状不变
        每天均值为0，标准差为1
        标准差为0的行变成NaN（避免除零）
    """
    mean = df.mean(axis=1, skipna=True)
    std = df.std(axis=1, ddof=1, skipna=True)
    std = std.where(std > 1e-12)  # 标准差太小 -> NaN
    return df.sub(mean, axis=0).div(std, axis=0)


@dataclass(frozen=True)
class FactorSignalEngine:
    """多因子组合信号引擎（完整版）

    Attributes:
        factor_ids: 因子ID元组，如 ("alpha101_001", "alpha101_012")
        weights: 因子权重元组，None 表示等权
        standardize: 是否横截面标准化（推荐True）
        top_n: 做多前N只股票（None表示不做TopN选股）
        bottom_n: 做空后N只股票（None表示不做空）
        min_factors: 最少需要几个因子成功（默认1）
    """

    factor_ids: tuple[str, ...]
    weights: Optional[tuple[float, ...]] = None
    standardize: bool = True
    top_n: Optional[int] = None
    bottom_n: Optional[int] = None
    min_factors: int = 1

    # 可选：注入自定义注册表（主要用于测试）
    _registry: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        """验证参数"""
        if not isinstance(self.factor_ids, tuple):
            object.__setattr__(self, "factor_ids", tuple(self.factor_ids))
        if len(self.factor_ids) == 0:
            raise ValueError("factor_ids 不能为空")
        if self.weights is not None:
            weights = tuple(self.weights)
            if len(weights) != len(self.factor_ids):
                raise ValueError(
                    f"weights 长度 {len(weights)} != factor_ids 长度 {len(self.factor_ids)}"
                )
            object.__setattr__(self, "weights", weights)
        if self.top_n is not None and self.top_n <= 0:
            raise ValueError(f"top_n 必须是正整数，当前: {self.top_n}")
        if self.bottom_n is not None and self.bottom_n <= 0:
            raise ValueError(f"bottom_n 必须是正整数，当前: {self.bottom_n}")
        if self.min_factors < 1:
            raise ValueError(f"min_factors 必须 >= 1，当前: {self.min_factors}")

    @classmethod
    def from_factors(
        cls,
        factor_ids,
        weights=None,
        *,
        standardize: bool = True,
        top_n: Optional[int] = None,
        bottom_n: Optional[int] = None,
        min_factors: int = 1,
        registry: Any = None,
    ) -> "FactorSignalEngine":
        """创建多因子组合引擎

        Args:
            factor_ids: 因子ID列表
            weights: 因子权重（可选，默认等权）
            standardize: 是否横截面标准化
            top_n: 做多前N只
            bottom_n: 做空后N只
            min_factors: 最少需要几个因子成功
            registry: 自定义因子注册表（可选）

        Returns:
            FactorSignalEngine 实例

        Examples:
            # 等权多因子策略
            engine = FactorSignalEngine.from_factors(
                ["alpha101_001", "alpha101_012", "qlib158_roc20"],
                top_n=20,
            )

            # 自定义权重的多空策略
            engine = FactorSignalEngine.from_factors(
                ["alpha101_001", "alpha101_012"],
                weights=[0.6, 0.4],
                top_n=10,
                bottom_n=10,
            )
        """
        return cls(
            factor_ids=tuple(factor_ids),
            weights=tuple(weights) if weights is not None else None,
            standardize=standardize,
            top_n=top_n,
            bottom_n=bottom_n,
            min_factors=min_factors,
            _registry=registry,
        )

    def compute_signal(self, panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
        """计算组合因子信号

        Args:
            panel: 宽表数据字典
                - panel["close"]: DataFrame (date × instruments)
                - panel["open"], panel["high"], ... 可选

        Returns:
            信号宽表 (date × instruments)
            - 如果设置了 top_n/bottom_n: 值为 {-1.0, 0.0, +1.0}
            - 否则: 原始组合分数（可能超出[-1, 1]）
        """
        ref = panel.get("close")
        if ref is None:
            raise ValueError("panel 必须包含 'close' DataFrame")

        # 延迟导入，避免循环依赖
        from backend.app.quant.factors.registry import Registry

        registry = self._registry if self._registry is not None else Registry()

        # Step 1: 计算每个因子
        per_factor: list[pd.DataFrame] = []
        per_factor_weights: list[float] = []

        for idx, factor_id in enumerate(self.factor_ids):
            w = float(self.weights[idx]) if self.weights is not None else 1.0

            try:
                # 计算因子值
                raw = registry.compute(factor_id, panel)

                # 横截面标准化
                if self.standardize:
                    raw = _zscore_cross_section(raw)

                per_factor.append(raw)
                per_factor_weights.append(w)

            except Exception as exc:
                # 因子失败，记录警告，继续下一个
                logger.warning(
                    "因子 %s 计算失败 (%s: %s)，自动跳过并重新分配权重",
                    factor_id, type(exc).__name__, exc,
                )
                continue

        # 检查是否有足够的因子成功
        if len(per_factor) < self.min_factors:
            logger.error(
                "只有 %d 个因子成功（最少需要 %d 个），返回全 NaN",
                len(per_factor), self.min_factors,
            )
            return pd.DataFrame(np.nan, index=ref.index, columns=ref.columns)

        # Step 2: 重新归一化权重（跳过失败的因子）
        total_w = sum(abs(w) for w in per_factor_weights)
        if total_w <= 1e-12:
            norm_weights = [1.0 / len(per_factor)] * len(per_factor)
        else:
            norm_weights = [w / total_w for w in per_factor_weights]

        # Step 3: 加权求和（NaN-aware）
        composite = per_factor[0].mul(norm_weights[0])
        for df, w in zip(per_factor[1:], norm_weights[1:]):
            composite = composite.add(df.mul(w), fill_value=np.nan)

        # 对齐到参考形状
        composite = composite.reindex(index=ref.index, columns=ref.columns)

        # Step 4: TopN 选股（如果设置）
        if self.top_n is None and self.bottom_n is None:
            return composite

        return self._to_positions(composite)

    def _to_positions(self, composite: pd.DataFrame) -> pd.DataFrame:
        """将连续分数转换为离散仓位 {-1, 0, +1}

        Args:
            composite: 每天每个股票的组合分数

        Returns:
            仓位宽表
            - +1.0: 做多（top_n）
            - -1.0: 做空（bottom_n）
            - 0.0: 不持有
        """
        positions = pd.DataFrame(0.0, index=composite.index, columns=composite.columns)

        # 每天排序（降序）
        ranks_desc = composite.rank(axis=1, method="first", ascending=False, na_option="bottom")
        # 每天排序（升序）
        ranks_asc = composite.rank(axis=1, method="first", ascending=True, na_option="bottom")

        # 有效行（至少有一个非NaN值）
        valid_row = composite.notna().any(axis=1)

        # TopN: 做多前N只
        if self.top_n is not None:
            top_mask = ranks_desc.le(self.top_n) & composite.notna()
            top_mask = top_mask.mul(valid_row, axis=0).astype(bool)
            positions = positions.mask(top_mask, 1.0)

        # BottomN: 做空后N只
        if self.bottom_n is not None:
            bot_mask = ranks_asc.le(self.bottom_n) & composite.notna()
            bot_mask = bot_mask.mul(valid_row, axis=0).astype(bool)
            # 如果某个股票同时在 top 和 bottom（极端情况），优先做多
            bot_mask = bot_mask & ~positions.gt(0)
            positions = positions.mask(bot_mask, -1.0)

        return positions

    def generate(self, data_map: dict[str, pd.DataFrame]) -> dict[str, pd.Series]:
        """适配器：将宽表信号转换为回测引擎期望的格式

        回测引擎期望: {code: signal Series}
        本引擎输出: DataFrame (宽表)

        Args:
            data_map: {code: OHLCV DataFrame}

        Returns:
            {code: signal Series}
        """
        if not data_map:
            return {}

        codes = list(data_map.keys())
        all_dates = sorted(set().union(*(df.index for df in data_map.values())))
        date_index = pd.DatetimeIndex(all_dates)

        # 长表转宽表
        panel: dict[str, pd.DataFrame] = {}
        for col in ("open", "high", "low", "close", "volume"):
            frames: dict[str, pd.Series] = {}
            for code in codes:
                df = data_map[code]
                if col in df.columns:
                    frames[code] = df[col]
            if not frames:
                continue
            panel[col] = pd.DataFrame(frames).reindex(date_index)

        if "close" not in panel:
            raise ValueError("data_map 中的 DataFrame 必须包含 'close' 列")

        # 计算组合信号（宽表）
        composite = self.compute_signal(panel)

        # 如果没有设置 top_n/bottom_n，裁剪到 [-1, 1]
        if self.top_n is None and self.bottom_n is None:
            composite = composite.clip(-1.0, 1.0)

        # 宽表转长表
        out: dict[str, pd.Series] = {}
        for code in codes:
            if code in composite.columns:
                series = composite[code].reindex(data_map[code].index).fillna(0.0)
            else:
                series = pd.Series(0.0, index=data_map[code].index)
            out[code] = series

        return out
