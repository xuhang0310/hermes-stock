# SixRabbits Stock 设计系统 v1

> 本文档是全站 UI 的唯一权威来源。新增或修改任何界面元素时，必须**引用**本文档的 token 与组件，禁止重新发明。防回归测试从本文档派生（见 §7）。
> 最近修订：2026-09-20（基于对 `frontend/src` 的量化审计）。

---

## 1. 审计结果（实测数据，不是印象）

对 `frontend/src` 全部 `.vue` / `.css` 文件的扫描结果：

### 1.1 颜色：四套并行配色

| 配色体系 | 位置 | 实测值 | 规模 |
|---|---|---|---|
| ① 全局暖墨金 | `styles.css` + 多数页面 | `--bg #050505`、`--gold #d5b46a`、`--gold-2 #f1d48a` | 基准体系 |
| ② Screener 私有 | `ScreenerView.vue` | `--sc-accent: #d7b76f`（第二个金）、`--sc-panel: #121518/#171b1f`（第二套深色） | 全套私有 token |
| ③ GitHub-dark | `AgentTaskBoardView.vue`、`BacktestHistoryView.vue` | `#0d1117 #161b22 #21262d #30363d #484f58 #8b949e #e6edf3` | 全套外来色板 |
| ④ 一次性色值 | `AutomaticSelectionView.vue`（67 处）、`FactorAnalysisView.vue`（`#eabf63` 第三个金，29 处） | `#785f2d #75602f #8a713b…` | 无体系 |

全局 `--gold` 在 15 个视图中的引用次数分布极不均匀：工作台/持仓/自选等老页面 0 次（各有硬编码），新页面 7–20 次。**金色在站内有 `#d5b46a`、`#d7b76f`、`#eabf63` 三个不同取值。**

### 1.2 字号：28 个不同取值

```
12px ×183   11px ×176   10px ×122   13px ×90    14px ×30
11.5px ×38  12.5px ×37  9px ×37     16px ×22    10.5px ×22
15px ×21    18px ×18    19px ×16    其余 9.5/13.5/14.5/17/20/21/22/23/24/25/26/28/32/38/42px
```

- 10–12px 合计 **481 处**，14px 基准仅 30 处：全站实际以“小字”为主，与设计基准（14px）倒挂。
- 存在 9.5/10.5/11.5/12.5/13.5/14.5 六个半档，纯粹是逐页手调的结果。

### 1.3 圆角：16 个取值

`6px ×87、50% ×48、8px ×41、999px ×39、5px ×32、4px ×28、7px ×27、3px ×16、9px ×12、2px ×10、10–16px 若干`。全局定义了 `--radius: 8px`，实际被遵守的比例不到两成；2–10px 九个连续值说明每页都在各取所好。

### 1.4 字体与组件

- 全局雅黑（`"Microsoft YaHei UI", "PingFang SC", …`）；`styles.css` 模型中心区域另有 **8 处 Georgia 衬线**（Hub 标题、对话框标题、指标数字）——两种展示声部并存。
- 按钮至少 5 套：`.sc-btn` ×33、`.auto-button` ×17、`.ia-primary`、全局按钮、页内私有变体，高度 31–42px 不等。
- 此前三个新页面把日期/数字切成 Consolas 等宽字体（15 处），已于 2026-09-20 清除并加防回归断言；等宽字体目前仅存于日志面板与 JSON 编辑框（合法场景）。

**结论：品牌身份（暖墨金暗色研究终端）成立且值得保留；问题不是“换设计”，而是实现层存在四代并行方言，需要把身份写成代码。**

---

## 2. 设计原则

