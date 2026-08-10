<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { api } from '../api'
import type { JobRun, ScheduleJob, ScheduleStatus } from '../types'
import { beijingFromDb } from '../utils/datetime'

type MessageTone = 'success' | 'error' | 'info'
type RhythmViewMode = 'settings' | 'history'

const jobs = ref<ScheduleJob[]>([])
const jobOptions = ref<ScheduleJob[]>([])
const runs = ref<JobRun[]>([])
const status = ref<ScheduleStatus | null>(null)
const activeView = ref<RhythmViewMode>('settings')
const editingKey = ref('')
const runsJobKey = ref('')
const runsStatus = ref('')
const editorOpen = ref(false)
const selectedRun = ref<JobRun | null>(null)
const runDetailLoading = ref(false)
const runDetailError = ref('')
const saving = ref(false)
const actionKey = ref('')
const message = ref('')
const messageTone = ref<MessageTone>('info')
const jobPage = ref(1)
const jobPageSize = 8
const jobTotal = ref(0)
const enabledJobTotal = ref(0)
const jobPages = ref(0)
const runPage = ref(1)
const runPageSize = 12
const runTotal = ref(0)
const runPages = ref(0)
let timer: number | undefined
let runDetailRequest = 0

const DIGITAL_HUMANS: Array<{ code: string; label: string }> = [
  { code: 'stock-pm', label: '总调度 PM' },
  { code: 'intelligence-officer', label: '情报官' },
  { code: 'market-sentinel', label: '盘中哨兵' },
  { code: 'research-analyst', label: '个股研究员' },
  { code: 'quant-analyst', label: '量化分析师' },
  { code: 'macro-strategist', label: '宏观策略师' },
  { code: 'short-term-hunter', label: '短线猎手' },
  { code: 'risk-officer', label: '风控复盘官' }
]

const CHANNELS: Array<{ key: string; label: string }> = [
  { key: '', label: '不推送' },
  { key: 'feishu-daily', label: '飞书任务群' }
]

const WEEKDAYS = [
  { value: '1', label: '周一' },
  { value: '2', label: '周二' },
  { value: '3', label: '周三' },
  { value: '4', label: '周四' },
  { value: '5', label: '周五' },
  { value: '6', label: '周六' },
  { value: '7', label: '周日' }
]

const form = reactive({
  name: '',
  description: '',
  schedule_type: 'daily',
  run_time: '',
  run_once_at: '',
  weekday_mask: '',
  interval_minutes: null as number | null,
  window_start: '',
  window_end: '',
  trading_day_only: true,
  digital_human_code: '',
  prompt_template: '',
  push_channel_key: '',
  enabled: true,
  allow_code_execution: false,
  sort_order: 0
})

const editingJob = computed(() => jobs.value.find((item) => item.job_key === editingKey.value) || null)
const isCreating = computed(() => editorOpen.value && !editingKey.value)
const todayExecutions = computed(() => {
  if (!status.value) return 0
  return status.value.today.success + status.value.today.failed + status.value.today.running
})
const todaySuccessRate = computed(() => {
  if (!status.value) return '-'
  const completed = status.value.today.success + status.value.today.failed
  return completed ? `${Math.round((status.value.today.success / completed) * 100)}%` : '-'
})
const schedulerState = computed(() => {
  if (!status.value) return '正在连接'
  if (!status.value.scheduler_enabled) return '已停用'
  return status.value.scheduler_alive ? '运行正常' : '心跳异常'
})
const notifyState = computed(() => {
  const channel = status.value?.notify_channel
  return channel?.configured && channel.enabled ? '已启用' : '未配置'
})
const historyJob = computed(() => jobOptions.value.find((item) => item.job_key === runsJobKey.value) || null)
const jobPageText = computed(() => `${jobTotal.value ? jobPage.value : 0} / ${jobPages.value || 0}`)
const runPageText = computed(() => `${runTotal.value ? runPage.value : 0} / ${runPages.value || 0}`)

watch([editorOpen, selectedRun], ([isEditorOpen, run]) => {
  document.body.classList.toggle('rhythm-modal-open', isEditorOpen || Boolean(run))
})

