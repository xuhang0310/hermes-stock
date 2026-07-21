<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { api } from '../api'
import DataTable from '../components/DataTable.vue'
import type { JobRun, ScheduleJob, ScheduleStatus } from '../types'
import { beijingFromDb } from '../utils/datetime'

const jobs = ref<ScheduleJob[]>([])
const runs = ref<JobRun[]>([])
const status = ref<ScheduleStatus | null>(null)
const editingKey = ref('')
const runsJobKey = ref('')
const message = ref('')
let timer: number | undefined

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

const form = reactive({
  name: '',
  description: '',
  schedule_type: 'daily',
  run_time: '',
  weekday_mask: '',
  interval_minutes: null as number | null,
  window_start: '',
  window_end: '',
  trading_day_only: true,
  digital_human_code: '',
  prompt_template: '',
  push_channel_key: '',
  enabled: true,
  sort_order: 0
})

const editingJob = computed(() => jobs.value.find((item) => item.job_key === editingKey.value) || null)

function humanLabel(code: string | null) {
  if (!code) return '-'
  return DIGITAL_HUMANS.find((item) => item.code === code)?.label || code
}

function channelLabel(job: ScheduleJob) {
  if (!job.push_channel_key) return '不推送'
  if (job.job_kind === 'health_check') return '飞书任务群（仅异常）'
  return CHANNELS.find((item) => item.key === job.push_channel_key)?.label || job.push_channel_key
}

function scheduleText(job: ScheduleJob) {
  if (job.schedule_type === 'interval') {
    return `每 ${job.interval_minutes ?? '-'} 分（${job.window_start || ''}-${job.window_end || ''}）`
  }
  const days = job.weekday_mask ? `[${job.weekday_mask}]` : '每天'
  if (job.schedule_type === 'weekly') return `每周 ${job.weekday_mask} ${job.run_time || ''}`
  if (job.schedule_type === 'monthly_last') return `月末 ${job.run_time || ''}`
  return `${days} ${job.run_time || ''}`
}

function statusText(value: string | null) {
  if (value === 'success') return '成功'
  if (value === 'failed') return '失败'
  if (value === 'running') return '运行中'
  if (value === 'skipped') return '跳过'
  return value || '未运行'
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
    jobs.value = await api.scheduleJobs()
  } catch (error) {
    message.value = `加载任务失败：${error instanceof Error ? error.message : String(error)}`
    return
  }
  if (!editingKey.value && jobs.value[0]) {
    edit(jobs.value[0])
  }
}

async function loadRuns(jobKey?: string) {
  try {
    runsJobKey.value = jobKey || ''
    runs.value = await api.scheduleRuns(jobKey)
  } catch (error) {
    message.value = `加载历史失败：${error instanceof Error ? error.message : String(error)}`
  }
}

function edit(job: ScheduleJob) {
  editingKey.value = job.job_key
  Object.assign(form, {
    name: job.name,
    description: job.description || '',
    schedule_type: job.schedule_type,
    run_time: job.run_time || '',
    weekday_mask: job.weekday_mask || '',
    interval_minutes: job.interval_minutes,
    window_start: job.window_start || '',
    window_end: job.window_end || '',
    trading_day_only: job.trading_day_only,
    digital_human_code: job.digital_human_code || '',
    prompt_template: job.prompt_template || '',
    push_channel_key: job.push_channel_key || '',
    enabled: job.enabled,
    sort_order: job.sort_order
  })
}

async function save() {
  if (!editingKey.value) return
  message.value = ''
  try {
    await api.saveScheduleJob(editingKey.value, {
      name: form.name,
      description: form.description || null,
      schedule_type: form.schedule_type,
      run_time: form.run_time || null,
      weekday_mask: form.weekday_mask || null,
      interval_minutes: form.interval_minutes,
      window_start: form.window_start || null,
      window_end: form.window_end || null,
      trading_day_only: form.trading_day_only,
      digital_human_code: form.digital_human_code || null,
      prompt_template: form.prompt_template || null,
      push_channel_key: form.push_channel_key || null,
      enabled: form.enabled,
      sort_order: form.sort_order
    })
    message.value = `${form.name} 已保存`
    await loadJobs()
  } catch (error) {
    message.value = `保存失败：${error instanceof Error ? error.message : String(error)}`
  }
}

