<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { api } from '../api'
import type { NotifyChannel, NotifyChannelTestResult } from '../types'

const channels = ref<NotifyChannel[]>([])
const editingKey = ref('')
const message = ref('')
const error = ref('')
const loading = ref(false)
const saving = ref(false)
const testing = ref(false)
const testingKey = ref('')
const dialogOpen = ref(false)
const testResult = ref<NotifyChannelTestResult | null>(null)

const form = reactive({
  name: '',
  webhook_url: '',
  secret: '',
  status: 'enabled',
  remark: ''
})

const stats = computed(() => ({
  total: channels.value.length,
  enabled: channels.value.filter((item) => item.status === 'enabled').length,
  ready: channels.value.filter((item) => item.status === 'enabled' && item.has_webhook && !item.cipher_invalid).length,
  signed: channels.value.filter((item) => item.has_secret).length
}))

const editingChannel = computed(() => channels.value.find((item) => item.channel_key === editingKey.value) || null)

async function load() {
  loading.value = true
  error.value = ''
  try {
    channels.value = await api.notifyChannels()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载通知通道失败'
  } finally {
    loading.value = false
  }
}

function edit(row: NotifyChannel) {
  editingKey.value = row.channel_key
  testResult.value = null
  message.value = ''
  error.value = ''
  Object.assign(form, {
    name: row.name,
    webhook_url: '',
    secret: '',
    status: row.status,
    remark: row.remark || ''
  })
  dialogOpen.value = true
}

function openFirstChannel() {
  if (channels.value[0]) edit(channels.value[0])
}

function closeDialog() {
  if (saving.value || testing.value) return
  dialogOpen.value = false
  testResult.value = null
}

async function save() {
  if (!editingKey.value) return
  message.value = ''
  error.value = ''
  saving.value = true
  try {
    await api.saveNotifyChannel(editingKey.value, {
      name: form.name,
      webhook_url: form.webhook_url || null,
      secret: form.secret || null,
      status: form.status,
      remark: form.remark || null
    })
    const savedName = form.name
    form.webhook_url = ''
    form.secret = ''
    await load()
    dialogOpen.value = false
    message.value = `${savedName} 已保存，Webhook 与签名密钥已加密入库。`
  } catch (err) {
    error.value = err instanceof Error ? err.message : '保存通知通道失败'
  } finally {
    saving.value = false
  }
}

async function runTest() {
  if (!editingKey.value && !form.webhook_url) return
  testing.value = true
  testResult.value = null
  error.value = ''
  try {
    testResult.value = await api.testNotifyChannel({
      channel_key: editingKey.value || null,
      webhook_url: form.webhook_url || null,
      secret: form.secret || null
    })
  } catch (err) {
    testResult.value = { ok: false, error: err instanceof Error ? err.message : String(err), latency_ms: 0 }
  } finally {
    testing.value = false
  }
}

async function testRow(item: NotifyChannel) {
  testingKey.value = item.channel_key
  message.value = ''
  error.value = ''
  try {
    const result = await api.testNotifyChannel({ channel_key: item.channel_key })
    if (result.ok) {
      message.value = `「${item.name}」测试消息已发送，请到目标群确认（${result.latency_ms}ms）。`
    } else {
      error.value = `「${item.name}」测试失败：${result.error || '未知错误'}`
    }
    await load()
  } catch (err) {
    error.value = `「${item.name}」测试失败：${err instanceof Error ? err.message : String(err)}`
  } finally {
    testingKey.value = ''
  }
}

function purposeLabel(purpose: string) {
  if (purpose === 'schedule') return '定时任务统一推送'
  return purpose
}

function channelState(item: NotifyChannel) {
  if (item.status !== 'enabled') return '已停用'
  if (item.cipher_invalid) return '密钥异常'
  if (!item.has_webhook) return '待配置'
  return '已连接'
}

function channelStateClass(item: NotifyChannel) {
  if (item.status !== 'enabled') return 'disabled'
  if (item.cipher_invalid) return 'error'
  if (!item.has_webhook) return 'warning'
  return 'enabled'
}

onMounted(load)
</script>