function setMessage(text: string, tone: MessageTone = 'info') {
  message.value = text
  messageTone.value = tone
}

function humanLabel(code: string | null) {
  if (!code) return '规则任务'
  return DIGITAL_HUMANS.find((item) => item.code === code)?.label || code
}

function channelLabel(job: ScheduleJob) {
  if (!job.push_channel_key) return '不推送'
  if (job.job_kind === 'health_check') return '飞书任务群，仅异常'
  return CHANNELS.find((item) => item.key === job.push_channel_key)?.label || job.push_channel_key
}

function weekdayValues(mask: string | null | undefined) {
  const values = String(mask || '')
    .replace(/[\[\]\s]/g, '')
    .split(',')
    .filter((value) => WEEKDAYS.some((day) => day.value === value))
  return Array.from(new Set(values)).sort((a, b) => Number(a) - Number(b))
}

function weekdayText(mask: string | null | undefined) {
  const values = weekdayValues(mask)
  if (!values.length || values.join(',') === '1,2,3,4,5,6,7') return '每天'
  if (values.join(',') === '1,2,3,4,5') return '工作日'
  return values.map((value) => WEEKDAYS.find((day) => day.value === value)?.label || value).join('、')
}

function scheduleText(job: ScheduleJob) {
  if (job.schedule_type === 'once') return `一次 · ${beijingFromDb(job.run_once_at)}`
  if (job.schedule_type === 'interval') {
    return `每 ${job.interval_minutes ?? '-'} 分钟 · ${job.window_start || '--:--'} 至 ${job.window_end || '--:--'}`
  }
  if (job.schedule_type === 'weekly') return `${weekdayText(job.weekday_mask)} · ${job.run_time || '--:--'}`
  if (job.schedule_type === 'monthly_last') return `每月最后一个交易日 · ${job.run_time || '--:--'}`
  return `${weekdayText(job.weekday_mask)} · ${job.run_time || '--:--'}`
}

function jobMark(job: ScheduleJob) {
  if (job.schedule_type === 'once') return '1x'
  if (job.schedule_type === 'interval') return `${job.interval_minutes || '-'}m`
  return (job.run_time || '--:--').slice(0, 5)
}

function jobKindLabel(kind: string) {
  if (kind === 'agent_task') return 'Agent 任务'
  if (kind === 'agent_report') return 'Agent 报告'
  if (kind === 'health_check') return '系统检查'
  if (kind === 'rule_scan') return '规则扫描'
  if (kind === 'l1_digest') return '事件摘要'
  return kind || '定时任务'
}

function jobDescription(job: ScheduleJob) {
  return job.description || `${jobKindLabel(job.job_kind)}，按设定时间自动执行。`
}

function statusText(value: string | null) {
  if (value === 'success') return '成功'
  if (value === 'failed') return '失败'
  if (value === 'running') return '运行中'
  if (value === 'skipped') return '已跳过'
  return value || '未运行'
}

function triggerText(value: string) {
  if (value === 'schedule') return '自动调度'
  if (value === 'manual') return '手动执行'
  if (value === 'retry') return '自动重试'
  return value || '-'
}

function durationText(value: number | null) {
  if (value === null || value === undefined) return '-'
  if (value < 1000) return `${value} ms`
  if (value < 60_000) return `${(value / 1000).toFixed(value < 10_000 ? 1 : 0)} 秒`
  const minutes = Math.floor(value / 60_000)
  const seconds = Math.round((value % 60_000) / 1000)
  return seconds ? `${minutes} 分 ${seconds} 秒` : `${minutes} 分钟`
}

function runPreview(run: JobRun) {
  const text = (run.error || run.output_preview || '本次运行没有输出内容').replace(/\s+/g, ' ').trim()
  return text.length > 120 ? `${text.slice(0, 120)}…` : text
}

function runDetail(run: JobRun) {
  return run.error || run.output_content || run.output_preview || '本次运行没有输出内容。'
}