1. **证据优先。** 数字、日期、覆盖率、ID 一律 `font-variant-numeric: tabular-nums`；缺失数据显示 `—`，不估算、不伪装、不拼接其他日期的数据。
2. **一个声音。** 全站一种无衬线字体。Georgia 衬线只允许存在于模型中心过渡期内，W5 波次清除（见 §6）——研究终端不需要两个展示声部。
3. **颜色有语义。** 金 = 选中/主操作/品牌；绿/琥珀/红 = 通过/警示/失败；红/绿仅在涨跌语境出现（A 股红涨绿跌，反用即缺陷）；青/粉仅用于 Agent 角色与协作标识。装饰性用色一律禁止。
4. **密度服从内容。** 本产品是密度型研究台，但最小正文字号为 12px（现行 10px 全部升档）；密度靠栅格与行高控制，不靠缩字号。
5. **页面无 Hero。** 页面标题与副标题归 AppShell；页内第一屏必须是可操作内容（命令行 + 工作区），禁止页内大标题、诗化标语、装饰性 eyebrow/序号。
6. **扁平层级。** 层级用 1px 边框与表面色阶表达；阴影只属于模态层（对话框/浮层）。

---

## 3. Token 规范

### 3.1 颜色

**基色（沿用现有 `:root`，冻结取值）**

| token | 值 | 用途 |
|---|---|---|
| `--bg` | `#050505` | 页面底色 |
| `--bg-2` | `#0b0b0c` | 凹陷区（表格头、输入底） |
| `--surface` | `#111113` | 面板/卡片 |
| `--surface-2` | `#171719` | 面板内嵌、工具条 |
| `--surface-3` | `#1d1c18` | 悬停底、分段控件 |
| `--text` | `#f2f0e8` | 正文 |
| `--muted` | `#9d9a8f` | 次要文本 |
| `--muted-2` | `#6d6a61` | 元信息 |
| `--line` | `#2a2925` | 分隔线 |
| `--line-strong` | `#4c4430` | 面板外框 |

**金色（唯一强调色，三档）**

| token | 值 | 用途 |
|---|---|---|
| `--gold` | `#d5b46a` | 选中态、主按钮、品牌 |
| `--gold-2` | `#f1d48a` | 强调文字、悬停 |
| `--gold-dim` | `#7d6736` | 描边、弱强调 |

**语义色（冻结）**：`--ok #27c58a`、`--warning #d5a642`、`--danger #ff5d5d`、`--cyan #2bd4d4`、`--pink #f254b8`。
涨跌：`pnl-up = var(--danger)`（红涨）、`pnl-down = var(--ok)`（绿跌）。

**迁移映射表（机械替换，不允许中间态）**

| 现有值 | 目标 token | 说明 |
|---|---|---|
| `#d7b76f`（--sc-accent） | `--gold` | Screener 私有金 |
| `#eabf63` | `--gold` | FactorAnalysis 第三金 |
| `#ead08f`（--sc-accent-strong） | `--gold-2` | |
| `#0d1117` | `--bg-2` | GitHub 底 |
| `#161b22` | `--surface` | GitHub 面板 |
| `#21262d` | `--surface-2` | |
| `#30363d` | `--line` | |
| `#484f58` | `--muted-2` | |
| `#8b949e` | `--muted` | |
| `#e6edf3` | `--text` | |
| `#785f2d #75602f #8a713b #745e2d #b38a34` 等 | 按语义归 `--gold` / `--gold-dim` / `--line-strong` | 自动选股 67 处硬编码逐一归位 |

### 3.2 字体刻度（28 档 → 8 档）

| 角色 | 规格 | 用途 |
|---|---|---|
| `meta` | 12px / 400 / 1.5 | 元信息、表头、次要数据（现行 10/11px 全部升档到此） |
| `body-sm` | 13px / 400 / 1.55 | 密集表格正文、卡片副文 |
| `body` | 14px / 400 / 1.6 | 正文、说明段落 |
| `card-title` | 16px / 500 / 1.35 | 面板标题 h3 |
| `page-title` | 20px / 500 / 1.3 | 页内区块标题 h2（页面标题归 AppShell） |
| `kpi` | 24px / 500 / 1.2 + tabular-nums | KPI 数字、核心指标 |
| `hero-num` | 32px / 500 / 1.1 + tabular-nums | 仅首屏关键大数（每页 ≤4 个） |
| `code` | 12px ui-monospace | 仅日志、JSON、SQL、配置编辑 |

