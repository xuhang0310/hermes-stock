---
name: code-and-backtest
description: 在受控工作区编写并执行 Python 代码、数据处理、指标计算和策略回测，记录参数、输入、输出、成本假设和错误。用户要求执行 Python、计算数据、验证策略、做回测、生成图表或用代码产生研究产物时使用。
compatibility: Requires Hermes Stock workspace execution to be enabled, user approval when required, and Python dependencies available in the sandbox.
metadata:
  hermes-display-name: 代码执行与回测
  hermes-category: execution
  hermes-version: "3.0.0"
  hermes-risk-level: high
allowed-tools: ws_run_python ws_write_file ws_read_file
---

# 代码执行与回测

代码执行是一项受控能力。使用最小权限、可复现输入和明确输出，不让研究脚本获得不必要的宿主机访问。

## 执行前

1. 明确计算目标、输入数据、输出文件和成功条件。
2. 检查是否允许执行代码，是否需要用户审批。
3. 只在工作区内读写，禁止读取凭据、系统目录和无关用户文件。
4. 网络、依赖安装和外部命令遵守运行时权限。
5. 优先修改已有脚本；只有没有可复用实现时才新建。

## 回测协议

1. 定义标的池、信号、持有期、调仓规则和基准。
2. 固定数据区间、复权口径和缺失值处理。
3. 避免未来函数、幸存者偏差和事后选择参数。
4. 纳入手续费、滑点、涨跌停、停牌和成交能力约束。
5. 分训练/验证或样本内/样本外区间。
6. 报告收益、波动、最大回撤、换手、交易次数和分阶段表现。
7. 做参数敏感性、基准比较和失效区间检查。

详细协议见 [references/backtest-protocol.md](references/backtest-protocol.md)。

## 运行后

- 检查退出码、超时、标准输出和错误输出；
- 校验生成文件的存在、哈希和格式；
- 保存脚本、参数和输入数据标识；
- 失败时保留可诊断错误，不把部分结果当成完整结果；
- 无输出且无产物的成功退出不能自动满足研究证据。

## 输出

说明做了什么、使用什么输入、执行结果、耗时、产物路径、局限和复现方法。
