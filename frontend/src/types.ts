export type HealthPayload = {
  ok: boolean
  database: string
  event_count: number
  report_count: number
  log_count: number
  journal_mode: string
  integrity: string
  missing_tables: string[]
  user_version: number
}

export type EventItem = {
  event_id: string
  level: 'L0' | 'L1' | 'L2'
  event_type: string
  symbol: string | null
  symbol_name: string | null
  title: string
  summary: string | null
  source_type: string
  status: string
  created_at: string
  updated_at: string
  facts?: Record<string, unknown> | null
}

export type PositionItem = {
  id: number
  snapshot_id?: number
  symbol: string
  name: string
  position_pct: number | null
  cost_price: number | null
  quantity: number | null
  position_quantity?: number | null
  available_quantity?: number | null
  frozen_quantity?: number | null
  today_buy_quantity?: number | null
  today_sell_quantity?: number | null
  cost_amount?: number | null
  latest_price?: number | null
  market_value?: number | null
  floating_pnl?: number | null
  floating_pnl_pct?: number | null
  day_pnl?: number | null
  stop_price: number | null
  take_profit_price: number | null
  thesis: string | null
  risk_note: string | null
  status: string
  opened_at: string | null
  updated_at: string
  as_of_date?: string | null
  source_type?: string | null
  confidence?: number | null
  account_id?: number | null
  broker_name?: string | null
  broker_code?: string | null
  account_name?: string | null
  account_no_masked?: string | null
}

export type BrokerPreset = { code: string; name: string; short: string }

export type PositionAccount = {
  id: number
  broker_code: string | null
  broker_name: string
  account_name: string | null
  account_no_masked: string | null
  account_type: string
  currency: string
  status: string
  is_default: boolean
  sort_order: number
  total_asset: number | null
  available_cash: number | null
  holding_market_value: number | null
  total_pnl: number | null
  day_pnl: number | null
  holding_count: number
  fund_updated_at: string | null
  remark: string | null
  updated_at: string | null
}

export type PositionSummary = {
  account_count: number
  total_asset: number | null
  available_cash: number | null
  holding_market_value: number | null
  total_pnl: number | null
  day_pnl: number | null
  holding_count: number
}

export type PositionPriceSyncResult = {
  skipped: boolean
  status: 'success' | 'failed'
  message: string
}

export type PositionTrade = {
  id: number
  symbol: string
  name: string | null
  action: 'buy' | 'sell' | 'add' | 'reduce' | 'close'
  quantity: number | null
  price: number | null
  amount: number | null
  account_id: number | null
  broker_name: string | null
  source_type: string | null
  created_by: string | null
  position_id: number | null
  created_at: string
}

export type ChartResolution = '1D' | '1' | '5' | '15' | '30' | '60'

export type ChartBar = {
  time: string | number
  open: number
  high: number
  low: number
  close: number
  volume: number | null
  amount: number | null
  change_pct: number | null
}

export type ChartOverlayPoint = {
  time: string | number
  value: number
}

export type PositionChartPayload = {
  symbol: string
  name: string
  source: string
  resolution: ChartResolution
  error: string | null
  bars: ChartBar[]
  overlays: Record<string, ChartOverlayPoint[]>
  position: PositionItem | null
}

export type PositionAgentAddResult = {
  batch: {
    id: number
    status: string
    image_sha256: string | null
    created_at: string | null
  } | null
  model: Partial<ModelConfig> | null
  intent?: string | null
  should_save?: boolean
  reply?: string | null
  steps?: Array<{
    type: string
    tool?: string
    display_name?: string
    content: string
    success?: boolean
  }>
  rows: Array<PositionItem | { error: string; raw?: Record<string, unknown> }>
  summary: string | null
  pending_action?: PositionAgentPendingAction | null
}

export type PositionAgentStep = {
  type: string
  tool?: string
  display_name?: string
  content: string
  success?: boolean
}