映射规则：9–12px → 12；12.5–13.5px → 13；14–14.5px → 14；15–16px → 16；17–20px → 20；21–26px → 24；28px+ → 32。数字一律 `font-variant-numeric: tabular-nums`（不换字体）。

### 3.3 间距（4px 栅格）

`space-1=4, space-2=8, space-3=12, space-4=16, space-5=20, space-6=24, space-8=32, space-12=48`。padding/margin/gap 只许取这些值；面板内边距只许 16/20/24；页面区块间距只许 12/16/24。

### 3.4 圆角（16 档 → 3 档 + 圆形）

| token | 值 | 用途 |
|---|---|---|
| `--r-ctrl` | `4px` | 按钮、输入、标签、小卡 |
| `--r-panel` | `8px` | 面板、卡片、对话框 |
| `--r-pill` | `999px` | 胶囊、状态点 |

映射：2/3/5/6/7px → 4px；9–16px → 8px；`50%` 保留用于头像与圆点。

### 3.5 层级 z-index

`sticky-header 2 → drawer 40 → top-notice 60 → dialog 1000 → toast 1100`。禁止其他取值。

### 3.6 动效

时长 120–160ms、`ease`；只响应用户动作（打开/选中/确认），禁止无触发循环装饰（唯一例外：加载 spinner）；`prefers-reduced-motion` 下全部禁用。

---

## 4. 组件规范

> 附录 B 给出可直接落地的基类 CSS。所有变体只允许通过修饰类组合，禁止页内重写按钮/面板。

### 4.1 按钮 `.ui-btn`

- 变体：`primary`（金底深字，主操作，每屏 ≤1）、`ghost`（透明描边，次操作）、`danger`（破坏性，需确认）、`text`（无框链接式）。
- 尺寸：`md` 高 34 / `sm` 高 28；`padding: 7px 14px`（sm 5/10）；半径 `--r-ctrl`；字号 13px / 600。
- 状态：hover 边框/文字升档；`focus-visible` 2px `--gold` 外描边 offset 2px；disabled `opacity .5` + `cursor: not-allowed`；loading 内置 spin 且禁用。
- 废除：`.sc-btn`、`.auto-button`、`.ia-primary`（迁移期可用 `@extend` 别名过渡）。

### 4.2 面板 `.ui-panel`

- `background: var(--surface)`、`border: 1px solid var(--line)`、`radius: --r-panel`、padding 16/20。
- `.ui-panel-head`：min-height 56px、底部 1px `--line`、标题 `card-title`；右侧操作区。
- 面板嵌套时用 `--surface-2` 区分，不加第二道边框阴影。

### 4.3 表格 `.ui-table`

- 容器独立滚动；表头 `position: sticky; top: 0; z-index: 2; background: var(--surface-2)`。
- 行高：舒适 52px / 紧凑 44px（修饰类）；数字右对齐 + tabular-nums；符号/代码左对齐。
- 选中行：`background: linear-gradient(90deg, rgba(213,180,106,.1), transparent)` + `box-shadow: inset 2px 0 var(--gold)`；可点行必须 `tabindex="0"` + Enter/Space 触发 + `aria-selected`。
- 涨跌色必须用高特异性覆盖（`.ui-table td.pnl-up/down`），防止被单元格基色吞掉。

### 4.4 KPI 条 `.ui-kpi`

- 连续单元格、1px 分隔、数字 `kpi` 档 tabular-nums、标签 `meta` 档 muted；每页一条，≤6 格。

### 4.5 状态胶囊 `.ui-pill`

- 四态：`ok / warning / danger / muted`；`radius: --r-pill`、字号 12px、1px 语义描边 + 8% 语义底色；不做第五种颜色。

### 4.6 标签页 `.ui-tabs`

- 下划线式（禁止药丸式）：活动项 2px `--gold` 底线 + `accent-strong` 文字；`role=tablist/tab/tabpanel` 齐全；键盘 ←/→/Home/End 漫游（不得只有 roving tabindex 没有方向键处理）。

