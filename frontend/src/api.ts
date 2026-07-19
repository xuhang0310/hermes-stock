import type {
  AgentItem,
  DashboardPayload,
  EventItem,
  HealthPayload,
  PositionAgentAddResult,
  PositionAgentStreamEvent,
  PositionChartPayload,
  PositionItem,
  PositionAccount,
  BrokerPreset,
  PositionSummary,
  PositionTrade,
  PromptRecommendation,
  DigitalHumanCapabilities,
  KnowledgeStats,
  KnowledgeDocumentPage,
  KnowledgeChunk,
  KnowledgeSearchHit,
  ReportItem,
  RiskCheck,
  SystemLog,
  UserPayload,
  LoginPayload,
  ModelPlatform,
  ModelConfig,
  DigitalHuman,
  DataSourceConfig,
  NotifyChannel,
  NotifyChannelTestResult,
  FeishuBotBinding,
  FeishuBotTestResult,
  ScheduleJob,
  JobRun,
  ScheduleStatus,
  AgentSession,
  AgentSessionDetail,
  AgentStreamEvent,
  ChartResolution,
  StockStrategy,
  WatchlistItem,
  WorkspaceDoc
} from './types'

const TOKEN_KEY = 'hermes_stock_token'
let authToken = localStorage.getItem(TOKEN_KEY) || ''

export function setAuthToken(token: string) {
  authToken = token
  if (token) {
    localStorage.setItem(TOKEN_KEY, token)
  } else {
    localStorage.removeItem(TOKEN_KEY)
  }
}

export function getAuthToken() {
  return authToken
}

export class ApiError extends Error {
  status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const isFormData = options?.body instanceof FormData
  const headers: Record<string, string> = isFormData ? {} : { 'Content-Type': 'application/json' }
  if (options?.headers) Object.assign(headers, options.headers as Record<string, string>)
  if (authToken) {
    headers.Authorization = `Bearer ${authToken}`
  }

  const response = await fetch(url, {
    ...options,
    headers
  })

  if (!response.ok) {
    const detail = await response.text()
    throw new ApiError(detail || `Request failed: ${response.status}`, response.status)
  }

  return response.json() as Promise<T>
}

async function streamRequest(
  url: string,
  payload: unknown,
  onEvent: (event: AgentStreamEvent) => void,
  signal?: AbortSignal
) {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (authToken) {
    headers.Authorization = `Bearer ${authToken}`
  }
  const response = await fetch(url, {
    method: 'POST',
    headers,
    body: JSON.stringify(payload),
    signal
  })
  if (!response.ok || !response.body) {
    const detail = await response.text()
    throw new Error(detail || `Request failed: ${response.status}`)
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const events = buffer.split('\n\n')
    buffer = events.pop() || ''
    for (const raw of events) {
      const dataLine = raw.split('\n').find((line) => line.startsWith('data: '))
      if (!dataLine) continue
      const event = JSON.parse(dataLine.slice(6)) as AgentStreamEvent
      onEvent(event)
      if (event.type === 'done') {
        await reader.cancel()
        return
      }
    }
  }
}

async function streamJsonRequest<T>(
  url: string,
  payload: unknown,
  onEvent: (event: T) => void
) {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (authToken) {
    headers.Authorization = `Bearer ${authToken}`
  }
  const response = await fetch(url, {
    method: 'POST',
    headers,
    body: JSON.stringify(payload)
  })
  if (!response.ok || !response.body) {
    const detail = await response.text()
    throw new Error(detail || `Request failed: ${response.status}`)
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const events = buffer.split('\n\n')
    buffer = events.pop() || ''
    for (const raw of events) {
      const dataLine = raw.split('\n').find((line) => line.startsWith('data: '))
      if (!dataLine) continue
      const event = JSON.parse(dataLine.slice(6)) as T
      onEvent(event)
      if ((event as { type?: string }).type === 'done') {
        await reader.cancel()
        return
      }
    }
  }
}

