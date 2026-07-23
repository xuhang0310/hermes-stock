# 量化因子库集成说明

## 范围

Hermes 已引入 Vibe-Trading 的因子计算核心和 462 个预构建 Alpha：

| 因子库 | 数量 | 主要用途 |
| --- | ---: | --- |
| Qlib 158 | 154 | 动量、波动率、成交量、价格位置等通用特征 |
| Kakushadze Alpha 101 | 101 | 价量关系、横截面排序和短周期交易信号 |
| 国泰君安 191 | 191 | 面向 A 股的技术、价量和微观结构信号 |
| 学术异象 | 12 | 动量、反转、流动性、质量等经典研究因子 |
| PIT 基本面 | 4 | 使用发布时点安全的基本面字段，避免未来函数 |

迁移内容位于 `backend/app/quant/factors`。该目录保留上游 MIT
许可证，Qlib 派生内容保留 Apache-2.0 NOTICE。Hermes 的数据库适配、
评估和 API 实现在 `backend/app/quant/factor_service.py` 与
`backend/app/quant/factor_api.py`。

## 运行链路

1. 从 Hermes `daily_bars` 读取本地行情。
2. 本地行情不足时，可通过现有 TuShare 数据源补齐。
3. 将长表行情转换为因子引擎需要的宽表 OHLCV 面板。
4. 计算指定 Alpha，并返回原始值和当日横截面百分位。
5. 基准测试使用次日收益计算每日横截面 Spearman IC，并汇总 IC、IR、
   正 IC 比例和有效样本数。

系统不会把“因子值高”直接解释为“可以买”。每个因子必须先在目标市场、
标的池和时间范围内完成验证，再由 Agent 结合基本面、风险和交易约束解释。

## HTTP API

- `GET /api/factors/health`：检查因子注册状态。
- `GET /api/factors`：按因子库、主题、标的池和关键词检索。
- `GET /api/factors/{alpha_id}`：查看公式、输入字段和说明。
- `POST /api/factors/score`：对指定标的池计算单因子横截面分数。
- `POST /api/factors/benchmark`：对最多 20 个因子执行 IC/IR 基准测试。

接口要求 `agent:use` 权限。单次评分最多 50 个标的，单次基准测试最多
20 个因子，避免用户请求占满 API 工作进程。

## Agent 工具

量化分析师、个股研究员和总调度 PM 可以渐进加载 `factor-research` 技能，
并使用：

- `list_factor_alphas`
- `get_factor_alpha`
- `score_factor_universe`
- `benchmark_factor_alphas`

技能要求先披露公式、数据覆盖和验证结果，再输出结论；证据不足时必须标记
`incomplete`，不能把回测缺口包装成投资建议。

## 当前边界

- 基准测试目前是研究级 IC/IR 验证，不是完整组合回测。
- 尚未计入手续费、冲击成本、涨跌停、停牌和成交容量。
- 尚未做行业、市值和风格中性化。
- PIT 基本面因子需要对应的时点安全财务字段；字段缺失时会跳过并返回原因。
- 因子库包含来自不同市场的公式，不能默认在 A 股有效。

生产化下一步应增加交易日历对齐、复权处理、截面去极值和标准化、行业/市值
中性化、组合分层回测、成本模型、因子版本与实验结果持久化。