export type PositionAgentPendingAction = {
  tool: 'save_position_holdings'
  arguments: {
    rows?: Array<Record<string, unknown>>
    account?: Record<string, unknown>
    as_of_date?: string
    [key: string]: unknown
  }
  review_required?: boolean
  validations?: PositionSymbolValidation[]
}

export type PositionSymbolValidation = {
  index?: number
  valid: boolean
  status: string
  input_symbol: string
  input_name?: string | null
  normalized_symbol: string
  canonical_name?: string | null
  message: string
  suggested_symbol?: string | null
  suggested_name?: string | null
}

export type PositionOcrRow = {
  symbol: string
  name: string
  position_quantity: number
  available_quantity?: number | null
  frozen_quantity?: number | null
  today_buy_quantity?: number | null
  today_sell_quantity?: number | null
  cost_price?: number | null
  cost_amount?: number | null
  latest_price?: number | null
  market_value?: number | null
  floating_pnl?: number | null
  floating_pnl_pct?: number | null
  day_pnl?: number | null
  position_pct?: number | null
  confidence?: number | null
  action: 'replace'
}

export type PositionOcrRecognizeResult = {
  schema_version: 'position_ocr.v1'
  image_sha256: string
  model: Partial<ModelConfig>
  account: Record<string, unknown>
  as_of_date: string | null
  rows: PositionOcrRow[]
  validations: PositionSymbolValidation[]
  valid_count: number
  invalid_count: number
}

export type PositionOcrConfirmResult = {
  schema_version: 'position_ocr.v1'
  batch_id: number | null
  status: string | null
  saved_count: number
  error_count: number
  rows: Array<PositionItem | { error: string; raw?: Record<string, unknown> }>
  summary: string
}

export type PositionAgentStreamEvent = {
  type: string
  model?: Partial<ModelConfig> | null
  intent?: string | null
  should_save?: boolean
  reply?: string | null
  summary?: string | null
  content?: string
  message?: string
  rows?: Array<PositionItem | { error: string; raw?: Record<string, unknown> }> | null
  tool?: string
  display_name?: string
  success?: boolean
  step?: PositionAgentStep
  pending_action?: PositionAgentPendingAction | null
}

export type AgentItem = {
  agent_name: string
  display_name: string
  enabled: boolean
  role_description: string | null
  pending_count: number
  running_count: number
  report_count: number
  last_update: string | null
  status: 'ready' | 'running' | 'disabled'
}

export type SystemLog = {
  id: number
  level: string
  module: string
  message: string
  context: Record<string, unknown> | string | null
  created_at: string
}

export type WatchlistItem = {
  id: number
  symbol: string
  name: string
  group_name: string
  reason: string | null
  status: string
  added_at: string
  updated_at: string
}

export type WatchlistAgentPendingAction = {
  tool: 'add_watchlist_items' | 'remove_watchlist_items'
  arguments: {
    rows: Array<{
      symbol?: string
      name?: string
      group_name?: string
      reason?: string
    }>
  }
  original_instruction?: string
  candidates?: Array<{ symbol: string; name: string; market?: string }>
}

export type WatchlistAgentStreamEvent = {
  type: string
  model?: Partial<ModelConfig> | null
  intent?: string | null
  should_save?: boolean
  reply?: string | null
  summary?: string | null
  content?: string
  message?: string
  tool?: string
  display_name?: string
  success?: boolean
  step?: PositionAgentStep
  pending_action?: WatchlistAgentPendingAction | null
}

export type ReportItem = {
  report_id: string
  agent_name: string
  display_name: string | null
  symbol: string | null
  symbol_name: string | null
  title: string
  conclusion: string | null
  risk_points: string | null
  action_suggestion: string
  model_name: string | null
  created_at: string
}

export type RiskCheck = {
  id: number
  symbol: string | null
  symbol_name: string | null
  risk_level: string
  risk_type: string
  finding: string
  required_action: string | null
  position_limit: number | null
  stop_condition: string | null
  cool_down_required: number
  created_at: string
}