export const api = {
  login: (payload: { username: string; password: string }) =>
    request<LoginPayload>('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify(payload)
    }),
  me: () => request<UserPayload>('/api/auth/me'),
  changePassword: (payload: { old_password: string; new_password: string }) =>
    request<{ ok: boolean }>('/api/auth/change-password', {
      method: 'POST',
      body: JSON.stringify(payload)
    }),
  health: () => request<HealthPayload>('/api/health'),
  dashboard: () => request<DashboardPayload>('/api/dashboard'),
  events: () => request<EventItem[]>('/api/events'),
  positions: (accountId?: number | null) =>
    request<PositionItem[]>(`/api/positions${accountId ? `?account_id=${accountId}` : ''}`),
  addPosition: (payload: {
    symbol: string
    name?: string
    position_quantity: number
    cost_price?: number
    execution_price?: number
    action: 'buy' | 'sell' | 'add' | 'reduce' | 'close' | 'replace'
    account_id?: number | null
  }) =>
    request<PositionItem>('/api/positions', { method: 'POST', body: JSON.stringify(payload) }),
  deletePosition: (symbol: string, accountId?: number | null) => {
    const q = accountId ? `?account_id=${accountId}` : ''
    return request<{ symbol: string; deleted_positions: number; deleted_snapshots: number }>(
      `/api/positions/${encodeURIComponent(symbol)}${q}`,
      { method: 'DELETE' }
    )
  },
  positionBrokers: () => request<BrokerPreset[]>('/api/positions/brokers'),
  positionAccounts: () => request<PositionAccount[]>('/api/positions/accounts'),
  positionsSummary: (accountId?: number | null) =>
    request<PositionSummary>(`/api/positions/summary${accountId ? `?account_id=${accountId}` : ''}`),
  positionTrades: (params: { symbol?: string | null; account_id?: number | null; limit?: number } = {}) => {
    const qs = new URLSearchParams()
    if (params.symbol) qs.set('symbol', params.symbol)
    if (params.account_id) qs.set('account_id', String(params.account_id))
    if (params.limit) qs.set('limit', String(params.limit))
    const suffix = qs.toString()
    return request<PositionTrade[]>(`/api/positions/trades${suffix ? `?${suffix}` : ''}`)
  },
  deletePositionTrade: (tradeId: number) =>
    request<{ deleted: boolean; id: number }>(`/api/positions/trades/${tradeId}`, { method: 'DELETE' }),
  createPositionAccount: (payload: Record<string, unknown>) =>
    request<PositionAccount>('/api/positions/accounts', { method: 'POST', body: JSON.stringify(payload) }),
  updatePositionAccount: (id: number, payload: Record<string, unknown>) =>
    request<PositionAccount>(`/api/positions/accounts/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  updateAccountFunds: (id: number, available_cash: number | null) =>
    request<PositionAccount>(`/api/positions/accounts/${id}/funds`, {
      method: 'PUT',
      body: JSON.stringify({ available_cash })
    }),
  positionChart: (symbol: string, options?: { limit?: number; refresh?: boolean; resolution?: ChartResolution }) => {
    const params = new URLSearchParams()
    params.set('limit', String(options?.limit ?? 240))
    if (options?.refresh) params.set('refresh', 'true')
    if (options?.resolution) params.set('resolution', options.resolution)
    return request<PositionChartPayload>(`/api/chart/positions/${encodeURIComponent(symbol)}?${params.toString()}`)
  },
  agentAddPositions: (payload: {
    instruction: string
    image_base64?: string
    mime_type?: string
    model_config_id?: number | null
  }) =>
    request<PositionAgentAddResult>('/api/positions/agent-add', {
      method: 'POST',
      body: JSON.stringify(payload)
    }),
  streamPositionAgentAdd: (
    payload: {
      instruction: string
      image_base64?: string
      mime_type?: string
      model_config_id?: number | null
      account_id?: number | null
    },
    onEvent: (event: PositionAgentStreamEvent) => void
  ) => streamJsonRequest<PositionAgentStreamEvent>('/api/positions/agent-add/stream', payload, onEvent),
  watchlist: () => request<WatchlistItem[]>('/api/watchlist'),
  addWatchlist: (payload: { symbol: string; group_name: string; reason?: string }) =>
    request<{ symbol: string; group_name: string; reason: string | null }>('/api/watchlist', {
      method: 'POST',
      body: JSON.stringify(payload)
    }),
  agents: () => request<AgentItem[]>('/api/agents'),
  reports: () => request<ReportItem[]>('/api/reports'),
  riskChecks: () => request<RiskCheck[]>('/api/risk-checks'),
  logs: (params?: { module?: string; level?: string; limit?: number }) => {
    const q = new URLSearchParams()
    if (params?.module) q.set('module', params.module)
    if (params?.level) q.set('level', params.level)
    if (params?.limit) q.set('limit', String(params.limit))
    const suffix = q.toString()
    return request<SystemLog[]>(`/api/logs${suffix ? `?${suffix}` : ''}`)
  },
  logModules: () => request<string[]>('/api/logs/modules'),
  seedDemo: () => request<{ ok: boolean; message: string }>('/api/dev/seed-demo', { method: 'POST' }),
  modelPlatforms: () => request<ModelPlatform[]>('/api/model-platforms'),
  saveModelPlatform: (payload: Record<string, unknown>, id?: number) =>
    request<ModelPlatform>(id ? `/api/model-platforms/${id}` : '/api/model-platforms', {
      method: id ? 'PUT' : 'POST',
      body: JSON.stringify(payload)
    }),
  testModelPlatform: (payload: Record<string, unknown>, id?: number) =>
    request<Record<string, unknown>>(id ? `/api/model-platforms/${id}/test` : '/api/model-platforms/test', {
      method: 'POST',
      body: JSON.stringify(payload)
    }),
  modelConfigs: () => request<ModelConfig[]>('/api/model-configs'),
  saveModelConfig: (payload: Record<string, unknown>, id?: number) =>
    request<ModelConfig>(id ? `/api/model-configs/${id}` : '/api/model-configs', {
      method: id ? 'PUT' : 'POST',
      body: JSON.stringify(payload)
    }),
  testModelConfig: (payload: Record<string, unknown>, id?: number) =>
    request<Record<string, unknown>>(id ? `/api/model-configs/${id}/test` : '/api/model-configs/test', {
      method: 'POST',
      body: JSON.stringify(payload)
    }),
  activateModelConfig: (id: number) =>
    request<ModelConfig>(`/api/model-configs/${id}/activate`, { method: 'POST' }),
  notifyChannels: () => request<NotifyChannel[]>('/api/notify-channels'),
  saveNotifyChannel: (channelKey: string, payload: Record<string, unknown>) =>
    request<NotifyChannel>(`/api/notify-channels/${channelKey}`, {
      method: 'PUT',
      body: JSON.stringify(payload)
    }),
  testNotifyChannel: (payload: { channel_key?: string | null; webhook_url?: string | null; secret?: string | null }) =>
    request<NotifyChannelTestResult>('/api/notify-channels/test', {
      method: 'POST',
      body: JSON.stringify(payload)
    }),
  feishuBots: () => request<FeishuBotBinding[]>('/api/feishu-bots'),
  saveFeishuBot: (profileId: number, payload: { app_id?: string | null; app_secret?: string | null; enabled: boolean }) =>
    request<FeishuBotBinding>(`/api/feishu-bots/${profileId}`, {
      method: 'PUT',
      body: JSON.stringify(payload)
    }),
  testFeishuBot: (profileId: number, payload: { app_id?: string | null; app_secret?: string | null }) =>
    request<FeishuBotTestResult>(`/api/feishu-bots/${profileId}/test`, {
      method: 'POST',
      body: JSON.stringify(payload)
    }),
  toggleFeishuBot: (profileId: number) =>
    request<FeishuBotBinding>(`/api/feishu-bots/${profileId}/toggle`, { method: 'POST' }),
  unbindFeishuBot: (profileId: number) =>
    request<{ ok: boolean }>(`/api/feishu-bots/${profileId}`, { method: 'DELETE' }),
  scheduleJobs: () => request<ScheduleJob[]>('/api/schedule/jobs'),
  saveScheduleJob: (jobKey: string, payload: Record<string, unknown>) =>
    request<ScheduleJob>(`/api/schedule/jobs/${jobKey}`, {
      method: 'PUT',
      body: JSON.stringify(payload)
    }),
  toggleScheduleJob: (jobKey: string) =>
    request<ScheduleJob>(`/api/schedule/jobs/${jobKey}/toggle`, { method: 'POST' }),
  runScheduleJob: (jobKey: string) =>
    request<{ run_id: string; job_key: string }>(`/api/schedule/jobs/${jobKey}/run`, { method: 'POST' }),
  scheduleRuns: (jobKey?: string) => {
    const params = new URLSearchParams()
    if (jobKey) params.set('job_key', jobKey)
    const suffix = params.toString()
    return request<JobRun[]>(`/api/schedule/runs${suffix ? `?${suffix}` : ''}`)
  },
  scheduleStatus: () => request<ScheduleStatus>('/api/schedule/status'),
  dataSources: () => request<DataSourceConfig[]>('/api/data-sources'),
  saveDataSource: (provider: string, payload: Record<string, unknown>) =>
    request<DataSourceConfig>(`/api/data-sources/${provider}`, {
      method: 'PUT',
      body: JSON.stringify(payload)
    }),
  digitalHumans: (scope?: 'admin') =>
    request<DigitalHuman[]>(`/api/digital-humans${scope === 'admin' ? '?scope=admin' : ''}`),
  saveDigitalHuman: (payload: Record<string, unknown>, id?: number) =>
    request<DigitalHuman>(id ? `/api/digital-humans/${id}` : '/api/digital-humans', {
      method: id ? 'PUT' : 'POST',
      body: JSON.stringify(payload)
    }),
  digitalHumanCapabilities: (id: number) =>
    request<DigitalHumanCapabilities>(`/api/digital-humans/${id}/capabilities`),
  workspaceDocuments: () => request<WorkspaceDoc[]>('/api/workspace/documents'),
  ingestWorkspaceDoc: (path: string) =>
    request<{ doc_id: number; chunks: number; failed: number; path: string }>('/api/workspace/documents/ingest', {
      method: 'POST',
      body: JSON.stringify({ path })
    }),
  // 下载端点需鉴权（Bearer），故走带 token 的 fetch → blob → 触发浏览器下载
  downloadWorkspaceDoc: async (path: string, filename: string) => {
    const res = await fetch(`/api/workspace/documents/download?path=${encodeURIComponent(path)}`, {
      headers: authToken ? { Authorization: `Bearer ${authToken}` } : {}
    })
    if (!res.ok) throw new Error((await res.text()) || `下载失败: ${res.status}`)
    const blob = await res.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  },
  knowledgeStats: () => request<KnowledgeStats>('/api/knowledge/stats'),
  knowledgeDocuments: (params: { doc_type?: string; trust_level?: string; keyword?: string; page?: number; page_size?: number } = {}) => {
    const qs = new URLSearchParams()
    if (params.doc_type) qs.set('doc_type', params.doc_type)
    if (params.trust_level) qs.set('trust_level', params.trust_level)
    if (params.keyword) qs.set('keyword', params.keyword)
    if (params.page) qs.set('page', String(params.page))
    if (params.page_size) qs.set('page_size', String(params.page_size))
    const suffix = qs.toString()
    return request<KnowledgeDocumentPage>(`/api/knowledge/documents${suffix ? `?${suffix}` : ''}`)
  },
  knowledgeChunks: (docId: number) => request<KnowledgeChunk[]>(`/api/knowledge/documents/${docId}/chunks`),
  knowledgeDisable: (docId: number) =>
    request<{ ok: boolean; disabled_chunks: number }>(`/api/knowledge/documents/${docId}/disable`, { method: 'POST' }),
  knowledgeReindex: (docId?: number) =>
    request<{ started: boolean; scope: number | string }>('/api/knowledge/reindex', {
      method: 'POST',
      body: JSON.stringify(docId ? { doc_id: docId } : {})
    }),
  knowledgeSearch: (query: string, topK?: number, symbol?: string) =>
    request<{ query: string; hits: KnowledgeSearchHit[] }>('/api/knowledge/search', {
      method: 'POST',
      body: JSON.stringify({ query, top_k: topK ?? 0, symbol: symbol ?? null })
    }),
  knowledgeUpload: (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request<{ upload_id: number; doc_id: number }>('/api/knowledge/upload', {
      method: 'POST',
      body: form
    })
  },
  knowledgeUploadStatus: (id: number) => request<{ id: number; filename: string; status: string; chunk_count: number; error: string | null }>(`/api/knowledge/uploads/${id}`),
  agentSessions: (chatMode?: 'research' | 'stock' | 'knowledge', digitalHumanId?: number | null) => {
    const params = new URLSearchParams()
    if (chatMode) params.set('chat_mode', chatMode)
    if (digitalHumanId) params.set('digital_human_id', String(digitalHumanId))
    const suffix = params.toString()
    return request<AgentSession[]>(`/api/agent/sessions${suffix ? `?${suffix}` : ''}`)
  },
  agentSession: (id: number) => request<AgentSessionDetail>(`/api/agent/sessions/${id}`),
  agentDeleteSession: (id: number) =>
    request<{ ok: boolean }>(`/api/agent/sessions/${id}`, { method: 'DELETE' }),
  promptRecommendations: (digitalHumanId: number) =>
    request<{ items: PromptRecommendation[]; fallback_prompts: string[] }>(
      `/api/agent/prompt-recommendations?digital_human_id=${digitalHumanId}`
    ),
  refreshPromptRecommendations: (digitalHumanId: number) =>
    request<{ items: PromptRecommendation[]; fallback_prompts: string[] }>('/api/agent/prompt-recommendations/refresh', {
      method: 'POST',
      body: JSON.stringify({ digital_human_id: digitalHumanId })
    }),
  dismissPromptRecommendation: (id: number) =>
    request<{ ok: boolean }>(`/api/agent/prompt-recommendations/${id}/dismiss`, { method: 'POST' }),
  stockPromptRecommendations: () =>
    request<{ items: PromptRecommendation[]; fallback_prompts: string[] }>('/api/agent/stock/prompt-recommendations'),
  refreshStockPromptRecommendations: () =>
    request<{ items: PromptRecommendation[]; fallback_prompts: string[] }>('/api/agent/stock/prompt-recommendations/refresh', {
      method: 'POST'
    }),
  stockStrategies: () => request<{ skills: StockStrategy[]; default_skill_id: string }>('/api/agent/stock/strategies'),
  streamAgentChat: (
    payload: {
      question: string
      history?: Array<{ role: 'user' | 'assistant'; content: string }>
      session_id?: number | null
      digital_human_id?: number | null
      model_config_id?: number | null
      chat_mode?: 'research' | 'stock' | 'knowledge'
      skills?: string[]
    },
    onEvent: (event: AgentStreamEvent) => void,
    signal?: AbortSignal
  ) => streamRequest('/api/agent/chat', payload, onEvent, signal),
  streamStockChat: (
    payload: {
      question: string
      history?: Array<{ role: 'user' | 'assistant'; content: string }>
      session_id?: number | null
      model_config_id?: number | null
      skills?: string[]
    },
    onEvent: (event: AgentStreamEvent) => void,
    signal?: AbortSignal
  ) => streamRequest('/api/agent/chat', { ...payload, chat_mode: 'stock' }, onEvent, signal)
}
