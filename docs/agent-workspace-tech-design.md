# Agent 工作区与 Python 沙箱技术方案

> 适用项目：SixRabbits Stock
> 当前状态：文件工具、Docker Python 执行、Office/PDF 产物链路已实现
> 默认策略：文件工作区开启，代码执行关闭；生产验证通过后显式开启

## 1. 设计结论

本项目采用 Hermes Agent 一类项目常见的“可替换执行后端”设计，不把 gVisor 作为前置条件：

- `local` 后端仅供受信任的本地开发环境使用，没有 OS 隔离。
- `docker` 后端用于生产，普通 Docker 配合固定安全参数。
- 公共 API 账号 `hermes` 不加入 `docker` 组，也不挂载 `docker.sock`。
- 独立账号 `hermes-sandbox` 运行窄接口 broker，API 仅通过 Unix Socket 提交用户 ID、相对脚本路径和超时。
- broker 不接受调用方指定镜像、命令、挂载点、环境变量、容器名或 Docker 参数。

这与 Hermes Agent、claw-code 的实际思路一致：先定义清楚信任边界和授权，再选普通进程或容器后端。claw-code 当前 Linux 本地沙箱基于 `unshare`，并不是 gVisor；Hermes Agent 同时提供 local、Docker、SSH 等后端。本项目是服务端多人工作区，生产默认选择普通 Docker。

## 2. 运行链路

```text
浏览器（本条消息勾选“执行 Python”）
  -> POST /api/agent/chat { allow_code_execution: true }
  -> run_agent_events 仅为本次请求下发 ws_run_python
  -> workspace_user_scope + workspace_execution_scope 双重上下文
  -> DockerExecutionBackend
  -> /run/hermes-stock-sandbox/sandbox.sock
  -> hermes-stock-sandboxd
  -> 当前用户专属容器
  -> /workspace/scripts/*.py
  -> /workspace/exports/*.{pdf,docx,xlsx,pptx,...}
  -> SSE artifact 事件
  -> 聊天下载按钮 + “生成文档”页面
```

飞书机器人、定时任务和其他非交互调用不传 `allow_code_execution`，因此即使全局开关已开启，也拿不到 `ws_run_python`。

## 3. 权限边界

代码执行需要同时满足：

1. `STOCK_WORKSPACE_ENABLED=true`。
2. `STOCK_WORKSPACE_EXEC_ENABLED=true`。
3. 当前请求显式传入 `allow_code_execution=true`。
4. 当前请求已经通过 `agent:use` 鉴权。
5. 脚本位于当前登录用户自己的工作区，扩展名为 `.py`，大小不超过 2MB。
6. Docker broker、固定镜像和用户容器健康。

网页开关是一次性的，发送后立即复位。提示词只负责告诉模型何时该执行；真正的授权由后端 ContextVar 和工具列表过滤完成，不依赖模型自觉。

## 4. Docker 隔离参数

每个应用用户对应一个可复用容器对象。每次运行前启动，运行后立即停止，避免后台子进程跨请求存活。

固定限制：

| 项目 | 默认值 |
| --- | --- |
| 网络 | `none` |
| 根文件系统 | 只读 |
| Host 挂载 | 仅当前用户工作区，读写 |
| Linux capabilities | 全部移除 |
| 提权 | `no-new-privileges` |
| CPU | 2 核 |
| 内存/交换 | 1GB / 1GB |
| PID | 128 |
| 单文件 | 64MB |
| 工作区 | 256MB、2000 文件 |
| `/tmp` | 256MB tmpfs，`noexec,nosuid,nodev` |
| 执行超时 | 默认 30 秒，最大 120 秒 |
| stdout/stderr | broker 各最多 32KB，应用返回各最多 8000 字符 |

容器内不包含项目代码、生产 `.env`、数据库文件和 Docker Socket。子进程环境从白名单重新构造，不继承任何 `STOCK_*`、数据库或模型密钥。

## 5. 文档能力

沙箱镜像预装：

- 数据与图表：NumPy、pandas、matplotlib、seaborn、Pillow。
- Word：python-docx。
- Excel：openpyxl、XlsxWriter。
- PowerPoint：python-pptx。
- PDF：ReportLab、pypdf。
- 中文字体：Noto CJK。

脚本执行前后会比较工作区文件快照，新建或修改的 PDF、DOCX、XLSX、PPTX、CSV、HTML、图片等文件作为 `artifact` 事件推送。生成文档页直接扫描用户工作区，因此即使数据库登记失败，文件仍可下载。

PDF、DOCX、PPTX、XLSX 和文本格式可提取正文后加入知识库；图片当前仅下载，不自动 OCR 入库。

## 6. 文件工具

| 工具 | 约束 |
| --- | --- |
| `ws_read_file` | 只读工作区文本，单文件 2MB，二进制拒绝 |
| `ws_write_file` | 写 UTF-8 文本，单文件 2MB，记录审计 |
| `ws_edit_file` | 唯一文本片段替换，避免误改 |
| `ws_glob` | 最多返回 200 个文件 |
| `ws_grep` | 最多返回 100 个命中 |
| `ws_run_python` | 仅运行已经写入工作区的 `.py` 文件，必须获得本次请求授权 |

所有路径统一经过 `Path.resolve()` 和 `relative_to()`。用户目录本身不能是符号链接，文件符号链接解析后也必须留在用户目录内。

## 7. 部署

部署脚本会：

1. 安装后端文档解析依赖。
2. 构建 `hermes-stock-python-sandbox:latest`。
3. 创建无登录权限的 `hermes-sandbox` 账号并仅将该账号加入 `docker` 组。
4. 安装并启动 `hermes-stock-sandboxd.service`。
5. 先验证 broker，再滚动重启后台实例和两个 API 副本。

关键配置：

```env
STOCK_WORKSPACE_DIR=/opt/hermes-stock/workspace
STOCK_WORKSPACE_EXEC_ENABLED=true
STOCK_WORKSPACE_EXEC_BACKEND=docker
STOCK_WORKSPACE_EXEC_SOCKET=/run/hermes-stock-sandbox/sandbox.sock
STOCK_WORKSPACE_EXEC_IMAGE=hermes-stock-python-sandbox:latest
```

## 8. 已知边界

- 普通 Docker 比宿主机 subprocess 安全得多，但不是针对恶意多租户的虚拟机边界。若未来向不受信任的公网用户开放，再评估 gVisor、Kata Containers 或 microVM。
- 工作区配额在运行前后检查，单文件还受内核 `RLIMIT_FSIZE` 约束；底层文件系统未启用项目级磁盘配额。
- 网络默认完全关闭。需要外部数据时，应先用系统已有的行情、搜索或知识库工具取数，再把结果写入工作区交给脚本处理。
- broker 账号可访问 Docker daemon，因此它是高权限控制面；其 Unix Socket 不对公网暴露，接口必须保持窄且不可接受任意 Docker 参数。