export type DashboardMarketItem = {
  symbol: string
  name: string
  price: number | null
  change_pct: number | null
  trade_date: string | null
  source: string
  updated_at: string | null
  sparkline: number[]
}

export type DashboardRiskEvent = {
  kind: 'risk' | 'event'
  id: string
  level: 'high' | 'medium' | 'low'
  symbol: string | null
  symbol_name: string | null
  title: string
  summary: string | null
  created_at: string | null
}

export type DashboardResearch = ReportItem

export type DashboardPosition = PositionItem & {
  stop_distance_pct: number | null
  day_pnl_pct: number | null
  latest_research: DashboardResearch | null
  latest_risk: RiskCheck | null
  risk_state: {
    level: 'high' | 'medium' | 'low'
    label: string
  }
}

export type DashboardRun = {
  run_id: string
  job_key: string
  name: string
  digital_human_code: string | null
  trigger_type: string
  status: string
  started_at: string | null
  finished_at: string | null
  duration_ms: number | null
  report_id: string | null
  output_preview: string | null
  output_content?: string | null
  error: string | null
}

export type DashboardApproval = AgentToolApproval & {
  session_id: number
  session_title: string | null
  created_at: string | null
}

export type DashboardPayload = {
  title: string
  market_status: string
  market_state: {
    code: string
    label: string
    tone: 'ok' | 'warning' | 'muted' | string
  }
  server_time: string
  generated_at: string
  pipeline: string[]
  disclaimer: string
  counts: {
    open_positions: number
    watchlist: number
    l0_events: number
    reports: number
    high_risk_24h: number
    pending_approvals: number
    running_jobs: number
  }
  portfolio: PositionSummary & { day_pnl_pct: number | null }
  market: {
    source: string
    items: DashboardMarketItem[]
  }
  risk_summary: {
    high: number
    medium: number
    low: number
    since: string
  }
  risk_events: DashboardRiskEvent[]
  recent_events: EventItem[]
  positions: DashboardPosition[]
  reports: DashboardResearch[]
  agents: AgentItem[]
  runtime: {
    running: DashboardRun[]
    recent: DashboardRun[]
  }
  approvals: DashboardApproval[]
  next_job: {
    job_key: string
    name: string
    run_at: string
    trading_day_only: boolean
    digital_human_code: string | null
  } | null
  data_freshness: {
    positions: string | null
    market: string | null
    risk: string | null
    reports: string | null
    runs: string | null
  }
  today: {
    reports: number
  }
  logs: SystemLog[]
}

export type UserPayload = {
  id: number
  username: string
  display_name: string
  role: string
  permissions: string[]
  last_login_at: string | null
}

export type LoginPayload = {
  access_token: string
  token_type: 'bearer'
  expires_in: number
  remember_me: boolean
  user: UserPayload
}

export type ModelPlatform = {
  id: number
  name: string
  provider_type: string
  base_url: string
  api_key_masked: string | null
  has_api_key: boolean
  status: string
  remark: string | null
  updated_at: string | null
}

export type ModelConfig = {
  id: number
  platform_id: number | null
  platform_name: string | null
  provider_name: string
  display_name: string
  base_url: string
  api_key_masked: string | null
  has_api_key: boolean
  model_name: string
  is_active: boolean
  status: string
  temperature: number
  max_steps: number
  remark: string | null
  updated_at: string | null
}

export type DataSourceConfig = {
  provider: 'tushare' | 'tavily' | 'embedding' | 'rerank' | string
  display_name: string
  provider_type: string
  base_url: string | null
  model: string | null
  api_key_masked: string | null
  has_api_key: boolean
  key_source: 'database' | 'environment' | 'missing' | string
  status: string
  remark: string | null
  updated_at: string | null
}

export type NotifyChannel = {
  channel_key: 'feishu-daily' | string
  name: string
  channel_type: string
  purpose: 'schedule' | string
  webhook_masked: string | null
  has_webhook: boolean
  cipher_invalid: boolean
  has_secret: boolean
  status: string
  remark: string | null
  last_ok_at: string | null
  last_error: string | null
  updated_at: string | null
}

