# 选股回测：接手执行方案

> **2026-11-12 更新：第 1 到 3 步已完成。** 两关都已跑完，结果见 `docs/quant-order-plan.md` 第 4.10 节：30 只通过，10 只没通过（回撤超限）。最终验证不能再跑。最新名单在 `~/hermes-stock-research/_reports/selection_picks_20261110.json`（带名称的版本是同目录的 `_named.csv`）。剩下的是第 4 步：写接入线上的方案，等用户确认。

写于 2026-11-11 22:25。给接手的模型（Opus 5.5）照着做。背景和判定标准在 `docs/quant-order-plan.md` 第 4.10 节，先读那一节。

## 目标

用历史数据检验：每 10 个交易日买排名前 10 或 30 只，扣费后能不能跑赢“全部可选股票等权”。通过才出名单、才接到自动选股；不通过就明说不行。**不要为了让结果通过而改任何东西。**

## 现在做到哪了

| 事项 | 状态 |
|---|---|
| 回测模块 `backend/app/quant/research_selection.py` | 写完 |
| 测试 `tests/test_research_selection.py`（24 个） | 全部通过 |
| 判定标准（`docs/quant-order-plan.md` 第 4.10 节） | 已写定，看结果前 |
| `CLAUDE.md` 的说明 | 已加 |
| 假数据全流程试跑（含真实 LightGBM） | 通过：没规律时都不过，有规律时模型能找出来 |
| 研究数据下载 | **进行中**，见下 |
| 第一关 `dev`、第二关 `final`、名单 `picks` | 都还没跑 |

所有改动都没有提交，这台机器没有装 git。

## 这台机器的情况

- 系统 Python 是 3.14，没有 pip，也没有 numpy。运行用临时环境：
  `/tmp/claude-1000/-workspace-hermes-stock/68a9800b-440b-4b87-b4eb-31893acfa2bc/scratchpad/venv/bin/python`（下面写作 `$V`），里面有 numpy、pandas、lightgbm、scipy、pytest。
  这个目录没了就重建：同目录 `uvbin/uv venv venv --python 3.14`，再 `uvbin/uv pip install --python venv/bin/python numpy pandas lightgbm scipy pytest`。
- 命令都在仓库根目录 `/workspace/hermes-stock` 跑，前面加 `PYTHONPATH=.`。
- 2 核，内存 7G（空闲约 3G），磁盘剩 5.7G。
- 访问 TuShare 只能走代理，每次请求约 4 秒，偶尔断连。绕过代理直连被权限拦了，不要再试。
- TuShare token 用户在对话里给过，只通过环境变量 `STOCK_TUSHARE_TOKEN` 传，**不要写进任何文件**。

## 第 1 步：等数据下完

下载脚本是临时目录里的 `pull.py`（只下日线 `daily` 和每日指标 `daily_basic`，回测只用这两类），日志是同目录的 `pull.log`。数据在 `~/hermes-stock-research`。

- 一共 3605 个交易日（2012-01-04 到 2026-11-10），两类数据合计 7210 个文件。
- 22:23 时下了 43 个，速度约每分钟 30 个，**估计要 4 小时左右**。
- 可以断点续传。进程没了就重跑，已下的不会再下：

```bash
STOCK_TUSHARE_TOKEN=<token> PYTHONPATH=. $V <临时目录>/pull.py 20261110 > <临时目录>/pull.log 2>&1
```

下完后检查。`daily` 和 `daily_basic` 都要是 3605 个文件、缺 0 个；其他三类数据（资金流向、涨跌停、筹码）没下，显示缺失是正常的：

```bash
PYTHONPATH=. $V -m backend.app.quant.research_data check --out ~/hermes-stock-research
```

**如果下载太慢或被限流**：第一关只需要 2018 年及以前的数据。可以先只下到 20181231 跑第一关，第一关不通过就不用下后面的了。做法是把 `pull.py` 的结束日期参数改成 `20181231`。注意：之后再补下 2019 年以后的数据时，缓存和特征会自动重建。

## 第 2 步：跑第一关（2015 到 2018 年）

```bash
PYTHONPATH=. $V -m backend.app.quant.research_selection dev --out ~/hermes-stock-research 2>&1 | tee <临时目录>/dev.log
```

