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
  PromptRecommendation,
  DigitalHumanCapabilities,
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
  WatchlistItem
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
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(options?.headers as Record<string, string> | undefined)
  }
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
  positionBrokers: () => request<BrokerPreset[]>('/api/positions/brokers'),
  positionAccounts: () => request<PositionAccount[]>('/api/positions/accounts'),
  positionsSummary: (accountId?: number | null) =>
    request<PositionSummary>(`/api/positions/summary${accountId ? `?account_id=${accountId}` : ''}`),
  createPositionAccount: (payload: Record<string, unknown>) =>
    request<PositionAccount>('/api/positions/accounts', { method: 'POST', body: JSON.stringify(payload) }),
  updatePositionAccount: (id: number, payload: Record<string, unknown>) =>
    request<PositionAccount>(`/api/positions/accounts/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  updateAccountFunds: (id: number, total_asset: number | null) =>
    request<PositionAccount>(`/api/positions/accounts/${id}/funds`, {
      method: 'PUT',
      body: JSON.stringify({ total_asset })
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
  agentSessions: (chatMode?: 'research' | 'stock', digitalHumanId?: number | null) => {
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
      chat_mode?: 'research' | 'stock'
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