export type NotifyChannelTestResult = {
  ok: boolean
  message?: string
  error?: string
  status_code?: number | null
  latency_ms: number
}

export type FeishuBotRuntime = {
  state: 'online' | 'connecting' | 'stopped' | 'error'
  running: boolean
  manager_running: boolean
  started_at: string | null
  today_count: number
  last_event_at: string | null
  error: string | null
}

export type FeishuBotBinding = {
  profile_id: number
  code: string
  name: string
  app_id_masked: string | null
  has_secret: boolean
  cipher_invalid: boolean
  enabled: boolean
  feature_enabled: boolean
  runtime: FeishuBotRuntime
}

export type FeishuBotTestResult = {
  ok: boolean
  message?: string
  error?: string
  latency_ms: number
}

export type ScheduleJob = {
  job_key: string
  name: string
  description: string | null
  job_kind: 'agent_task' | 'agent_report' | 'health_check' | 'rule_scan' | 'l1_digest' | string
  schedule_type: 'once' | 'daily' | 'weekly' | 'monthly_last' | 'interval' | string
  run_time: string | null
  run_once_at: string | null
  timezone: string
  weekday_mask: string | null
  interval_minutes: number | null
  window_start: string | null
  window_end: string | null
  trading_day_only: boolean
  digital_human_code: string | null
  agent_name: string | null
  prompt_template: string | null
  push_channel_key: string | null
  owner_user_id: number | null
  created_by_run_id: string | null
  created_via: string
  allow_code_execution: boolean
  enabled: boolean
  sort_order: number
  last_run_at: string | null
  last_status: string | null
  last_error: string | null
  updated_at: string | null
}

export type JobRun = {
  run_id: string
  job_key: string
  job_name?: string | null
  trigger_type: string
  status: string
  started_at: string | null
  finished_at: string | null
  duration_ms: number | null
  report_id: string | null
  notification_id: number | null
  output_preview: string | null
  output_content?: string | null
  error: string | null
  created_at: string | null
}

export type PageResult<T> = {
  items: T[]
  page: number
  page_size: number
  total: number
  pages: number
}

export type ScheduleJobPage = PageResult<ScheduleJob> & {
  enabled_total: number
}

export type ScheduleStatus = {
  scheduler_enabled: boolean
  scheduler_alive: boolean
  heartbeat_at: string | null
  is_trading_day: boolean
  server_time: string
  today: { success: number; failed: number; running: number }
  notify_channel?: {
    name: string
    configured: boolean
    signed: boolean
    enabled: boolean
    last_ok_at: string | null
    last_error: string | null
  }
}

export type DigitalHuman = {
  id: number
  code: string
  name: string
  avatar_tone: string
  target_roles: string
  description: string
  system_prompt: string
  quick_prompts: string[]
  status: string
  sort_order: number
  can_write_positions: boolean
  delegation_enabled: boolean
  delegation_description: string | null
  feishu_bot_enabled: boolean
  updated_at: string | null
}

export type DigitalHumanToolMeta = {
  name: string
  display_name: string
  description: string
}

export type DigitalHumanCapabilities = {
  can_write_positions: boolean
  common_tools: DigitalHumanToolMeta[]
  role_tools: DigitalHumanToolMeta[]
  role_fallback_code: string | null
  write_tools: DigitalHumanToolMeta[]
  workspace_tools?: DigitalHumanToolMeta[]
}

export type WorkspaceDoc = {
  path: string
  name: string
  bytes: number
  mtime: number
  title: string
  created_by: string | null
  digital_human_code: string | null
  session: { id: number; title: string; chat_mode: string } | null
  ingested: boolean
  ingested_doc_id: number | null
  ingestable: boolean
  artifact_id: string | null
  active_version: number | null
}

export type ArtifactVersion = {
  artifact_id: string
  version: number
  parent_version: number | null
  sha256: string
  bytes: number
  mime_type: string | null
  validation_status: string
  active: boolean
  run_id: string | null
  created_at: string | null
}

