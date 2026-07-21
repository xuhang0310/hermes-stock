<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { api } from '../api'
import type { FeishuBotBinding } from '../types'

const bindings = ref<FeishuBotBinding[]>([])
const editingId = ref<number | null>(null)
const message = ref('')
const testing = ref(false)
let timer: number | undefined

const form = reactive({
  app_id: '',
  app_secret: '',
  enabled: true
})

const editing = computed(() => bindings.value.find((item) => item.profile_id === editingId.value) || null)
const onlineCount = computed(() => bindings.value.filter((item) => item.runtime.running).length)
const boundCount = computed(() => bindings.value.filter((item) => item.app_id_masked).length)
const todayTotal = computed(() => bindings.value.reduce((sum, item) => sum + (item.runtime.today_count || 0), 0))
const featureEnabled = computed(() => bindings.value[0]?.feature_enabled ?? true)
const managerRunning = computed(() => bindings.value[0]?.runtime.manager_running ?? false)

function statusText(item: FeishuBotBinding) {
  if (!item.app_id_masked) return '未绑定'
  if (item.cipher_invalid) return '密文失效'
  if (!item.enabled) return '已停用'
  if (item.runtime.state === 'online') return '在线'
  if (item.runtime.state === 'connecting') return '连接中'
  if (item.runtime.state === 'error') return '异常'
  return '未启动'
}

function statusClass(item: FeishuBotBinding) {
  if (!item.app_id_masked) return 'none'
  if (item.cipher_invalid || item.runtime.state === 'error') return 'failed'
  if (!item.enabled) return 'none'
  if (item.runtime.state === 'online') return 'success'
  if (item.runtime.state === 'connecting') return 'running'
  return 'none'
}

async function load() {
  try {
    bindings.value = await api.feishuBots()
  } catch (error) {
    message.value = `加载失败：${error instanceof Error ? error.message : String(error)}`
  }
}

function edit(item: FeishuBotBinding) {
  editingId.value = item.profile_id
  Object.assign(form, { app_id: '', app_secret: '', enabled: item.enabled || !item.app_id_masked })
  message.value = item.app_id_masked ? `正在配置「${item.name}」，App ID 留空则保留 ${item.app_id_masked}` : ''
}

async function save() {
  const item = editing.value
  if (!item) return
  const appId = form.app_id.trim() || null
  if (!appId && !item.app_id_masked) {
    message.value = '请填写 App ID'
    return
  }
  message.value = ''
  try {
    const saved = await api.saveFeishuBot(item.profile_id, {
      app_id: appId,
      app_secret: form.app_secret || null,
      enabled: form.enabled
    })
    if (saved.runtime?.error) {
      message.value = `「${item.name}」已保存，但长连接启动失败：${saved.runtime.error}`
    } else {
      message.value = `「${item.name}」绑定已保存${form.enabled ? '，长连接已启动' : ''}`
    }
    form.app_id = ''
    form.app_secret = ''
    await load()
  } catch (error) {
    message.value = `保存失败：${error instanceof Error ? error.message : String(error)}`
  }
}

async function testRow(item: FeishuBotBinding) {
  // 行内测试：只测「已保存」的凭据，不读编辑表单（避免残留输入串台）
  message.value = `正在测试「${item.name}」已保存的凭据…`
  try {
    const result = await api.testFeishuBot(item.profile_id, {})
    message.value = result.ok
      ? `✅ 「${item.name}」${result.message}（${result.latency_ms}ms）`
      : `❌ 「${item.name}」${result.error}`
  } catch (error) {
    message.value = `「${item.name}」测试失败：${error instanceof Error ? error.message : String(error)}`
  }
}

async function runTest() {
  const item = editing.value
  if (!item) return
  testing.value = true
  message.value = ''
  try {
    const result = await api.testFeishuBot(item.profile_id, {
      app_id: form.app_id || null,
      app_secret: form.app_secret || null
    })
    message.value = result.ok
      ? `✅ ${result.message}（${result.latency_ms}ms）`
      : `❌ ${result.error}`
  } catch (error) {
    message.value = `测试失败：${error instanceof Error ? error.message : String(error)}`
  } finally {
    testing.value = false
  }
}

async function toggle(item: FeishuBotBinding) {
  message.value = ''
  try {
    await api.toggleFeishuBot(item.profile_id)
    await load()
  } catch (error) {
    message.value = `启停失败：${error instanceof Error ? error.message : String(error)}`
  }
}

