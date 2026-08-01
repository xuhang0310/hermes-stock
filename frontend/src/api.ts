import type {
  ArtifactVersion,
  AgentItem,
  DashboardPayload,
  EventItem,
  HealthPayload,
  PositionAgentAddResult,
  PositionAgentPendingAction,
  PositionAgentStreamEvent,
  PositionOcrConfirmResult,
  PositionOcrRecognizeResult,
  PositionOcrRow,
  PositionSymbolValidation,
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
  ScheduleJobPage,
  JobRun,
  PageResult,
  ScheduleStatus,
  AgentSession,
  AgentSessionDetail,
  AgentAttachment,
  AgentToolApproval,
  ApprovalPolicy,
  AgentStreamEvent,
  AgentSkill,
  AgentWorkflowRun,
  AgentWorkflowTemplate,
  ChartResolution,
  StockStrategy,
  WatchlistAgentPendingAction,
  WatchlistAgentStreamEvent,
  WatchlistItem,
  WorkspaceDoc,
  WorkspaceStatus,
  BacktestRun,
  AgentBoardCounts,
  AgentTask,
  AgentTaskDetail,
  AgentTaskEvent,
  AgentBoardAgent,
  TaskBatch,
} from './types'
import { readSseStream } from './utils/sse'

const TOKEN_KEY = 'hermes_stock_token'
const persistentAuthStorage = typeof localStorage === 'undefined' ? null : localStorage
const sessionAuthStorage = typeof sessionStorage === 'undefined' ? null : sessionStorage
let authToken =
  persistentAuthStorage?.getItem(TOKEN_KEY) || sessionAuthStorage?.getItem(TOKEN_KEY) || ''
let authExpiryNotified = false

export function setAuthToken(token: string, persistent = true) {
  authToken = token
  if (token) authExpiryNotified = false
  persistentAuthStorage?.removeItem(TOKEN_KEY)
  sessionAuthStorage?.removeItem(TOKEN_KEY)
  if (token) {
    const storage = persistent ? persistentAuthStorage : sessionAuthStorage
    storage?.setItem(TOKEN_KEY, token)
  }
}

export function getAuthToken() {
  return authToken
}

async function authorizedFetch(input: RequestInfo | URL, init?: RequestInit) {
  const response = await fetch(input, init)
  if (response.status === 401 && authToken) {
    setAuthToken('')
    if (!authExpiryNotified) {
      authExpiryNotified = true
      window.dispatchEvent(new CustomEvent('hermes-auth-expired'))
    }
  }
  return response
}

async function responseErrorMessage(response: Response) {
  const text = await response.text()
  if (!text) return `Request failed: ${response.status}`
  try {
    const payload = JSON.parse(text) as { detail?: unknown }
    if (typeof payload.detail === 'string') return payload.detail
  } catch {
    // Non-JSON errors are returned as plain text by some upstream services.
  }
  return text
}

export class ApiError extends Error {
  status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

type PromptRecommendationResponse = {
  items: PromptRecommendation[]
  fallback_prompts: string[]
  refreshing?: boolean
}

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const isFormData = options?.body instanceof FormData
  const headers: Record<string, string> = isFormData ? {} : { 'Content-Type': 'application/json' }
  if (options?.headers) Object.assign(headers, options.headers as Record<string, string>)
  if (authToken) {
    headers.Authorization = `Bearer ${authToken}`
  }

  const response = await authorizedFetch(url, {
    ...options,
    headers
  })

  if (!response.ok) {
    throw new ApiError(await responseErrorMessage(response), response.status)
  }

  return response.json() as Promise<T>
}

type AgentRunCreated = {
  run_id: string
  session_id: number
  engine: string
  status: string
  created: boolean
}

type AgentRunStatus = AgentRunCreated & {
  source_type?: string
  last_event_seq: number
  attempts: number
  max_attempts: number
  cancel_requested: boolean
  error?: string | null
}

function abortError() {
  return new DOMException('The operation was aborted.', 'AbortError')
}