export type WorkspaceStatus = {
  enabled: boolean
  exec_enabled: boolean
  exec_available: boolean
  exec_backend: string
  isolated: boolean
}

export type KnowledgeIngestProgress = {
  running: boolean
  processed: number
  total: number
  indexed: number
  failed: number
  last_result: string
}

export type KnowledgeStats = {
  documents: number
  chunks: number
  indexed_docs: number
  indexed_pct: number
  searchable_chunks: number
  vector_count: number
  vector_pct: number
  vector_healthy: boolean
  vector_error: string
  backend: string
  collection: string
  dim: number
  by_type: Record<string, number>
  ingest: KnowledgeIngestProgress
}

export type KnowledgeDocument = {
  id: number
  title: string
  doc_type: string
  source: string
  source_url: string | null
  trust_level: string
  published_at: string | null
  fetched_at: string | null
  chunk_count: number
  indexed: boolean
  status: string
}

export type KnowledgeDocumentPage = {
  total: number
  page: number
  page_size: number
  items: KnowledgeDocument[]
}

export type KnowledgeChunk = {
  id: number
  chunk_index: number
  content: string
  token_count: number
  status: string
  hit_count: number
}

export type KnowledgeSearchHit = {
  chunk_id: number
  chunk_index: number
  doc_id: number
  title: string
  source: string
  doc_type: string
  source_url: string | null
  content: string
  vector_score: number
  keyword_score: number
  fused_score: number
  rerank_score: number | null
}

export type PromptRecommendation = {
  id: number
  digital_human_id: number
  digital_human_code: string
  chat_mode: 'research' | 'stock' | 'knowledge' | string
  prompt_text: string
  reason: string | null
  source_type: string
  score: number
  status: string
  expires_at: string | null
  updated_at: string | null
}

export type AgentSession = {
  id: number
  title: string
  chat_mode: 'research' | 'stock' | 'knowledge' | string
  digital_human_id: number | null
  digital_human_name: string | null
  model_config_id: number | null
  model_display_name: string | null
  status: string
  message_count: number
  updated_at: string
  source_type: 'web' | 'feishu' | 'scheduler' | 'delegation' | 'workflow' | string
  source_label: string
  source_identifier: string | null
  read_only: boolean
  is_child_task: boolean
  child_task_index: number | null
}

export type AgentMessage = {
  id: number
  role: 'user' | 'assistant'
  content: string
  attachments: AgentAttachment[]
  error: string | null
  duration_ms: number | null
  backtest_run_id: string | null
  created_at: string
}

export type ApprovalPolicy = 'ask' | 'smart' | 'full'

export type AgentAttachment = {
  path: string
  name: string
  mime_type: string
  bytes: number
  kind: 'image' | 'document'
  preview_url?: string
}

export type AgentToolApproval = {
  approval_id: string
  tool: string
  display_name: string
  risk_level: 'low' | 'medium' | 'high'
  arguments_preview: string
  expires_at: string
  status: 'pending' | 'approved' | 'denied' | 'expired' | 'cancelled'
  session_id?: number
  session_title?: string | null
  created_at?: string | null
}

export type AgentSessionDetail = {
  id: number
  title: string
  chat_mode: 'research' | 'stock' | 'knowledge' | string
  digital_human_id: number | null
  digital_human_name: string | null
  model_config_id: number | null
  model_display_name: string | null
  source_type: 'web' | 'feishu' | 'scheduler' | 'delegation' | 'workflow' | string
  source_label: string
  source_identifier: string | null
  read_only: boolean
  is_child_task: boolean
  child_task_index: number | null
  messages: AgentMessage[]
}

export type StockStrategy = {
  id: string
  name: string
  description: string
}

export type StrategyDescriptor = {
  code: string
  name: string
  description: string
  explain: string
  scenario: string
  risk_level: 'low' | 'medium' | 'high'
  kind: 'single' | 'cross_sectional'
  default_symbols: string[]
  default_params: Record<string, number>
  symbol_labels?: Record<string, string>
}