async function unbind(item: FeishuBotBinding) {
  if (!window.confirm(`确定解除「${item.name}」的飞书绑定？凭据将被删除。`)) return
  try {
    await api.unbindFeishuBot(item.profile_id)
    message.value = `已解除「${item.name}」的绑定`
    if (editingId.value === item.profile_id) editingId.value = null
    await load()
  } catch (error) {
    message.value = `解绑失败：${error instanceof Error ? error.message : String(error)}`
  }
}

onMounted(async () => {
  await load()
  timer = window.setInterval(() => void load(), 10000)
})

onUnmounted(() => {
  if (timer) window.clearInterval(timer)
})
</script>

<template>
  <section class="page-section">
    <header class="section-header">
      <div>
        <h2>飞书机器人</h2>
        <p>
          每个数字人可绑定一个飞书自建应用（机器人）。用户在飞书私聊或群 @ 机器人，
          即由对应数字人应答。App Secret 加密存储，接口只回传掩码。
        </p>
      </div>
    </header>
    <div class="rhythm-status">
      <span class="status-pill" :class="featureEnabled && managerRunning ? 'ok' : 'danger'">
        {{ !featureEnabled
          ? '功能未启用（STOCK_FEISHU_BOT_ENABLED=false）'
          : managerRunning ? '长连接管理器 运行中' : '长连接管理器 未运行' }}
      </span>
      <span class="status-pill">机器人在线 {{ onlineCount }} / 已绑定 {{ boundCount }}</span>
      <span class="status-pill">今日消息 {{ todayTotal }}</span>
    </div>
  </section>

  <section class="page-section">
    <div class="table-wrap">
      <table class="list-table">
        <thead>
          <tr>
            <th>数字人</th>
            <th>飞书应用</th>
            <th>连接状态</th>
            <th>今日消息</th>
            <th>最近活跃</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="!bindings.length">
            <td colspan="6" class="empty-cell">暂无数字人</td>
          </tr>
          <tr v-for="item in bindings" :key="item.profile_id" :class="{ 'row-active': item.profile_id === editingId }">
            <td>
              <span class="job-dot" :class="item.runtime.state === 'online' ? 'on' : 'off'"></span>
              {{ item.name }}
              <span v-if="item.runtime.error" class="err-flag" :title="item.runtime.error">!</span>
            </td>
            <td class="nowrap">{{ item.app_id_masked || '未绑定' }}</td>
            <td><span class="job-status" :class="statusClass(item)">{{ statusText(item) }}</span></td>
            <td>{{ item.app_id_masked ? item.runtime.today_count : '-' }}</td>
            <td class="nowrap">{{ item.runtime.last_event_at || '-' }}</td>
            <td>
              <div class="cell-actions">
                <template v-if="item.app_id_masked">
                  <button type="button" @click="edit(item)">配置</button>
                  <button type="button" :class="{ danger: item.enabled }" @click="toggle(item)">
                    {{ item.enabled ? '停用' : '启用' }}
                  </button>
                  <button type="button" @click="testRow(item)">测试</button>
                  <button type="button" class="danger" @click="unbind(item)">解绑</button>
                </template>
                <button v-else type="button" @click="edit(item)">绑定机器人</button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <form v-if="editing" class="editor-panel feishu-bot-editor" @submit.prevent="save">
      <header>
        <h2>配置飞书机器人 — {{ editing.name }}</h2>
      </header>
      <label>
        <span>App ID{{ editing.app_id_masked ? `（留空保留 ${editing.app_id_masked}）` : '' }}</span>
        <input v-model="form.app_id" :placeholder="editing.app_id_masked || 'cli_xxxxxxxxxxxxxxxxx'" />
      </label>
      <label>
        <span>App Secret（{{ editing.has_secret ? '留空保留已保存值' : '首次绑定必填' }}）</span>
        <input v-model="form.app_secret" type="password" placeholder="飞书开放平台 · 凭证与基础信息中获取" />
      </label>
      <label class="checkbox-row">
        <input v-model="form.enabled" type="checkbox" />
        <span>启用长连接（收到私聊 / 群 @ 即由该数字人应答）</span>
      </label>
      <div class="row-actions">
        <button type="submit">保存绑定</button>
        <button type="button" :disabled="testing" @click="runTest">{{ testing ? '测试中…' : '测试连接' }}</button>
      </div>
      <p class="form-message">{{ message }}</p>
      <div class="feishu-bot-guide">
        如何创建：飞书开放平台 → 创建企业自建应用 → 添加「机器人」能力 → 开通权限
        im:message、im:message:send_as_bot → 事件订阅选「长连接」并订阅 im.message.receive_v1
        → 发布版本 → 复制 App ID / App Secret 填入此处
      </div>
    </form>
    <p v-else class="form-message">{{ message }}</p>
  </section>
</template>
