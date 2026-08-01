# 策略回测引擎 v2.0 - 实现总结

## 🎯 核心改进

从 Vibe-Trading 项目移植了完整的回测引擎架构，实现了**策略与执行引擎解耦**，支持多策略信号回测。

## 📦 新增模块

### 1. SignalEngine 抽象基类 (`signal_engine.py`)
- **作用**：定义策略信号生成的标准接口
- **核心方法**：`generate(data_map) -> Dict[str, Series]`
- **输出**：每个标的的仓位权重序列（-1到1之间）
- **优势**：用户可自定义任意策略，无需修改核心引擎

### 2. ChinaAEngine A股市场引擎 (`engines/china_a.py`)
- **T+1规则**：当日买入不能卖出
- **涨跌停限制**：主板±10%，创业板/科创板±20%
- **整手交易**：最小100股，必须是100的倍数
- **费用结构**：
  - 佣金：万2.5（最低5元）
  - 印花税：万5（卖方）
  - 过户费：万0.1
- **滑点模型**：默认0.1%

### 3. 完整绩效指标 (`backtest_metrics.py`)
- **收益指标**：总收益、年化收益、月度收益
- **风险指标**：最大回撤、波动率、下行风险
- **风险调整**：夏普比率、Sortino比率、Calmar比率
- **交易质量**：胜率、盈亏比、平均持仓天数
- **相对表现**：Alpha、Beta、信息比率
- **稳健性**：自举法置信区间

### 4. 示例策略 (`signals/example_signals.py`)
1. **MACD金叉死叉** (`MacdSignalEngine`)
   - 参数：fast=12, slow=26, signal=9
   - 信号：MACD线上穿信号线买入，下穿卖出

2. **均线交叉** (`MaCrossSignalEngine`)
   - 参数：short=5, long=20
   - 信号：短期均线上穿长期均线买入，下穿卖出

3. **布林带均值回归** (`BollingerBandSignalEngine`)
   - 参数：period=20, num_std=2.0
   - 信号：价格触及下轨买入，触及上轨卖出

4. **RSI超买超卖** (`RsiSignalEngine`)
   - 参数：period=14, oversold=30, overbought=70
   - 信号：RSI<30买入，RSI>70卖出

### 5. 回测执行器 (`backtest_runner.py`)
- **数据加载**：从TuShare获取历史数据
- **策略注册**：自动发现可用策略
- **结果格式化**：返回绩效指标、逐笔交易、权益曲线

## 🛠️ Agent 工具集成

### 新增工具

#### 1. `run_signal_backtest`
运行基于信号引擎的策略回测

**参数**：
```json
{
  "strategy": "macd|ma_cross|bollinger|rsi",
  "symbols": ["000001.SZ", "600519.SH"],
  "start_date": "20220101",
  "end_date": "20240101",
  "strategy_params": {"fast": 12, "slow": 26, "signal": 9},
  "initial_cash": 1000000,
  "commission_rate": 0.00025,
  "slippage": 0.001
}
```

**返回**：
```json
{
  "strategy": "macd",
  "symbols": ["000001.SZ"],
  "period": {"start": "20220101", "end": "20240101"},
  "metrics": {
    "total_return": 0.325,
    "annual_return": 0.158,
    "sharpe": 1.23,
    "max_drawdown": -0.123,
    "win_rate": 0.58,
    "profit_factor": 1.8,
    ...
  },
  "trades": [...],
  "equity_curve": [...]
}
```

#### 2. `list_backtest_strategies`
列出所有可用的回测策略及其描述

## 📝 使用示例

### 场景1：用户问"帮我回测一下MACD策略在平安银行上的表现"

**Agent 识别意图** → 调用 `run_signal_backtest`：
```python
run_signal_backtest(
    strategy="macd",
    symbols=["000001.SZ"],
    start_date="20220101",
    end_date="20240101"
)
```

### 场景2：用户问"均线金叉买入死叉卖出的策略怎么样"

**Agent 识别策略** → 调用 `run_signal_backtest`：
```python
run_signal_backtest(
    strategy="ma_cross",
    symbols=["600519.SH"],
    start_date="20220101",
    end_date="20240101",
    strategy_params={"short_period": 5, "long_period": 20}
)
```