async function openRunDetail(run: JobRun) {
  const requestId = ++runDetailRequest
  selectedRun.value = run
  runDetailLoading.value = true
  runDetailError.value = ''
  try {
    const detail = await api.scheduleRun(run.run_id)
    if (requestId === runDetailRequest) selectedRun.value = detail
  } catch (error) {
    if (requestId === runDetailRequest) {
      runDetailError.value = error instanceof Error ? error.message : '完整结果加载失败'
    }
  } finally {
    if (requestId === runDetailRequest) runDetailLoading.value = false
  }
}

function closeRunDetail() {
  runDetailRequest += 1
  selectedRun.value = null
  runDetailLoading.value = false
  runDetailError.value = ''
}

function weekdaySelected(value: string) {
  return weekdayValues(form.weekday_mask).includes(value)
}

function toggleWeekday(value: string) {
  const selected = new Set(weekdayValues(form.weekday_mask))
  if (selected.has(value)) selected.delete(value)
  else selected.add(value)
  form.weekday_mask = Array.from(selected).sort((a, b) => Number(a) - Number(b)).join(',')
}

function isActionBusy(action: string, job: ScheduleJob) {
  return actionKey.value === `${action}:${job.job_key}`
}

async function loadStatus() {
  try {
    status.value = await api.scheduleStatus()
  } catch {
    /* 状态刷新失败不打断页面 */
  }
}

async function loadJobs() {
  try {
    const result = await api.scheduleJobsPaged({ page: jobPage.value, page_size: jobPageSize })
    jobs.value = result.items
    jobTotal.value = result.total
    enabledJobTotal.value = result.enabled_total
    jobPages.value = result.pages
    if (result.pages && jobPage.value > result.pages) {
      jobPage.value = result.pages
      await loadJobs()
    }
  } catch (error) {
    setMessage(`加载任务失败：${error instanceof Error ? error.message : String(error)}`, 'error')
  }
}

async function loadJobOptions() {
  try {
    jobOptions.value = await api.scheduleJobs()
  } catch {
    jobOptions.value = [...jobs.value]
  }
}

async function loadRuns() {
  try {
    const result = await api.scheduleRunsPaged({
      page: runPage.value,
      page_size: runPageSize,
      job_key: runsJobKey.value || undefined,
      status: runsStatus.value || undefined
    })
    runs.value = result.items
    runTotal.value = result.total
    runPages.value = result.pages
    if (result.pages && runPage.value > result.pages) {
      runPage.value = result.pages
      await loadRuns()
    }
  } catch (error) {
    setMessage(`加载历史失败：${error instanceof Error ? error.message : String(error)}`, 'error')
  }
}

async function setJobPage(page: number) {
  if (page < 1 || page > jobPages.value || page === jobPage.value) return
  jobPage.value = page
  await loadJobs()
}

async function setRunPage(page: number) {
  if (page < 1 || page > runPages.value || page === runPage.value) return
  runPage.value = page
  await loadRuns()
}

async function applyHistoryFilters() {
  runPage.value = 1
  await loadRuns()
}

async function clearHistory() {
  if (actionKey.value || !runTotal.value) return
  const scope = [
    historyJob.value ? `任务“${historyJob.value.name}”` : '全部任务',
    runsStatus.value ? `状态“${statusText(runsStatus.value)}”` : ''
  ].filter(Boolean).join('、')
  const confirmed = window.confirm(
    `确定清空${scope}的历史记录吗？\n\n正在运行的记录会自动保留，此操作不可恢复。`
  )
  if (!confirmed) return
  actionKey.value = 'clear-runs'
  try {
    const result = await api.clearScheduleRuns({
      job_key: runsJobKey.value || undefined,
      status: runsStatus.value || undefined
    })
    runPage.value = 1
    await Promise.all([loadRuns(), loadStatus()])
    const skipped = result.skipped_running ? `，保留 ${result.skipped_running} 条运行中记录` : ''
    setMessage(`已清空 ${result.deleted} 条历史记录${skipped}。`, 'success')
  } catch (error) {
    setMessage(`清空失败：${error instanceof Error ? error.message : String(error)}`, 'error')
  } finally {
    actionKey.value = ''
  }
}