export type StockCandidate = {
  ts_code: string
  symbol: string
  name: string
}

export type AgentSkillCriterion = {
  code: string
  description: string
  required: boolean
  min_evidence: number
  validation_profile: string
  max_age_days: number | null
}

export type AgentSkill = {
  name: string
  display_name: string
  summary: string
  description: string
  version: string
  category: string
  risk_level: 'low' | 'medium' | 'high' | string
  compatibility: string
  license: string
  chat_modes: string[]
  roles: string[]
  tool_names: string[]
  allowed_tools: string[]
  resources: string[]
  output_contract: string[]
  professional_body_available: boolean
  content_sha256: string
  criteria: AgentSkillCriterion[]
  available_tool_names: string[]
  unavailable_tool_names: string[]
  availability: 'ready' | 'partial'
  readiness_percent: number
  instructions?: string
  professional_body?: string
  metadata?: Record<string, string>
}

export type SwarmTeamMember = {
  role: string
  label: string
  task: string
}

export type SwarmTeam = {
  id: string
  name: string
  description: string
  icon: string
  members: SwarmTeamMember[]
  objective_template: string
  tags: string[]
  member_count: number
  member_roles: string[]
}

export type AgentWorkflowTemplate = {
  code: string
  name: string
  description: string
  category: string
  version: number
  variables: Record<string, { required?: boolean; default?: string; description?: string }>
  nodes: Array<{
    key: string
    role: string
    agent_code: string
    depends_on: string[]
    expected_output?: string | null
  }>
}

export type AgentWorkflowRun = {
  workflow_run_id: string
  template_code: string
  template_version: number
  objective: string
  variables: Record<string, unknown>
  status: string
  deadline_at: string | null
  finished_at: string | null
  summary: string | null
  nodes: Array<{
    node_key: string
    node_index: number
    role_name: string
    agent_code: string
    depends_on: string[]
    child_run_id: string | null
    status: string
    attempt: number
    max_attempts: number
    result: Record<string, unknown>
    error: string | null
    started_at: string | null
    finished_at: string | null
  }>
  error: string | null
}

export type AgentStreamEvent = {
  type: string
  run_id?: string
  seq?: number
  session_id?: number
  chat_mode?: 'research' | 'stock' | 'knowledge'
  profile?: DigitalHuman | null
  model?: Partial<ModelConfig> | null
  role?: 'thought' | 'answer'
  tool?: string
  tool_call_id?: string
  display_name?: string
  message?: string
  content?: string
  step?: number
  stage?: string
  status?: string
  state?: 'complete' | 'incomplete' | 'not_applicable'
  success?: boolean
  duration?: number
  duration_ms?: number
  // tool_progress（工具参数生成进度）：index=第几个工具调用，chars=已生成参数字数，delta=content 明文预览增量
  index?: number
  chars?: number
  delta?: string
  path?: string
  name?: string
  bytes?: number
  mtime?: number
  approval_id?: string
  risk_level?: 'low' | 'medium' | 'high'
  arguments_preview?: string
  expires_at?: string
  decision?: 'approved' | 'denied' | 'expired' | 'cancelled'
  attempt?: number
  max_attempts?: number
  retry_after_ms?: number
  reset_stream?: boolean
  team_id?: string
  workflow_run_id?: string
  template_code?: string
  template_version?: number
  node_key?: string
  node_count?: number
  role_name?: string
  depends_on?: string[]
  child_run_id?: string
  agent_code?: string
  agent_name?: string
  member_count?: number
  requested_task_count?: number
  accepted_task_count?: number
  agent_count?: number
  merged_task_count?: number
  rejected_task_count?: number
  completed?: number
  total?: number
  summary?: string
  deadline_at?: string
  report_run_id?: string
  report_url?: string
  task?: string
  symbols?: string[]
  selected_skills?: string[]
  required?: string[]
  report_sections?: string[]
  execution?: Record<string, unknown>
  retry_round?: number
  max_retries?: number
  reason?: string
}