### 场景3：用户问"有哪些回测策略可以用"

**Agent 调用** → `list_backtest_strategies`：
```python
{
  "strategies": {
    "macd": "MACD 金叉死叉策略（快线 12，慢线 26，信号线 9）",
    "ma_cross": "均线交叉策略（短期 5 日，长期 20 日）",
    "bollinger": "布林带均值回归策略（周期 20，标准差倍数 2.0）",
    "rsi": "RSI 超买超卖策略（周期 14，超卖 30，超买 70）"
  }
}
```

## 🚀 扩展新策略

### 步骤1：创建 SignalEngine 子类

```python
from backend.app.quant.signal_engine import SignalEngine

class MyCustomSignalEngine(SignalEngine):
    def __init__(self, param1=10, param2=20):
        self.param1 = param1
        self.param2 = param2
    
    def generate(self, data_map):
        signals = {}
        for code, df in data_map.items():
            # 你的策略逻辑
            close = df['close']
            position = ...  # 计算仓位权重
            signals[code] = position
        return signals
```

### 步骤2：注册到策略注册表

在 `backtest_runner.py` 中添加：
```python
SIGNAL_ENGINE_REGISTRY["my_strategy"] = MyCustomSignalEngine
```

### 步骤3：更新工具描述

在 `strategy_backtest.py` 的 tool schema 中添加新策略到 enum。

## ✅ 与 Vibe-Trading 的对比

| 特性 | Vibe-Trading | hermes-stock v2.0 |
|------|--------------|-------------------|
| 策略扩展性 | ✅ SignalEngine | ✅ SignalEngine |
| A股规则 | ✅ ChinaAEngine | ✅ ChinaAEngine |
| 绩效指标 | 40+ | 20+（核心指标） |
| 数据加载 | 30+数据源 | TuShare（聚焦A股） |
| 组合优化 | ✅ | ❌（暂不需要） |
| Agent集成 | ❌ | ✅ 完整集成 |
| 自然语言触发 | ❌ | ✅ 智能识别 |

## 🎓 技术亮点

1. **避免未来函数**：信号 shift(1)，确保不使用未来数据
2. **避免偷看价格**：用 bar open 估值，而非 close
3. **资金不足处理**：等比缩放目标权重，保持组合比例
4. **T+1强制执行**：严格检查买入日期，阻止当日卖出
5. **涨跌停判断**：根据板块自动识别限制（10%/20%/30%）
6. **整手交易**：自动向下取整到100股

## 📊 性能指标详解

### 收益类
- `total_return`：总收益率
- `annual_return`：年化收益率
- `n_days`：回测天数

### 风险类
- `max_drawdown`：最大回撤
- `volatility`：年化波动率
- `downside_risk`：下行风险

### 风险调整
- `sharpe`：夏普比率（超额收益/波动率）
- `sortino`：Sortino比率（超额收益/下行风险）
- `calmar`：Calmar比率（年化收益/最大回撤）

### 交易质量
- `n_trades`：交易次数
- `win_rate`：胜率
- `profit_factor`：盈亏比
- `avg_pnl_pct`：平均收益率
- `avg_holding_bars`：平均持仓天数

### 相对表现
- `alpha`：超额收益（相对基准）
- `beta`：市场敏感度
- `information_ratio`：信息比率

## 🔧 后续优化方向

1. **更多策略**：海龟交易法、动量策略、配对交易等
2. **组合优化**：风险平价、均值方差、最大分散化
3. **基准对比**：自动获取沪深300/中证500基准
4. **稳健性检验**：Walk-forward分析、参数敏感性测试
5. **可视化报告**：HTML报告生成（权益曲线、交易标注）
6. **策略市场**：用户分享/购买策略

## 📚 参考

- Vibe-Trading 回测架构：`E:/workspace/Vibe-Trading-source/Vibe-Trading-main/agent/backtest/`
- A股交易规则：T+1、涨跌停、整手、费用结构
- 绩效指标计算：CFA Institute 标准

---

**分支**：`feat/backtest-vibe-port`  
**提交**：`25c1427`  
**状态**：✅ 已推送到 Gitee，等待合并到 master
