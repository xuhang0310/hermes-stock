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
  total_asset: number
  available_cash: number
  holding_market_value: number
  total_pnl: number
  day_pnl: number
  holding_count: number
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
}

export type PositionAgentStep = {
  type: string
  tool?: string
  display_name?: string
  content: string
  success?: boolean
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

export type DashboardPayload = {
  title: string
  market_status: string
  pipeline: string[]
  disclaimer: string
  counts: {
    open_positions: number
    watchlist: number
    l0_events: number
    reports: number
  }
  recent_events: EventItem[]
  positions: PositionItem[]
  agents: AgentItem[]
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
  provider: 'tushare' | 'tavily' | string
  display_name: string
  provider_type: string
  base_url: string | null
  api_key_masked: string | null
  has_api_key: boolean
  key_source: 'database' | 'environment' | 'missing' | string
  status: string
  remark: string | null
  updated_at: string | null
}

export type NotifyChannel = {
  channel_key: 'feishu-alert' | 'feishu-daily' | string
  name: string
  channel_type: string
  purpose: 'alert' | 'daily' | string
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
  running: boolean
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
  job_kind: 'agent_report' | 'health_check' | 'rule_scan' | 'l1_digest' | string
  schedule_type: 'daily' | 'weekly' | 'monthly_last' | 'interval' | string
  run_time: string | null
  weekday_mask: string | null
  interval_minutes: number | null
  window_start: string | null
  window_end: string | null
  trading_day_only: boolean
  digital_human_code: string | null
  agent_name: string | null
  prompt_template: string | null
  push_channel_key: string | null
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
  trigger_type: string
  status: string
  started_at: string | null
  finished_at: string | null
  duration_ms: number | null
  report_id: string | null
  notification_id: number | null
  output_preview: string | null
  error: string | null
  created_at: string | null
}

export type ScheduleStatus = {
  scheduler_enabled: boolean
  scheduler_alive: boolean
  heartbeat_at: string | null
  is_trading_day: boolean
  server_time: string
  today: { success: number; failed: number; running: number }
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
  updated_at: string | null
}

export type PromptRecommendation = {
  id: number
  digital_human_id: number
  digital_human_code: string
  chat_mode: 'research' | 'stock' | string
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
  chat_mode: 'research' | 'stock' | string
  digital_human_id: number | null
  digital_human_name: string | null
  model_config_id: number | null
  model_display_name: string | null
  status: string
  message_count: number
  updated_at: string
}

export type AgentMessage = {
  id: number
  role: 'user' | 'assistant'
  content: string
  error: string | null
  created_at: string
}

export type AgentSessionDetail = {
  id: number
  title: string
  chat_mode: 'research' | 'stock' | string
  digital_human_id: number | null
  digital_human_name: string | null
  model_config_id: number | null
  model_display_name: string | null
  messages: AgentMessage[]
}

export type StockStrategy = {
  id: string
  name: string
  description: string
}

export type AgentStreamEvent = {
  type: string
  session_id?: number
  chat_mode?: 'research' | 'stock'
  profile?: DigitalHuman | null
  model?: Partial<ModelConfig> | null
  role?: 'thought' | 'answer'
  tool?: string
  display_name?: string
  message?: string
  content?: string
  step?: number
  stage?: string
  status?: string
  success?: boolean
  duration?: number
}