<template>
  <section class="model-hub settings-resource-hub">
    <header class="model-hub-hero">
      <div>
        <span class="eyebrow">NOTIFICATION ROUTES</span>
        <h1>通知通道</h1>
        <p>管理定时报告与系统告警的送达路径，敏感凭据始终加密保存。</p>
      </div>
      <button class="primary-action" type="button" :disabled="loading || !channels.length" @click="openFirstChannel">
        <b>⚙</b> 配置通道
      </button>
    </header>

    <section v-if="channels.length" class="active-model-card settings-overview-card">
      <div class="active-model-orb">NT</div>
      <div>
        <span>消息投递状态</span>
        <strong>{{ stats.ready }} 个通知通道可用</strong>
        <code>定时任务报告和系统异常通过消息卡片统一推送</code>
      </div>
      <i>{{ stats.signed }} 个已启用签名</i>
    </section>
    <section v-else-if="!loading" class="empty-callout">
      <span>还没有通知通道</span>
      <p>服务端尚未返回可配置的通道定义。</p>
    </section>

    <div v-if="message" class="hub-message success" role="status">{{ message }}</div>
    <div v-if="error" class="hub-message error" role="alert">{{ error }}</div>

    <section class="hub-section">
      <header class="hub-section-title">
        <div>
          <h2>消息出口</h2>
          <p>{{ stats.ready }} 个已连接，共 {{ stats.total }} 个通道</p>
        </div>
        <button class="quiet-action" type="button" :disabled="loading" @click="load">{{ loading ? '加载中…' : '刷新状态' }}</button>
      </header>

      <div class="provider-grid settings-card-grid">
        <article
          v-for="item in channels"
          :key="item.channel_key"
          class="provider-card settings-resource-card notify-channel-card"
          :class="{ disabled: item.status === 'disabled' }"
        >
          <div class="provider-card-top">
            <div class="provider-monogram">飞</div>
            <span class="status-pill" :class="channelStateClass(item)">{{ channelState(item) }}</span>
          </div>
          <div class="settings-card-copy">
            <h3>{{ item.name }}</h3>
            <p>{{ purposeLabel(item.purpose) }} · {{ item.channel_type }}</p>
          </div>
          <div class="provider-facts settings-card-facts">
            <span><b>{{ item.has_webhook ? '已配置' : '未配置' }}</b> Webhook</span>
            <span><b>{{ item.has_secret ? '已签名' : '未签名' }}</b> 安全校验</span>
          </div>
          <div class="settings-card-detail">
            <span>最近成功</span>
            <code>{{ item.last_ok_at || '尚无成功记录' }}</code>
          </div>
          <div v-if="item.last_error" class="settings-card-alert" :title="item.last_error">最近错误：{{ item.last_error }}</div>
          <div class="provider-actions">
            <button type="button" :disabled="testingKey === item.channel_key" @click="testRow(item)">{{ testingKey === item.channel_key ? '测试中…' : '发送测试' }}</button>
            <button class="icon-action" type="button" @click="edit(item)">配置</button>
          </div>
        </article>
      </div>
    </section>

    <div v-if="dialogOpen && editingChannel" class="model-dialog-backdrop" @mousedown.self="closeDialog">
      <form class="model-dialog" @submit.prevent="save">
        <header class="dialog-header">
          <div><small>CHANNEL SETUP</small><h2>配置 {{ editingChannel.name }}</h2></div>
          <button type="button" title="关闭" @click="closeDialog">×</button>
        </header>
        <div class="dialog-body">
          <div class="friendly-tip">
            <b>飞书群机器人</b>
            <span>Webhook 与签名密钥只会加密存储，页面不会回显原文。留空表示保留已有值。</span>
          </div>
          <label><span>通道名称</span><input v-model="form.name" required /></label>
          <label><span>Webhook 地址</span><input v-model="form.webhook_url" type="password" placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/…" /></label>
          <label><span>签名校验密钥</span><input v-model="form.secret" type="password" placeholder="留空保留机器人当前签名密钥" /></label>
          <label class="friendly-switch">
            <input v-model="form.status" type="checkbox" true-value="enabled" false-value="disabled" />
            <i></i><span><b>启用这个通知通道</b><small>停用后定时任务仍执行，但不会向此通道推送</small></span>
          </label>
          <label><span>备注</span><textarea v-model="form.remark" rows="3"></textarea></label>
          <div v-if="testResult" class="test-result" :class="{ ok: testResult.ok, fail: !testResult.ok }">
            <strong>{{ testResult.ok ? testResult.message || '测试消息已发送' : testResult.error || '测试失败' }}</strong>
            <span v-if="testResult.latency_ms">{{ testResult.latency_ms }}ms</span>
          </div>
          <div v-if="error" class="test-result fail"><strong>{{ error }}</strong></div>
        </div>
        <footer>
          <button type="button" :disabled="saving || testing" @click="closeDialog">取消</button><span></span>
          <button type="button" :disabled="testing || saving" @click="runTest">{{ testing ? '测试中…' : '测试推送' }}</button>
          <button class="primary-action" type="submit" :disabled="saving || testing">{{ saving ? '保存中…' : '保存通道' }}</button>
        </footer>
      </form>
    </div>
  </section>
</template>