### 4.7 空状态 `.ui-empty`

- 主句 14px 说“发生了什么/缺什么”，次句 12px 说“怎么解决”，最多一个主动作按钮；禁止只有一句“暂无数据”。

### 4.8 对话框

- 优先原生 `<dialog>`（`showModal()`），Teleport 兜底；宽度 `min(560px, 92vw)`，表单型可 720px；backdrop `rgba(0,0,0,.72)`；必须具备：标题关联、`aria-modal`、Esc 关闭、Tab 焦点圈定、关闭后焦点归还触发元素、body 滚动锁定、移动端近全屏。

### 4.9 表单字段 `.ui-field`

- 标签 12px muted 上置；控件高 34px、半径 `--r-ctrl`、底 `--bg-2`；聚焦：边框 `--gold` + 2px 14% 金色柔光；区间输入用“低—高”双框同排，禁止堆三个独立输入。

---

## 5. 页面骨架（只允许三种布局）

| 布局 | 结构 | 适用页 |
|---|---|---|
| L1 文档流 | 单栏 max-width 内容 | 报告、设置、通知 |
| L2 主从 | 主区 fluid + 300–340px 侧栏 | 行业分析、因子评估 |
| L3 工作台 | 轨 220–260px / 主区 fluid / 检查器 300–360px | AI 选股构建、自动选股驾驶舱、数字人中心 |

**页首命令行**（可选）：左侧上下文（目标日/策略/状态），右侧主操作（≤1 个 primary + ≤2 个 ghost）。命令行之下即内容，无任何 Hero 区。

**响应式断点全站统一**：`1380 / 980 / 680`。1380 检查器下移；980 单栏；680 触控目标 ≥44px、表格横向滚动带吸顶表头。

---

## 6. 迁移计划

| 波次 | 范围 | 动作 | 验收 |
|---|---|---|---|
| W0 | `styles.css` | 追加 §3 全部 token 与附录 B 组件基类（纯新增，零破坏）；新建 `tests/designSystem.test.mjs` | 全量测试 + 构建 |
| W1 | `ScreenerView.vue` | `--sc-*` 映射到全局 token；`.sc-btn` → `.ui-btn`；半档字号入刻度 | 页面测试 + 生产目视 |
| W2 | `AutomaticSelectionView.vue` | 67 处硬编码按 §3.1 映射归位；`.auto-button` → `.ui-btn` | 同上 |
| W3 | `AgentTaskBoardView.vue`、`BacktestHistoryView.vue` | GitHub 色板按 §3.1 映射全量替换 | 同上 |
| W4 | `FactorAnalysisView`、`PositionChartView`、`KnowledgeBaseView`、`OpsConsoleView` | 第三金/私有色归位、按钮收敛 | 同上 |
| W5 | 模型中心（styles.css Georgia 区） | 衬线字体清除，标题入刻度 | 同上 |

每波纪律：单页单次提交；测试全绿 + 构建 + Jenkins 部署 + 生产目视后，才开下一波。

---

## 7. 防回归

`frontend/tests/designSystem.test.mjs`（W0 建立）对全部 `.vue` 文件断言：

1. 禁止出现 `eyebrow` / `ia-eyebrow` / `sc-eyebrow` 类名与装饰性序号（`section-number`）。
2. 禁止 `Consolas|ui-monospace|monospace` 出现在白名单外（白名单：日志面板、JSON/SQL 编辑）。
3. 禁止新增硬编码金系色值：`#d7b76f|#eabf63|#ead08f|#785f2d|#75602f|#8a713b|#745e2d`（token 已覆盖）。
4. `font-size` 只允许 `12/13/14/16/20/24/32px`（`code` 场景除外）。
5. `border-radius` 只允许 `4px/8px/999px/50%`（`var(--r-*)` 优先）。
6. 涨跌只允许经 `pnl-up/pnl-down` 或语义 token 表达（红涨绿跌反向即失败）。

迁移波次中每个页面测试复用这些断言，并追加本页专属断言。

---

## 附录 A：`styles.css` 追加 token 草案