function edit(job: ScheduleJob) {
  editingKey.value = job.job_key
  Object.assign(form, {
    name: job.name,
    description: job.description || '',
    schedule_type: job.schedule_type,
    run_time: job.run_time || '',
    run_once_at: job.run_once_at ? job.run_once_at.slice(0, 16) : '',
    weekday_mask: weekdayValues(job.weekday_mask).join(','),
    interval_minutes: job.interval_minutes,
    window_start: job.window_start || '',
    window_end: job.window_end || '',
    trading_day_only: job.trading_day_only,
    digital_human_code: job.digital_human_code || '',
    prompt_template: job.prompt_template || '',
    push_channel_key: job.push_channel_key || '',
    enabled: job.enabled,
    allow_code_execution: job.allow_code_execution,
    sort_order: job.sort_order
  })
  editorOpen.value = true
}

function openCreate() {
  editingKey.value = ''
  Object.assign(form, {
    name: '',
    description: '',
    schedule_type: 'daily',
    run_time: '09:00',
    run_once_at: '',
    weekday_mask: '1,2,3,4,5',
    interval_minutes: null,
    window_start: '',
    window_end: '',
    trading_day_only: true,
    digital_human_code: 'stock-pm',
    prompt_template: '',
    push_channel_key: '',
    enabled: true,
    allow_code_execution: false,
    sort_order: 0
  })
  editorOpen.value = true
}

function closeEditor() {
  if (saving.value) return
  editorOpen.value = false
}

async function save() {
  if (saving.value) return
  const creating = !editingKey.value
  saving.value = true
  try {
    const payload = {
      name: form.name,
      description: form.description || null,
      job_kind: 'agent_task',
      schedule_type: form.schedule_type,
      run_time: form.run_time || null,
      run_once_at: form.run_once_at || null,
      weekday_mask: form.weekday_mask || null,
      interval_minutes: form.interval_minutes,
      window_start: form.window_start || null,
      window_end: form.window_end || null,
      trading_day_only: form.trading_day_only,
      digital_human_code: form.digital_human_code || null,
      prompt_template: form.prompt_template || null,
      push_channel_key: form.push_channel_key || null,
      enabled: form.enabled,
      allow_code_execution: form.allow_code_execution,
      sort_order: form.sort_order
    }
    if (editingKey.value) await api.saveScheduleJob(editingKey.value, payload)
    else await api.createScheduleJob(payload)
    await Promise.all([loadJobs(), loadJobOptions()])
    editorOpen.value = false
    setMessage(`${form.name} 已${creating ? '创建' : '保存'}，新的调度配置立即生效。`, 'success')
  } catch (error) {
    setMessage(`保存失败：${error instanceof Error ? error.message : String(error)}`, 'error')
  } finally {
    saving.value = false
  }
}

async function toggle(job: ScheduleJob) {
  if (actionKey.value) return
  actionKey.value = `toggle:${job.job_key}`
  try {
    await api.toggleScheduleJob(job.job_key)
    await Promise.all([loadJobs(), loadJobOptions()])
    setMessage(`${job.name} 已${job.enabled ? '停用' : '启用'}。`, 'success')
  } catch (error) {
    setMessage(`启停失败：${error instanceof Error ? error.message : String(error)}`, 'error')
  } finally {
    actionKey.value = ''
  }
}

async function removeJob(job: ScheduleJob) {
  if (actionKey.value) return
  const confirmed = window.confirm(
    `确定删除定时任务“${job.name}”吗？\n\n任务删除后不会再自动执行，已有历史记录仍会保留。`
  )
  if (!confirmed) return
  actionKey.value = `delete:${job.job_key}`
  try {
    await api.deleteScheduleJob(job.job_key)
    if (runsJobKey.value === job.job_key) runsJobKey.value = ''
    await Promise.all([loadJobs(), loadJobOptions()])
    setMessage(`${job.name} 已删除，已有执行历史仍可查询或清空。`, 'success')
  } catch (error) {
    setMessage(`删除失败：${error instanceof Error ? error.message : String(error)}`, 'error')
  } finally {
    actionKey.value = ''
  }
}