- 第一次会先把按日文件拼成矩阵缓存（`~/hermes-stock-research/_cache`），要几分钟，占 2G 左右磁盘。跑之前看一眼 `df -h ~`，不够 3G 先告诉用户。
- 用后台方式跑，预计十几分钟到半小时。
- 结果写到 `~/hermes-stock-research/_reports/selection_dev_20181231.json`，屏幕上有一张表和一行“进最终验证的做法”。

**跑完先做合理性检查，再看通不通过：**

1. 基准年化应该和这几年的小盘股走势对得上：2015 年大涨、2016 到 2018 年偏弱，四年合起来不应该是很大的正数。
2. 年换手如果超过 20 倍，说明持有带没起作用，要查原因。
3. model 和 composite 的超额如果高得离谱（比如年化超额超过 60%），先怀疑有未来数据或者选到了买不进的股票，查清楚再往下。
4. 2017 年大概率是三种做法里最差的一年（第 4.8 节里那年模型 IC 只有 0.046），这是预期内的。

如果发现的是**代码错误**（比如算错了、用了未来数据），可以修，修完加测试、重跑 `dev`，并在文档里写明改了什么、为什么。如果只是**结果不好**，不许改常量、特征、标准。

## 第 3 步：按结果分两条路

### 第一关没有做法通过

到此为止，**不要跑 `final`**，2019 年以后的数据留着。然后：

1. 把第一关的表格和“没有通过”写进 `docs/quant-order-plan.md` 第 4.10 节的“结果”。
2. 告诉用户：现在的自动选股没有经过历史验证，新做法也没通过。建议在自动选股页面注明“规则未经历史验证”。要不要改页面，等用户决定。
3. 可以提的下一步（要另写标准、另开小节，不是改这一版）：补测估值因子 `ep`、`bp`；下载财务数据做基本面因子，按公告日对齐。

### 第一关有做法通过

跑第二关。**这一步只能跑一次**，报告文件存在后代码会拒绝再跑。跑之前确认：数据已经下到 20261110 并且检查通过；第一关的合理性检查都过了。

```bash
PYTHONPATH=. $V -m backend.app.quant.research_selection final --out ~/hermes-stock-research 2>&1 | tee <临时目录>/final.log
```

- 这一步读全部年份，内存吃紧。跑之前 `free -g` 看一眼；如果被系统杀掉（没有写出报告文件），报告文件不存在，可以重跑，这不算看过结果。
- 不管通过不通过，把表格和判定填进第 4.10 节的“结果”，**原样填，不挑着写**。

第二关通过后：

```bash
PYTHONPATH=. $V -m backend.app.quant.research_selection picks --out ~/hermes-stock-research --top-n 10
```

把名单给用户看，同时说清楚：这是研究结论，不是收益承诺；系统不下单；资金量大时开盘价买不到。

## 第 4 步：通过之后怎么接到线上（先出方案，等用户同意再动手）

> **2026-11-16：已完成。** 用户确认后按下面的要点接入，作为挑战者 `model-rank-30`，见 `docs/automatic-selection.md`“模型排名挑战者”。

不要直接改 `DEFAULT_STRATEGY_CONFIG` 或现有评分实现，那会改变 config_hash 和指纹，破坏现有运行的可追溯性。要点：

- 线上每天已经在算模型 1 的排名，存在 `stock_model_signals.model_rank`（`shadow/rank_signal.latest_ranks`）。新选股可以直接读它，不用再训练。
- 线上的模型是 `quant/rank_model.json`，用全部数据训练的导出版，和回测里逐年训练的不是同一个文件，但特征和参数相同。方案里要写明这一点。
- 自动选股要新增一种评分方式（按 `model_rank` 排名，加 2000 万元成交额下限、每 10 个交易日换一次、前 2N 持有带），作为新的策略版本，先当挑战者和现在的方案并行，不替换。
- 需要更新 `SCORING_IMPLEMENTATION_VERSION`，并补自动选股的测试。
- 涉及页面、调度、数据库，改动不小，先写方案给用户确认。

## 不能做的事

- 不改 `research_selection.py` 顶部的常量和判定函数里的门槛。
- 不重跑 `final`，不删最终报告文件再跑。
- 第一关没通过，不看 2019 年以后的结果。
- 不把 token 写进文件，不提交研究数据。
- 通过之前不把新做法接到自动选股。
- 不对用户说“能赚钱”。只说检验结果是什么。

## 汇报方式

用户说中文，汇报用中文、说人话：先说通过还是没通过，再给表格里的关键数字（年化、基准、超额、跑赢年数、最大回撤），最后说下一步。