async function toggle(job: ScheduleJob) {
  message.value = ''
  try {
    await api.toggleScheduleJob(job.job_key)
    await loadJobs()
  } catch (error) {
    message.value = `启停失败：${error instanceof Error ? error.message : String(error)}`
  }
}

async function runNow(job: ScheduleJob) {
  message.value = ''
  try {
    await api.runScheduleJob(job.job_key)
    message.value = `${job.name} 已触发，可在运行历史查看结果`
    await loadRuns(job.job_key)
  } catch (error) {
    message.value = `触发失败：${error instanceof Error ? error.message : String(error)}`
  }
}

onMounted(async () => {
  await Promise.all([loadStatus(), loadJobs()])
  await loadRuns()
  timer = window.setInterval(() => {
    void loadStatus()
    if (runs.value.some((item) => item.status === 'running')) {
      void loadRuns(runsJobKey.value || undefined)
    }
  }, 5000)
})

onUnmounted(() => {
  if (timer) window.clearInterval(timer)
})
</script>

<template>
  <section class="page-section">
    <header class="section-header">
      <div>
        <h2>定时任务</h2>
      </div>
    </header>
    <div class="rhythm-status" v-if="status">
      <span class="status-pill" :class="status.scheduler_alive ? 'ok' : 'danger'">
        {{ status.scheduler_alive ? '调度器运行中' : (status.scheduler_enabled ? '调度器无心跳' : '调度器已停用') }}
      </span>
      <span class="status-pill">{{ status.is_trading_day ? '今日为交易日' : '今日非交易日' }}</span>
      <span class="status-pill">今日成功 {{ status.today.success }}</span>
      <span class="status-pill" :class="status.today.failed ? 'danger' : ''">今日失败 {{ status.today.failed }}</span>
      <span class="status-pill">运行中 {{ status.today.running }}</span>
      <span
        class="status-pill"
        :class="status.notify_channel?.configured && status.notify_channel?.enabled ? 'ok' : 'danger'"
        :title="status.notify_channel?.last_error || ''"
      >
        飞书任务群 {{ status.notify_channel?.configured && status.notify_channel?.enabled ? '已启用' : '未配置' }}
      </span>
      <span class="status-muted">心跳 {{ status.heartbeat_at || '-' }}</span>
    </div>
  </section>

  <section class="management-grid">
    <form class="editor-panel" @submit.prevent="save">
      <header>
        <h2>编辑任务</h2>
      </header>
      <label>
        <span>当前任务</span>
        <select v-model="editingKey" @change="editingJob && edit(editingJob)">
          <option v-for="job in jobs" :key="job.job_key" :value="job.job_key">{{ job.name }}</option>
        </select>
      </label>
      <label>
        <span>名称</span>
        <input v-model="form.name" maxlength="160" required />
      </label>
      <label>
        <span>调度类型</span>
        <select v-model="form.schedule_type">
          <option value="daily">每日 daily</option>
          <option value="weekly">每周 weekly</option>
          <option value="monthly_last">月末 monthly_last</option>
          <option value="interval">盘中区间 interval</option>
        </select>
      </label>
      <label v-if="form.schedule_type !== 'interval'">
        <span>触发时间 HH:MM</span>
        <input v-model="form.run_time" placeholder="08:30" maxlength="5" />
      </label>
      <label v-if="form.schedule_type === 'daily' || form.schedule_type === 'weekly'">
        <span>星期（1-7 逗号分隔，留空=每天）</span>
        <input v-model="form.weekday_mask" placeholder="1,2,3,4,5" maxlength="20" />
      </label>
      <template v-if="form.schedule_type === 'interval'">
        <label>
          <span>间隔分钟</span>
          <input v-model.number="form.interval_minutes" type="number" min="1" max="1440" />
        </label>
        <label>
          <span>窗口开始 / 结束</span>
          <span class="inline-pair">
            <input v-model="form.window_start" placeholder="09:30" maxlength="5" />
            <input v-model="form.window_end" placeholder="15:00" maxlength="5" />
          </span>
        </label>
      </template>
      <label class="checkbox-row">
        <input v-model="form.trading_day_only" type="checkbox" />
        <span>仅交易日执行</span>
      </label>
      <label>
        <span>数字人角色</span>
        <select v-model="form.digital_human_code">
          <option value="">（健康检查等规则任务留空）</option>
          <option v-for="item in DIGITAL_HUMANS" :key="item.code" :value="item.code">{{ item.label }}</option>
        </select>
      </label>
      <label>
        <span>群消息推送</span>
        <select v-model="form.push_channel_key">
          <option v-for="item in CHANNELS" :key="item.key" :value="item.key">{{ item.label }}</option>
        </select>
      </label>
      <label>
        <span>提示词模板（支持 {date} {weekday} {time}）</span>
        <textarea v-model="form.prompt_template" rows="4"></textarea>
      </label>
      <label class="checkbox-row">
        <input v-model="form.enabled" type="checkbox" />
        <span>启用该任务</span>
      </label>
      <button type="submit" :disabled="!editingKey">保存任务</button>
      <p class="form-message">{{ message }}</p>
    </form>

    <section class="page-section">
      <header class="section-header">
        <div>
          <h2>节律任务</h2>
          <p>立即执行会无视交易日与时间窗，用于验收。</p>
        </div>
      </header>
      <div class="table-wrap">
        <table class="list-table">
          <thead>
            <tr>
              <th>任务</th>
              <th>调度</th>
              <th>角色</th>
              <th>推送</th>
              <th>上次运行</th>
              <th>状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="!jobs.length">
              <td colspan="7" class="empty-cell">暂无任务</td>
            </tr>
            <tr v-for="job in jobs" :key="job.job_key" :class="{ 'row-active': job.job_key === editingKey }">
              <td>
                <span class="job-dot" :class="job.enabled ? 'on' : 'off'" :title="job.enabled ? '已启用' : '已停用'"></span>
                {{ job.name }}
              </td>
              <td>{{ scheduleText(job) }}</td>
              <td>{{ humanLabel(job.digital_human_code) }}</td>
              <td>{{ channelLabel(job) }}</td>
              <td class="nowrap">{{ job.last_run_at || '-' }}</td>
              <td><span class="job-status" :class="job.last_status || 'none'">{{ statusText(job.last_status) }}</span></td>
              <td>
                <div class="cell-actions">
                  <button type="button" @click="edit(job)">编辑</button>
                  <button type="button" :class="{ danger: job.enabled }" @click="toggle(job)">
                    {{ job.enabled ? '停用' : '启用' }}
                  </button>
                  <button type="button" @click="runNow(job)">执行</button>
                  <button type="button" @click="loadRuns(job.job_key)">历史</button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <header class="section-header">
        <div>
          <h2>运行历史{{ runsJobKey ? `（${runsJobKey}）` : '' }}</h2>
          <p>
            <button type="button" class="link-button" @click="loadRuns()">查看全部</button>
          </p>
        </div>
      </header>
      <DataTable
        :columns="['时间', '任务', '触发', '状态', '耗时ms', '结果 / 错误']"
        :rows="runs.map((run) => [
          beijingFromDb(run.created_at),
          run.job_key,
          run.trigger_type,
          statusText(run.status),
          run.duration_ms ?? '-',
          run.error || run.output_preview || '-'
        ])"
      />
    </section>
  </section>
</template>
