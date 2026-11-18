# 给在这个仓库里写代码的 AI 的规矩

项目结构和各模块的说明见 `CLAUDE.md`，开工前先读。下面是硬规矩，Claude、Gemini 和其他 AI 都要遵守。

## 安全边界

- 系统只做研究、盯盘和复盘，不接券商、不下单。不要加任何交易通道。
- 不提交 `backend/.env`、各种 token 和研究数据。

## 量化规则上线前必须验证

- 任何打分规则、权重、阈值、过滤条件，上线前先把通过标准写进文档（写明日期），再用 2018 年及以前的数据回测。
- 通过后用 2019 年以后的数据测一次，只能跑一次；脚本要在结果文件已经存在时拒绝重跑。
- 回测按次日开盘成交，停牌和一字板不能成交，扣手续费，基准是同一批可选股票等权平均。
- 结果原样写进文档。不通过就写不通过，不为了通过去改参数。
- 没有回测结果的规则，可以作为“挑战者”并排记录，但不能在页面上给出“买入”“动手”这类结论，注释和文档里也不能写“经过实证校准”“经过验证”。
- 不要改 `backend/app/quant/research_selection.py` 顶部的常量和判定门槛，不要重跑它的 `final`。

## 每个问题只有一个答案，不要另写一套

| 问题 | 唯一来源 |
|---|---|
| 挂买价 | 买入模型：`stock_model_signals.buy_k` → `order_engine.model_buy_price`；模型说不挂就不给价 |
| 止损 | 可以不设；设了不能比 3 倍 ATR 更近：`order_engine.STOP_ATR`、`plan_orders(...).stop` |
| 股数 | `shadow/rules.target_shares` + `plans/quant_sizes.budget_for` × `shadow/rules.rank_tilt` |
| 选股名单 | 自动选股 `quant/automatic_selection_*`，验证过的是挑战者 `model-rank-30` |
| 成绩 | 自动选股的前瞻评估；操作建议的成绩单 `scorecard/` |

要加新功能，先找现有模块扩展，不要另起一套同名或同用途的东西。量化选股（L1–L7）的现状和还没做的事见 `docs/quant-selection.md`。

## 协作

- 开工前看 `git status` 和 `git log`，确认没有别人没提交的改动；同一块代码一次只让一个 AI 改。
- 改了行为，要同步改 `CLAUDE.md` 和 `docs/` 里对应的说明。
- 提交前跑相关的测试；跑不了要在提交说明里写明。