function waitBeforeReconnect(milliseconds: number, signal?: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    if (signal?.aborted) {
      reject(abortError())
      return
    }
    const onAbort = () => {
      window.clearTimeout(timer)
      reject(abortError())
    }
    const timer = window.setTimeout(() => {
      signal?.removeEventListener('abort', onAbort)
      resolve()
    }, milliseconds)
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

async function cancelAgentRunQuietly(runId: string) {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (authToken) headers.Authorization = `Bearer ${authToken}`
  try {
    await authorizedFetch(`/api/agent/runs/${encodeURIComponent(runId)}/cancel`, { method: 'POST', headers })
  } catch {
    // Cancellation is durable when the API is reachable; a transient network
    // failure is handled by the server-side run timeout and stale-run recovery.
  }
}

async function consumeAgentRunEvents(
  runId: string,
  onEvent: (event: AgentStreamEvent) => void,
  signal?: AbortSignal
) {
  let afterSeq = 0
  let terminal = false
  let reconnects = 0
  while (!terminal) {
    if (signal?.aborted) throw abortError()
    const seqBeforeConnect = afterSeq
    const headers: Record<string, string> = { Accept: 'text/event-stream' }
    if (authToken) headers.Authorization = `Bearer ${authToken}`
    if (afterSeq > 0) headers['Last-Event-ID'] = String(afterSeq)

    try {
      const response = await authorizedFetch(
        `/api/agent/runs/${encodeURIComponent(runId)}/events?after_seq=${afterSeq}`,
        { headers, signal }
      )
      if (!response.ok || !response.body) {
        const detail = await response.text()
        throw new ApiError(detail || `Request failed: ${response.status}`, response.status)
      }
      await readSseStream(response.body, (event: AgentStreamEvent) => {
        if (typeof event.seq === 'number') afterSeq = Math.max(afterSeq, event.seq)
        terminal = event.type === 'done'
        onEvent(event)
      })
      if (terminal) return

      const status = await request<AgentRunStatus>(`/api/agent/runs/${encodeURIComponent(runId)}`, { signal })
      if (['completed', 'failed', 'cancelled'].includes(status.status)) {
        if (afterSeq < status.last_event_seq) {
          reconnects = 0
          continue
        }
        if (status.status === 'failed' && status.error) {
          onEvent({ type: 'error', run_id: runId, content: status.error })
        }
        onEvent({
          type: 'done',
          run_id: runId,
          seq: status.last_event_seq,
          status: status.status,
          content: status.error || undefined
        })
        return
      }
      reconnects = afterSeq > seqBeforeConnect ? 0 : reconnects + 1
    } catch (error) {
      if (signal?.aborted) throw abortError()
      if (error instanceof ApiError && error.status < 500) throw error
      reconnects += 1
    }

    if (reconnects > 8) {
      throw new Error('Agent stream was interrupted repeatedly. The task remains recoverable from its run record.')
    }
    await waitBeforeReconnect(Math.min(5000, 400 * 2 ** Math.min(reconnects, 4)), signal)
  }
}

async function resumeAgentRunStream(
  runId: string,
  onEvent: (event: AgentStreamEvent) => void,
  signal?: AbortSignal
) {
  try {
    await consumeAgentRunEvents(runId, onEvent, signal)
  } catch (error) {
    if (signal?.aborted) await cancelAgentRunQuietly(runId)
    throw error
  }
}

async function durableAgentStreamRequest(
  payload: unknown,
  onEvent: (event: AgentStreamEvent) => void,
  signal?: AbortSignal
) {
  const idempotencyKey = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`
  let createdRun: AgentRunCreated | null = null

  try {
    let createError: unknown = null
    for (let attempt = 0; attempt < 3 && !createdRun; attempt += 1) {
      if (signal?.aborted) throw abortError()
      try {
        createdRun = await request<AgentRunCreated>('/api/agent/runs', {
          method: 'POST',
          headers: { 'Idempotency-Key': idempotencyKey },
          body: JSON.stringify(payload),
          signal
        })
      } catch (error) {
        createError = error
        if (error instanceof ApiError && error.status < 500) throw error
        if (attempt < 2) await waitBeforeReconnect(300 * 2 ** attempt, signal)
      }
    }
    if (!createdRun) throw createError instanceof Error ? createError : new Error('Agent task could not be created.')

    await consumeAgentRunEvents(createdRun.run_id, onEvent, signal)
  } catch (error) {
    if (signal?.aborted && createdRun) await cancelAgentRunQuietly(createdRun.run_id)
    throw error
  }
}

async function streamJsonRequest<T extends { type?: string }>(
  url: string,
  payload: unknown,
  onEvent: (event: T) => void
) {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  headers['Idempotency-Key'] =
    globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`
  if (authToken) {
    headers.Authorization = `Bearer ${authToken}`
  }
  const response = await authorizedFetch(url, {
    method: 'POST',
    headers,
    body: JSON.stringify(payload)
  })
  if (!response.ok || !response.body) {
    const detail = await response.text()
    throw new Error(detail || `Request failed: ${response.status}`)
  }

  await readSseStream(response.body, onEvent)
}

export const api = {
  login: (payload: { username: string; password: string; remember_me: boolean }) =>
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
  dashboard: () =>
    request<DashboardPayload>('/api/dashboard', {
      cache: 'no-store',
      headers: {
        'Cache-Control': 'no-cache'
      }
    }),
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
  correctPosition: (
    positionId: number,
    payload: { symbol: string; name: string; position_quantity: number; cost_price: number }
  ) =>
    request<PositionItem>(`/api/positions/by-id/${positionId}`, {
      method: 'PATCH',
      body: JSON.stringify(payload)
    }),
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
  recognizePositionOcr: (payload: {
    image_base64: string
    mime_type: string
    model_config_id?: number | null
  }) =>
    request<PositionOcrRecognizeResult>('/api/positions/ocr/recognize', {
      method: 'POST',
      body: JSON.stringify(payload)
    }),
  confirmPositionOcr: (payload: {
    rows: PositionOcrRow[]
    account_id: number
    as_of_date?: string | null
    image_sha256: string
    mime_type: string
  }) =>
    request<PositionOcrConfirmResult>('/api/positions/ocr/confirm', {
      method: 'POST',
      body: JSON.stringify(payload)
    }),
  agentAddPositions: (payload: {
    instruction: string
    image_base64?: string
    mime_type?: string
    model_config_id?: number | null
    account_id?: number | null
    history?: Array<{ role: 'user' | 'assistant'; content: string }>
    pending_action?: PositionAgentPendingAction | null
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
      history?: Array<{ role: 'user' | 'assistant'; content: string }>
      pending_action?: PositionAgentPendingAction | null
    },
    onEvent: (event: PositionAgentStreamEvent) => void
  ) => streamJsonRequest<PositionAgentStreamEvent>('/api/positions/agent-add/stream', payload, onEvent),
  validatePositionAgentSymbol: (payload: { symbol: string; name?: string | null }) =>
    request<PositionSymbolValidation>('/api/positions/agent-validate-symbol', {
      method: 'POST',
      body: JSON.stringify(payload)
    }),
  confirmPositionAgentDraft: (payload: {
    rows: Array<Record<string, unknown>>
    account?: Record<string, unknown>
    as_of_date?: string
    account_id?: number | null
  }) =>
    request<{ rows: PositionItem[]; should_save: boolean; summary: string }>('/api/positions/agent-confirm', {
      method: 'POST',
      body: JSON.stringify(payload)
    }),
  discardPositionAgentDraft: () =>
    request<{ cleared: number }>('/api/positions/agent-draft', { method: 'DELETE' }),
  watchlist: () => request<WatchlistItem[]>('/api/watchlist'),
  addWatchlist: (payload: { symbol: string; group_name: string; reason?: string }) =>
    request<WatchlistItem>('/api/watchlist', {
      method: 'POST',
      body: JSON.stringify(payload)
    }),
  deleteWatchlist: (itemId: number) =>
    request<{ deleted: boolean; id: number; symbol: string; group_name: string }>(`/api/watchlist/${itemId}`, {
      method: 'DELETE'
    }),
  streamWatchlistAgent: (
    payload: {
      instruction: string
      model_config_id?: number | null
      history?: Array<{ role: 'user' | 'assistant'; content: string }>
      pending_action?: WatchlistAgentPendingAction | null
    },
    onEvent: (event: WatchlistAgentStreamEvent) => void
  ) => streamJsonRequest<WatchlistAgentStreamEvent>('/api/watchlist/agent/stream', payload, onEvent),
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
  scheduleJobsPaged: (params: { page?: number; page_size?: number; enabled?: boolean; keyword?: string } = {}) => {
    const query = new URLSearchParams()
    if (params.page) query.set('page', String(params.page))
    if (params.page_size) query.set('page_size', String(params.page_size))
    if (params.enabled !== undefined) query.set('enabled', String(params.enabled))
    if (params.keyword) query.set('keyword', params.keyword)
    return request<ScheduleJobPage>(`/api/schedule/jobs/paged?${query.toString()}`)
  },
  saveScheduleJob: (jobKey: string, payload: Record<string, unknown>) =>
    request<ScheduleJob>(`/api/schedule/jobs/${jobKey}`, {
      method: 'PUT',
      body: JSON.stringify(payload)
    }),
  deleteScheduleJob: (jobKey: string) =>
    request<{ deleted: boolean; job_key: string }>(`/api/schedule/jobs/${encodeURIComponent(jobKey)}`, {
      method: 'DELETE'
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
  scheduleRunsPaged: (
    params: { page?: number; page_size?: number; job_key?: string; status?: string } = {}
  ) => {
    const query = new URLSearchParams()
    if (params.page) query.set('page', String(params.page))
    if (params.page_size) query.set('page_size', String(params.page_size))
    if (params.job_key) query.set('job_key', params.job_key)
    if (params.status) query.set('status', params.status)
    return request<PageResult<JobRun>>(`/api/schedule/runs/paged?${query.toString()}`)
  },
  clearScheduleRuns: (params: { job_key?: string; status?: string } = {}) => {
    const query = new URLSearchParams()
    if (params.job_key) query.set('job_key', params.job_key)
    if (params.status) query.set('status', params.status)
    const suffix = query.toString()
    return request<{ deleted: number; skipped_running: number }>(
      `/api/schedule/runs${suffix ? `?${suffix}` : ''}`,
      { method: 'DELETE' }
    )
  },
  scheduleRun: (runId: string) =>
    request<JobRun>(`/api/schedule/runs/${encodeURIComponent(runId)}`),
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
  workspaceStatus: () => request<WorkspaceStatus>('/api/workspace/status'),
  workspaceDocuments: () => request<WorkspaceDoc[]>('/api/workspace/documents'),
  backtests: () => request<{ items: Array<Partial<BacktestRun>> }>('/api/backtests'),
  backtest: (runId: string) =>
    request<BacktestRun>(`/api/backtests/${encodeURIComponent(runId)}`),
  createMacdBacktest: (payload: Record<string, unknown>) =>
    request<BacktestRun>('/api/backtests', {
      method: 'POST',
      body: JSON.stringify(payload)
    }),
  downloadBacktestFile: async (runId: string, kind: 'trades.csv' | 'metrics.json') => {
    const response = await authorizedFetch(`/api/backtests/${encodeURIComponent(runId)}/${kind}`, {
      headers: authToken ? { Authorization: `Bearer ${authToken}` } : {}
    })
    if (!response.ok) throw new Error((await response.text()) || `下载失败: ${response.status}`)
    const blob = await response.blob()
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `${runId}-${kind}`
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
    URL.revokeObjectURL(url)
  },
  artifactVersions: (artifactId: string) =>
    request<ArtifactVersion[]>(`/api/workspace/artifacts/${encodeURIComponent(artifactId)}/versions`),
  restoreArtifactVersion: (artifactId: string, version: number) =>
    request<Record<string, unknown>>(`/api/workspace/artifacts/${encodeURIComponent(artifactId)}/restore`, {
      method: 'POST',
      body: JSON.stringify({ version })
    }),
  ingestWorkspaceDoc: (path: string) =>
    request<{ doc_id: number; chunks: number; failed: number; path: string }>('/api/workspace/documents/ingest', {
      method: 'POST',
      body: JSON.stringify({ path })
    }),
  // 下载端点需鉴权（Bearer），故走带 token 的 fetch → blob → 触发浏览器下载
  downloadWorkspaceDoc: async (path: string, filename: string) => {
    const res = await authorizedFetch(`/api/workspace/documents/download?path=${encodeURIComponent(path)}`, {
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
  uploadAgentAttachment: (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request<AgentAttachment>('/api/agent/attachments', { method: 'POST', body: form })
  },
  resolveAgentApproval: (approvalId: string, decision: 'approved' | 'denied') =>
    request<{ approval_id: string; status: string }>(`/api/agent/approvals/${approvalId}`, {
      method: 'POST',
      body: JSON.stringify({ decision })
    }),
  agentApprovals: (status = 'pending') =>
    request<AgentToolApproval[]>(`/api/agent/approvals?status=${encodeURIComponent(status)}`),
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
    request<PromptRecommendationResponse>(
      `/api/agent/prompt-recommendations?digital_human_id=${digitalHumanId}`
    ),
  refreshPromptRecommendations: (digitalHumanId: number) =>
    request<PromptRecommendationResponse>('/api/agent/prompt-recommendations/refresh', {
      method: 'POST',
      body: JSON.stringify({ digital_human_id: digitalHumanId })
    }),
  dismissPromptRecommendation: (id: number) =>
    request<{ ok: boolean }>(`/api/agent/prompt-recommendations/${id}/dismiss`, { method: 'POST' }),
  stockPromptRecommendations: () =>
    request<PromptRecommendationResponse>('/api/agent/stock/prompt-recommendations'),
  refreshStockPromptRecommendations: () =>
    request<PromptRecommendationResponse>('/api/agent/stock/prompt-recommendations/refresh', {
      method: 'POST'
    }),
  stockStrategies: () => request<{ skills: StockStrategy[]; default_skill_id: string }>('/api/agent/stock/strategies'),
  agentSkills: () => request<AgentSkill[]>('/api/agent/skills/core'),
  agentSkill: (name: string) =>
    request<AgentSkill>(`/api/agent/skills/core/${encodeURIComponent(name)}`),
  swarmTeams: () => request<import('./types').SwarmTeam[]>('/api/agent/swarm-teams'),
  agentWorkflowTemplates: () =>
    request<AgentWorkflowTemplate[]>('/api/agent/workflows/templates'),
  agentWorkflow: (runId: string) =>
    request<AgentWorkflowRun>(`/api/agent/runs/${encodeURIComponent(runId)}/workflow`),
  retryAgentWorkflowNode: (workflowRunId: string, nodeKey: string) =>
    request<AgentWorkflowRun>(
      `/api/agent/workflows/${encodeURIComponent(workflowRunId)}/nodes/${encodeURIComponent(nodeKey)}/retry`,
      { method: 'POST' }
    ),
  activeAgentRuns: (params: { chat_mode?: 'research' | 'stock' | 'knowledge'; session_id?: number | null } = {}) => {
    const query = new URLSearchParams()
    if (params.chat_mode) query.set('chat_mode', params.chat_mode)
    if (params.session_id) query.set('session_id', String(params.session_id))
    const suffix = query.toString()
    return request<AgentRunStatus[]>(`/api/agent/runs/active${suffix ? `?${suffix}` : ''}`)
  },
  resumeAgentRun: (
    runId: string,
    onEvent: (event: AgentStreamEvent) => void,
    signal?: AbortSignal
  ) => resumeAgentRunStream(runId, onEvent, signal),
  streamAgentChat: (
    payload: {
      question: string
      history?: Array<{ role: 'user' | 'assistant'; content: string }>
      session_id?: number | null
      digital_human_id?: number | null
      model_config_id?: number | null
      chat_mode?: 'research' | 'stock' | 'knowledge'
      skills?: string[]
      selected_skill_names?: string[]
      attachments?: Array<{ path: string }>
      approval_policy?: ApprovalPolicy
      allow_code_execution?: boolean
    },
    onEvent: (event: AgentStreamEvent) => void,
    signal?: AbortSignal
  ) => durableAgentStreamRequest(payload, onEvent, signal),
  streamStockChat: (
    payload: {
      question: string
      history?: Array<{ role: 'user' | 'assistant'; content: string }>
      session_id?: number | null
      model_config_id?: number | null
      skills?: string[]
      selected_skill_names?: string[]
      attachments?: Array<{ path: string }>
      approval_policy?: ApprovalPolicy
      allow_code_execution?: boolean
    },
    onEvent: (event: AgentStreamEvent) => void,
    signal?: AbortSignal
  ) => durableAgentStreamRequest({ ...payload, chat_mode: 'stock' }, onEvent, signal),
  // Agent 任务看板
  agentBoardSummary: (params?: { date_from?: string; date_to?: string }) => {
    const query = new URLSearchParams()
    if (params?.date_from) query.set('date_from', params.date_from)
    if (params?.date_to) query.set('date_to', params.date_to)
    const suffix = query.toString()
    return request<{ counts: AgentBoardCounts }>(
      `/api/agent-board/summary${suffix ? `?${suffix}` : ''}`
    )
  },
  agentBoardTasks: (params: {
    status?: string
    agent_code?: string
    source_type?: string
    scheduled_job_key?: string
    date_from?: string
    date_to?: string
    page?: number
    page_size?: number
  } = {}) => {
    const query = new URLSearchParams()
    if (params.status) query.set('status', params.status)
    if (params.agent_code) query.set('agent_code', params.agent_code)
    if (params.source_type) query.set('source_type', params.source_type)
    if (params.scheduled_job_key) query.set('scheduled_job_key', params.scheduled_job_key)
    if (params.date_from) query.set('date_from', params.date_from)
    if (params.date_to) query.set('date_to', params.date_to)
    if (params.page) query.set('page', String(params.page))
    if (params.page_size) query.set('page_size', String(params.page_size))
    const suffix = query.toString()
    return request<{ items: AgentTask[]; total: number; page: number; page_size: number }>(
      `/api/agent-board/tasks${suffix ? `?${suffix}` : ''}`
    )
  },
  agentBoardTaskDetail: (runId: string) =>
    request<AgentTaskDetail>(`/api/agent-board/tasks/${encodeURIComponent(runId)}`),
  agentBoardTaskEvents: (runId: string, limit = 50) =>
    request<{ events: AgentTaskEvent[] }>(
      `/api/agent-board/tasks/${encodeURIComponent(runId)}/events?limit=${limit}`
    ),
  agentBoardRetry: (runId: string) =>
    request<{ status: string; attempts: number; message: string }>(
      `/api/agent-board/tasks/${encodeURIComponent(runId)}/retry`,
      { method: 'POST' }
    ),
  agentBoardCancel: (runId: string) =>
    request<{ status: string; message: string }>(
      `/api/agent-board/tasks/${encodeURIComponent(runId)}/cancel`,
      { method: 'POST' }
    ),
  agentBoardSetPriority: (runId: string, priority: number) =>
    request<{ priority: number; old_priority: number; message: string }>(
      `/api/agent-board/tasks/${encodeURIComponent(runId)}/priority?priority=${priority}`,
      { method: 'POST' }
    ),
  agentBoardBatchDetail: (batchId: string) =>
    request<TaskBatch>(`/api/agent-board/batches/${encodeURIComponent(batchId)}`),
  agentBoardAgents: () =>
    request<{ agents: AgentBoardAgent[] }>('/api/agent-board/agents'),
}