async function runNow(job: ScheduleJob) {
  if (actionKey.value) return
  actionKey.value = `run:${job.job_key}`
  try {
    await api.runScheduleJob(job.job_key)
    setMessage(`${job.name} 已进入执行队列，正在打开执行历史。`, 'success')
    await showHistory(job)
  } catch (error) {
    setMessage(`触发失败：${error instanceof Error ? error.message : String(error)}`, 'error')
  } finally {
    actionKey.value = ''
  }
}

async function showHistory(job: ScheduleJob) {
  activeView.value = 'history'
  runsJobKey.value = job.job_key
  runsStatus.value = ''
  runPage.value = 1
  await loadRuns()
}

async function showAllHistory() {
  activeView.value = 'history'
  runsJobKey.value = ''
  runsStatus.value = ''
  runPage.value = 1
  await loadRuns()
}

onMounted(async () => {
  await Promise.all([loadStatus(), loadJobs(), loadJobOptions()])
  await loadRuns()
  timer = window.setInterval(() => {
    void loadStatus()
    if (activeView.value === 'history' && runs.value.some((item) => item.status === 'running')) {
      void loadRuns()
    }
  }, 5000)
})

onUnmounted(() => {
  if (timer) window.clearInterval(timer)
  document.body.classList.remove('rhythm-modal-open')
})
</script>