```css
:root {
  /* 语义别名（不改变既有 token 取值，供组件层统一引用） */
  --accent: var(--gold);
  --accent-strong: var(--gold-2);
  --accent-dim: var(--gold-dim);
  --up: var(--danger);   /* A 股红涨 */
  --down: var(--ok);     /* A 股绿跌 */

  /* 圆角 */
  --r-ctrl: 4px;
  --r-panel: 8px;
  --r-pill: 999px;

  /* 间距（4px 栅格） */
  --space-1: 4px; --space-2: 8px; --space-3: 12px; --space-4: 16px;
  --space-5: 20px; --space-6: 24px; --space-8: 32px; --space-12: 48px;

  /* 层级 */
  --z-sticky: 2; --z-drawer: 40; --z-notice: 60; --z-dialog: 1000; --z-toast: 1100;

  /* 动效 */
  --motion: 140ms ease;
}
```

## 附录 B：组件基类 CSS 草案（节选，W0 落 `styles.css`）

```css
/* 按钮 */
.ui-btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 7px;
  min-height: 34px; padding: 7px 14px; border: 1px solid transparent;
  border-radius: var(--r-ctrl); background: var(--surface-2); color: var(--text);
  font-size: 13px; font-weight: 600; transition: border-color var(--motion), color var(--motion);
}
.ui-btn:hover:not(:disabled) { border-color: var(--accent-dim); color: var(--accent-strong); }
.ui-btn:disabled { cursor: not-allowed; opacity: .5; }
.ui-btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.ui-btn--primary { border-color: #785f2d; background: linear-gradient(180deg, #c29d4f, #8e6f33); color: #100d07; }
.ui-btn--danger { border-color: #7d3030; background: #5a2424; color: #ffd1d1; }
.ui-btn--ghost { background: transparent; }
.ui-btn--text { min-height: 0; padding: 0; border: 0; background: none; color: var(--accent-strong); }
.ui-btn--sm { min-height: 28px; padding: 5px 10px; font-size: 12px; }

/* 面板 */
.ui-panel { background: var(--surface); border: 1px solid var(--line); border-radius: var(--r-panel); }
.ui-panel-head { display: flex; align-items: center; justify-content: space-between; gap: 12px;
  min-height: 56px; padding: 0 16px; border-bottom: 1px solid var(--line); }
.ui-panel-head > h3 { margin: 0; font-size: 16px; font-weight: 500; }
.ui-panel-body { padding: 16px; }

/* 表格 */
.ui-table-wrap { overflow: auto; }
.ui-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.ui-table th { position: sticky; top: 0; z-index: var(--z-sticky); background: var(--surface-2);
  color: var(--muted); font-size: 12px; font-weight: 500; text-align: left; padding: 9px 12px;
  border-bottom: 1px solid var(--line); white-space: nowrap; }
.ui-table td { padding: 12px; border-bottom: 1px solid var(--line); color: var(--text);
  font-variant-numeric: tabular-nums; white-space: nowrap; }
.ui-table tr[aria-selected="true"] td { background: rgba(213, 180, 106, .08);
  box-shadow: inset 2px 0 var(--accent); }
.ui-table td.pnl-up { color: var(--up); }
.ui-table td.pnl-down { color: var(--down); }

/* 状态胶囊 */
.ui-pill { display: inline-flex; align-items: center; border: 1px solid; border-radius: var(--r-pill);
  padding: 2px 8px; font-size: 12px; white-space: nowrap; }
.ui-pill--ok { border-color: rgba(39, 197, 138, .35); background: rgba(39, 197, 138, .08); color: #6fd8b2; }
.ui-pill--warning { border-color: rgba(213, 166, 66, .4); background: rgba(213, 166, 66, .1); color: #e6c36f; }
.ui-pill--danger { border-color: rgba(255, 93, 93, .4); background: rgba(255, 93, 93, .08); color: #ff8b8b; }
.ui-pill--muted { border-color: var(--line); background: var(--surface-2); color: var(--muted); }
```
