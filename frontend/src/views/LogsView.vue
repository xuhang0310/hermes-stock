<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api } from '../api'
import type { SystemLog } from '../types'

const logs = ref<SystemLog[]>([])
const modules = ref<string[]>([])
const activeModule = ref('')
const activeLevel = ref('')
const autoRefresh = ref(false)
const message = ref('')
let timer: number | undefined

const LEVELS = ['ERROR', 'WARNING', 'INFO', 'DEBUG']

const MODULE_LABELS: Record<string, string> = {
  system: '系统 / 生命周期',
  auth: '登录鉴权',
  api: 'HTTP 请求',
  agent: 'Agent 对话',
  agent_sql: 'SQL 沙箱',
  scheduler: '定时调度',
  feishu: '飞书推送',
  feishu_bot: '飞书机器人',
  position: '持仓录入',
  positions: '持仓录入',
  db: '数据库迁移',
  seed: '种子数据'
}

function moduleLabel(m: string) {
  return MODULE_LABELS[m] ? `${MODULE_LABELS[m]}（${m}）` : m
}

async function load() {
  message.value = ''
  try {
    logs.value = await api.logs({
      module: activeModule.value || undefined,
      level: activeLevel.value || undefined,
      limit: 200
    })
  } catch (error) {
    message.value = `加载日志失败：${error instanceof Error ? error.message : String(error)}`
  }
}

async function loadModules() {
  try {
    modules.value = await api.logModules()
  } catch {
    /* 忽略 */
  }
}

const counts = computed(() => {
  const c: Record<string, number> = { ERROR: 0, WARNING: 0, INFO: 0, DEBUG: 0 }
  for (const log of logs.value) c[log.level] = (c[log.level] || 0) + 1
  return c
})

function toggleAuto() {
  autoRefresh.value = !autoRefresh.value
  if (autoRefresh.value) {
    timer = window.setInterval(load, 5000)
  } else if (timer) {
    window.clearInterval(timer)
    timer = undefined
  }
}

onMounted(async () => {
  await Promise.all([loadModules(), load()])
})

onUnmounted(() => {
  if (timer) window.clearInterval(timer)
})
</script>

<template>
  <section class="page-section">
    <header class="section-header">
      <div>
        <h2>运行日志</h2>
        <p>分门别类记录：登录、请求、Agent 对话、定时调度、飞书推送/机器人、持仓录入等。进程细节另落 logs/ 文件。</p>
      </div>
      <button type="button" @click="load">刷新</button>
    </header>

    <div class="log-toolbar">
      <label>
        <span>类别</span>
        <select v-model="activeModule" @change="load">
          <option value="">全部</option>
          <option v-for="m in modules" :key="m" :value="m">{{ moduleLabel(m) }}</option>
        </select>
      </label>
      <label>
        <span>级别</span>
        <select v-model="activeLevel" @change="load">
          <option value="">全部</option>
          <option v-for="lv in LEVELS" :key="lv" :value="lv">{{ lv }}</option>
        </select>
      </label>
      <button type="button" :class="{ 'auto-on': autoRefresh }" @click="toggleAuto">
        {{ autoRefresh ? '自动刷新中（5s）' : '开启自动刷新' }}
      </button>
      <span class="log-counts">
        <span class="log-error">ERROR {{ counts.ERROR }}</span>
        <span class="log-warning">WARN {{ counts.WARNING }}</span>
        <span class="log-info">INFO {{ counts.INFO }}</span>
        <span>共 {{ logs.length }} 条</span>
      </span>
    </div>

    <div class="log-panel log-panel-scroll">
      <div v-if="message" class="empty-cell">{{ message }}</div>
      <div v-else-if="!logs.length" class="empty-cell">暂无日志</div>
      <div v-for="log in logs" :key="log.id" class="log-line">
        <span>{{ log.created_at }}</span>
        <strong :class="`log-${log.level.toLowerCase()}`">{{ log.level }}</strong>
        <code>{{ log.module }}</code>
        <p>{{ log.message }}</p>
      </div>
    </div>
  </section>
</template>