<template>
  <section class="rhythm-hub">
    <header class="rhythm-hero">
      <div>
        <span class="eyebrow">AUTOMATION CONTROL</span>
        <h1>定时任务</h1>
        <p>统一管理盘前、盘中、收盘后的自动研究与群消息推送。</p>
      </div>
      <div v-if="status" class="rhythm-health-badge" :class="{ ok: status.scheduler_alive, danger: !status.scheduler_alive }">
        <i></i>
        <span><small>调度服务</small><strong>{{ schedulerState }}</strong></span>
      </div>
    </header>

    <section v-if="status" class="hub-metrics rhythm-metrics" aria-label="调度概览">
      <div><span>启用任务</span><strong>{{ enabledJobTotal }} / {{ jobTotal }}</strong></div>
      <div><span>今日执行</span><strong>{{ todayExecutions }}</strong></div>
      <div><span>今日成功率</span><strong>{{ todaySuccessRate }}</strong></div>
      <div><span>飞书任务群</span><strong>{{ notifyState }}</strong></div>
    </section>

    <div v-if="status" class="rhythm-meta-line">
      <span :class="{ accent: status.is_trading_day }">{{ status.is_trading_day ? '今日为交易日' : '今日非交易日' }}</span>
      <span>成功 {{ status.today.success }}</span>
      <span :class="{ danger: status.today.failed }">失败 {{ status.today.failed }}</span>
      <span>运行中 {{ status.today.running }}</span>
      <span>最近心跳 {{ beijingFromDb(status.heartbeat_at) }}</span>
    </div>

    <nav class="rhythm-view-tabs" aria-label="定时任务功能">
      <button type="button" :class="{ active: activeView === 'settings' }" @click="activeView = 'settings'">
        <span>任务设置</span>
        <small>计划、执行内容与启停管理</small>
      </button>
      <button type="button" :class="{ active: activeView === 'history' }" @click="showAllHistory">
        <span>执行历史</span>
        <small>运行状态、耗时与完整结果</small>
      </button>
    </nav>

    <p v-if="message" class="hub-message" :class="messageTone">{{ message }}</p>

    <section v-if="activeView === 'settings'" class="hub-section">
      <header class="hub-section-title">
        <div><h2>定时任务设置</h2><p>维护执行计划、数字人、推送方式和安全权限。</p></div>
        <div class="rhythm-section-actions">
          <span class="rhythm-section-count">共 {{ jobTotal }} 项，{{ enabledJobTotal }} 项已启用</span>
          <button type="button" class="rhythm-create-button" @click="openCreate">＋ 新建任务</button>
        </div>
      </header>

      <div class="rhythm-job-list">
        <article
          v-for="job in jobs"
          :key="job.job_key"
          :class="{ active: job.job_key === runsJobKey, disabled: !job.enabled }"
        >
          <div class="rhythm-job-mark" :class="{ off: !job.enabled }">{{ jobMark(job) }}</div>
          <div class="rhythm-job-identity">
            <div>
              <strong>{{ job.name }}</strong>
              <span>{{ jobKindLabel(job.job_kind) }}</span>
              <span v-if="job.created_via === 'agent'">总调度创建</span>
            </div>
            <p>{{ jobDescription(job) }}</p>
            <code>{{ job.job_key }}</code>
          </div>
          <div class="rhythm-job-fact schedule">
            <small>调度计划</small>
            <strong>{{ scheduleText(job) }}</strong>
            <span>{{ job.trading_day_only ? '仅交易日执行' : '每日均可执行' }}</span>
          </div>
          <div class="rhythm-job-fact delivery">
            <small>执行与推送</small>
            <strong>{{ humanLabel(job.digital_human_code) }}</strong>
            <span>{{ channelLabel(job) }}</span>
          </div>
          <div class="rhythm-job-last">
            <small>上次运行</small>
            <span class="job-status" :class="job.last_status || 'none'" :title="job.last_error || ''">
              <i></i>{{ statusText(job.last_status) }}
            </span>
            <time>{{ beijingFromDb(job.last_run_at) }}</time>
          </div>
          <div class="rhythm-job-actions">
            <button type="button" @click="edit(job)">配置</button>
            <button type="button" :class="{ danger: job.enabled }" :disabled="Boolean(actionKey)" @click="toggle(job)">
              {{ isActionBusy('toggle', job) ? '处理中' : job.enabled ? '停用' : '启用' }}
            </button>
            <button class="run" type="button" :disabled="Boolean(actionKey)" @click="runNow(job)">
              {{ isActionBusy('run', job) ? '执行中' : '立即执行' }}
            </button>
            <button type="button" @click="showHistory(job)">运行记录</button>
            <button
              class="danger"
              type="button"
              :disabled="Boolean(actionKey)"
              @click="removeJob(job)"
            >
              {{ isActionBusy('delete', job) ? '删除中' : '删除' }}
            </button>
          </div>
        </article>
        <div v-if="!jobs.length" class="rhythm-empty">
          <strong>这个账号还没有定时任务</strong>
          <span>新用户的任务空间默认留空，可以从这里创建第一项任务。</span>
          <button type="button" @click="openCreate">新建任务</button>
        </div>
      </div>

      <footer v-if="jobPages > 1" class="rhythm-pagination" aria-label="任务设置分页">
        <span>第 {{ jobPageText }} 页 · 共 {{ jobTotal }} 项</span>
        <div>
          <button type="button" :disabled="jobPage <= 1" @click="setJobPage(jobPage - 1)">上一页</button>
          <button type="button" :disabled="jobPage >= jobPages" @click="setJobPage(jobPage + 1)">下一页</button>
        </div>
      </footer>
    </section>

    <section v-else class="hub-section rhythm-history-section">
      <header class="hub-section-title">
        <div>
          <h2>定时任务执行历史</h2>
          <p>{{ historyJob ? `当前筛选：${historyJob.name}` : '查看全部自动调度与手动执行记录' }}</p>
        </div>
        <span class="rhythm-section-count">共 {{ runTotal }} 条记录</span>
      </header>

      <div class="rhythm-history-toolbar">
        <label>
          <span>任务</span>
          <select v-model="runsJobKey" @change="applyHistoryFilters">
            <option value="">全部任务</option>
            <option v-for="job in jobOptions" :key="job.job_key" :value="job.job_key">{{ job.name }}</option>
          </select>
        </label>
        <label>
          <span>状态</span>
          <select v-model="runsStatus" @change="applyHistoryFilters">
            <option value="">全部状态</option>
            <option value="success">成功</option>
            <option value="failed">失败</option>
            <option value="running">运行中</option>
            <option value="skipped">已跳过</option>
          </select>
        </label>
        <button class="quiet-action" type="button" @click="applyHistoryFilters">刷新记录</button>
        <button
          class="quiet-action danger"
          type="button"
          :disabled="Boolean(actionKey) || !runTotal"
          @click="clearHistory"
        >
          {{ actionKey === 'clear-runs' ? '清空中' : '清空记录' }}
        </button>
      </div>

      <div class="rhythm-run-list">
        <div class="rhythm-run-head" aria-hidden="true">
          <span>状态</span><span>开始时间</span><span>任务</span><span>触发方式</span><span>耗时</span><span>结果摘要</span><span></span>
        </div>
        <article v-for="run in runs" :key="run.run_id">
          <span class="rhythm-run-state" :class="run.status"><i></i>{{ statusText(run.status) }}</span>
          <time>{{ beijingFromDb(run.started_at || run.created_at) }}</time>
          <strong>{{ run.job_name || jobOptions.find((job) => job.job_key === run.job_key)?.name || run.job_key }}</strong>
          <span>{{ triggerText(run.trigger_type) }}</span>
          <span>{{ durationText(run.duration_ms) }}</span>
          <p :class="{ error: Boolean(run.error) }">{{ runPreview(run) }}</p>
          <button type="button" @click="openRunDetail(run)">查看</button>
        </article>
        <div v-if="!runs.length" class="rhythm-empty">暂无运行记录。</div>
      </div>

      <footer v-if="runPages > 1" class="rhythm-pagination" aria-label="执行历史分页">
        <span>第 {{ runPageText }} 页 · 共 {{ runTotal }} 条</span>
        <div>
          <button type="button" :disabled="runPage <= 1" @click="setRunPage(runPage - 1)">上一页</button>
          <button type="button" :disabled="runPage >= runPages" @click="setRunPage(runPage + 1)">下一页</button>
        </div>
      </footer>
    </section>

    <div v-if="editorOpen" class="model-dialog-backdrop rhythm-dialog-backdrop" @mousedown.self="closeEditor">
      <form class="model-dialog model-dialog--wide rhythm-dialog" role="dialog" aria-modal="true" @submit.prevent="save">
        <header class="dialog-header">
          <div><small>SCHEDULE SETUP</small><h2>{{ isCreating ? '新建定时任务' : `配置 ${editingJob?.name || ''}` }}</h2></div>
          <button type="button" title="关闭" aria-label="关闭" :disabled="saving" @click="closeEditor">×</button>
        </header>

        <div class="dialog-body rhythm-dialog-body">
          <div class="friendly-tip">
            <b>{{ isCreating ? '个人定时任务' : `${jobKindLabel(editingJob?.job_kind || 'agent_task')} · ${editingJob?.job_key}` }}</b>
            <span>{{ isCreating ? '任务只在当前用户的数据空间中可见。' : '保存后立即生效，不需要重启服务。' }}</span>
          </div>

          <section class="rhythm-form-section">
            <header><h3>基本信息</h3><span>用于列表识别和运行记录</span></header>
            <div class="rhythm-form-grid">
              <label><span>任务名称</span><input v-model="form.name" maxlength="160" required /></label>
              <label><span>调度类型</span><select v-model="form.schedule_type"><option value="once">一次执行</option><option value="daily">每日计划</option><option value="weekly">每周计划</option><option value="monthly_last">月末计划</option><option value="interval">盘中循环</option></select></label>
              <label class="span-all"><span>任务说明</span><textarea v-model="form.description" rows="2" placeholder="说明这项任务会在什么时间完成什么工作"></textarea></label>
            </div>
          </section>

          <section class="rhythm-form-section">
            <header><h3>执行计划</h3><span>所有时间均按北京时间</span></header>
            <div class="rhythm-form-grid">
              <template v-if="form.schedule_type === 'once'">
                <label><span>执行时间</span><input v-model="form.run_once_at" type="datetime-local" required /></label>
              </template>
              <template v-else-if="form.schedule_type === 'interval'">
                <label><span>执行间隔</span><div class="rhythm-number-field"><input v-model.number="form.interval_minutes" type="number" min="1" max="1440" /><b>分钟</b></div></label>
                <label><span>执行窗口</span><div class="rhythm-time-range"><input v-model="form.window_start" type="time" /><i>至</i><input v-model="form.window_end" type="time" /></div></label>
              </template>
              <label v-else><span>触发时间</span><input v-model="form.run_time" type="time" required /></label>
              <label v-if="form.schedule_type === 'monthly_last'"><span>执行日期</span><input value="每月最后一个交易日" disabled /></label>
              <label v-if="form.schedule_type === 'daily' || form.schedule_type === 'weekly'" class="span-all">
                <span>执行星期 <small>不选择代表每天</small></span>
                <span class="rhythm-weekdays">
                  <button v-for="day in WEEKDAYS" :key="day.value" type="button" :class="{ selected: weekdaySelected(day.value) }" @click="toggleWeekday(day.value)">{{ day.label }}</button>
                </span>
              </label>
            </div>
          </section>

          <section class="rhythm-form-section">
            <header><h3>执行内容</h3><span>选择负责的数字人和结果去向</span></header>
            <div class="rhythm-form-grid">
              <label><span>数字人角色</span><select v-model="form.digital_human_code"><option value="">规则任务，不使用数字人</option><option v-for="item in DIGITAL_HUMANS" :key="item.code" :value="item.code">{{ item.label }}</option></select></label>
              <label><span>结果推送</span><select v-model="form.push_channel_key"><option v-for="item in CHANNELS" :key="item.key" :value="item.key">{{ item.label }}</option></select></label>
              <label class="span-all"><span>提示词模板 <small>支持 {date}、{weekday}、{time}</small></span><textarea v-model="form.prompt_template" rows="6" placeholder="填写数字人执行任务时收到的完整指令"></textarea></label>
            </div>
          </section>

          <div class="rhythm-switch-grid">
            <label class="friendly-switch"><input v-model="form.trading_day_only" type="checkbox" /><i></i><span><b>仅交易日执行</b><small>节假日和周末自动跳过</small></span></label>
            <label class="friendly-switch"><input v-model="form.enabled" type="checkbox" /><i></i><span><b>启用该任务</b><small>关闭后不再自动触发</small></span></label>
            <label class="friendly-switch"><input v-model="form.allow_code_execution" type="checkbox" /><i></i><span><b>允许沙箱代码</b><small>仅在当前用户隔离工作区的 Docker 沙箱执行</small></span></label>
          </div>

          <details class="rhythm-advanced">
            <summary>高级设置</summary>
            <label><span>显示顺序</span><input v-model.number="form.sort_order" type="number" min="0" /></label>
          </details>
        </div>

        <footer><button type="button" :disabled="saving" @click="closeEditor">取消</button><span></span><button class="primary-action" type="submit" :disabled="saving">{{ saving ? '保存中…' : '保存配置' }}</button></footer>
      </form>
    </div>

    <div v-if="selectedRun" class="model-dialog-backdrop rhythm-dialog-backdrop" @mousedown.self="closeRunDetail">
      <section class="model-dialog model-dialog--wide rhythm-run-dialog" role="dialog" aria-modal="true">
        <header class="dialog-header">
          <div><small>RUN DETAIL</small><h2>运行详情</h2></div>
          <button type="button" title="关闭" aria-label="关闭" @click="closeRunDetail">×</button>
        </header>
        <div class="dialog-body">
          <div class="rhythm-run-meta">
            <div><span>任务</span><strong>{{ selectedRun.job_name || jobOptions.find((job) => job.job_key === selectedRun?.job_key)?.name || selectedRun.job_key }}</strong></div>
            <div><span>状态</span><strong class="job-status" :class="selectedRun.status">{{ statusText(selectedRun.status) }}</strong></div>
            <div><span>开始时间</span><strong>{{ beijingFromDb(selectedRun.started_at || selectedRun.created_at) }}</strong></div>
            <div><span>运行耗时</span><strong>{{ durationText(selectedRun.duration_ms) }}</strong></div>
          </div>
          <div class="rhythm-run-output" :class="{ error: Boolean(selectedRun.error) }">
            <span>{{ selectedRun.error ? '错误信息' : '结果输出' }}</span>
            <pre v-if="runDetailLoading">正在读取完整结果…</pre>
            <pre v-else>{{ runDetailError ? `${runDetailError}\n\n当前显示摘要：\n${runDetail(selectedRun)}` : runDetail(selectedRun) }}</pre>
          </div>
        </div>
        <footer><span></span><button type="button" @click="closeRunDetail">关闭</button></footer>
      </section>
    </div>
  </section>
</template>