export type BacktestMetrics = {
  initial_cash: number
  final_equity: number
  total_return: number
  annual_return: number
  max_drawdown: number
  sharpe: number
  sortino: number
  calmar: number
  win_rate: number
  profit_loss_ratio: number
  trade_count: number
  winning_trades: number
  losing_trades: number
  avg_holding_days: number
  benchmark_return?: number | null
  excess_return?: number | null
  information_ratio?: number | null
  alpha_daily?: number | null
  alpha_annual?: number | null
  alpha_t_stat?: number | null
  beta?: number | null
  r_squared?: number | null
  annual_volatility?: number
  rebalance_count?: number
  turnover?: number
  total_cost?: number
}

export type BacktestListItem = {
  run_id: string
  symbol: string
  symbol_name: string | null
  strategy_code: string
  timeframe: string
  start_date: string
  end_date: string
  confidence: string
  metrics: BacktestMetrics
  created_at: string | null
  report_url: string
}

export type BacktestPricePoint = {
  time: string
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export type BacktestIndicatorSeries = {
  key: string
  label: string
  color: string
  pane: 'price' | 'osc'
  histogram?: boolean
  points: Array<{ time: string; value: number }>
}

export type BacktestTrade = {
  symbol: string
  direction: 'long' | 'short'
  entry_date: string
  exit_date: string
  entry_price: number
  exit_price: number
  quantity: number
  holding_days: number
  gross_pnl: number
  fees: number
  pnl: number
  return_pct: number
  exit_reason: string
}

export type BacktestRun = {
  run_id: string
  report_url: string
  status: string
  report_type?: 'single' | 'portfolio'
  symbol: string
  symbols?: string[]
  benchmark_label?: string
  strategy: {
    code: string
    name: string
    timeframe: string
    source: string
  }
  period: {
    start_date: string
    end_date: string
    trading_days: number
  }
  config: Record<string, unknown>
  metrics: BacktestMetrics
  benchmark_metrics?: BacktestMetrics
  confidence: 'insufficient' | 'low' | 'medium' | string
  rating: string
  warnings: string[]
  price_series: BacktestPricePoint[]
  indicator_series?: BacktestIndicatorSeries[]
  trade_markers: Array<{
    time: string
    position: 'aboveBar' | 'belowBar'
    shape: 'arrowUp' | 'arrowDown'
    text: string
  }>
  equity_curve: Array<{ time: string; equity: number; drawdown: number }>
  benchmark_curve?: Array<{ time: string; equity: number }>
  asset_curves?: Array<{
    symbol: string
    points: Array<{ time: string; value: number }>
  }>
  weight_series?: Array<{
    time: string
    weights: Record<string, number>
  }>
  rebalance_history?: Array<{
    time: string
    turnover: number
    cost: number
    weights: Record<string, number>
    risk_contributions: Record<string, number>
  }>
  trades: BacktestTrade[]
  attribution: {
    top_winner?: BacktestTrade | null
    top_loser?: BacktestTrade | null
    remove_top_winner_pnl?: number | null
    exit_reasons: Array<{
      reason: string
      count: number
      total_pnl: number
      avg_pnl: number
      win_rate: number
    }>
    asset_stats?: Array<{
      symbol: string
      asset_return: number
      average_weight: number
      average_risk_contribution: number
      pnl_contribution: number
      return_contribution: number
    }>
    concentration?: {
      net_profit: number
      top_contributor?: {
        symbol: string
        pnl_contribution: number
        return_contribution: number
      } | null
      top_positive_share?: number | null
      remove_top_contributor_pnl?: number | null
      total_cost: number
    }
    market_regression?: {
      benchmark: string
      beta?: number | null
      alpha_annual?: number | null
      alpha_t_stat?: number | null
      r_squared?: number | null
    }
    regime_analysis?: {
      status: string
      reason?: string
      window_days?: number
      regimes?: Array<{
        code: string
        label: string
        trading_days: number
        annual_return?: number | null
        sharpe?: number | null
      }>
    }
    statistical_tests?: {
      status: string
      reason?: string
      method?: string
      iterations?: number
      annual_excess_return?: number
      excess_return_p_value?: number
      significant?: boolean
      actual_max_drawdown?: number
      drawdown_worse_percentile?: number
    }
  }
  summary_markdown: string
  source_code?: string | null
  data_fingerprint: string
}

// Agent 任务看板
export type AgentBoardCounts = {
  queued: number
  running: number
  approval: number
  completed: number
  abnormal: number
}

export type AgentTask = {
  run_id: string
  session_id: number | null
  task_title: string
  status: string
  board_column: 'queued' | 'running' | 'approval' | 'completed' | 'abnormal'
  priority: number
  agent_name: string | null
  agent_code: string | null
  agent_avatar: string | null
  wait_seconds: number | null
  run_seconds: number | null
  attempts: number
  max_attempts: number
  error: string | null
  created_at: string | null
  started_at: string | null
  finished_at: string | null
  job_run: { job_key: string; status: string } | null
  scheduled_job: { name: string; job_key: string } | null
  team: { team_id: string; objective: string } | null
  current_step: string | null
  recent_tool: string | null
}

export type AgentTaskDetail = {
  run_id: string
  task_title: string | null
  status: string
  priority: number
  agent_name: string | null
  agent_code: string | null
  source_type: string
  source_ref: string | null
  parent_run_id: string | null
  team_id: string | null
  attempts: number
  max_attempts: number
  error: string | null
  created_at: string | null
  started_at: string | null
  finished_at: string | null
  queued_at: string | null
  delegation: {
    delegation_id: string
    title: string | null
    action_key: string | null
    instruction: string
    expected_output: string | null
    required: boolean
    extraction_source: string
  } | null
  team: {
    team_id: string
    leader_run_id: string
    objective: string
    status: string
    execution_mode: string
    completion_policy: string
  } | null
  job: {
    job_run_id: string
    job_key: string
    status: string
    started_at: string | null
    scheduled_job_name: string | null
  } | null
}

export type AgentTaskEvent = {
  event_id: number
  seq: number
  event_type: string
  payload_json: string
  created_at: string | null
}

export type AgentBoardAgent = {
  id: number
  code: string
  name: string
  avatar_url: string | null
}

export type TaskBatch = {
  team_id: string
  leader_run_id: string
  objective: string
  status: string
  execution_mode: string
  completion_policy: string
  deadline_at: string | null
  finished_at: string | null
  children: Array<{
    delegation_id: string
    title: string | null
    action_key: string | null
    agent_name: string | null
    agent_code: string
    status: string
    priority: number
    created_at: string | null
  }>
}

export type FactorBenchmarkItem = {
  id: string
  zoo: string
  ic_mean: number
  ic_std: number
  ir: number
  ic_positive_ratio: number
  ic_count: number
  rank_stability: number
  train_ic: number | null
  test_ic: number | null
  train_ic_count: number
  test_ic_count: number
  category: 'alive' | 'reversed' | 'dead'
  ic_series?: Array<{ time: string; value: number }>
}

export type FactorDedupResult = {
  threshold: number
  kept_ids: string[]
  dropped: Array<{ id: string; max_corr_with_kept: number }>
  note: string
}

export type FactorSymbolCoverage = {
  total: number
  with_data: number
  empty_symbols: string[]
  usable_dates: number
}

export type FactorBenchmarkResult = {
  results: FactorBenchmarkItem[]
  skipped: Array<{ id: string; reason: string }>
  dedup: FactorDedupResult | null
  coverage: {
    symbols: string[]
    start_date: string | null
    end_date: string | null
    rows: number
    dates: number
  }
  symbol_coverage: FactorSymbolCoverage
  refresh_failures: Array<{ symbol: string; error: string }>
  methodology: string
}
