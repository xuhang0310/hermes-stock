<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { api } from '../api'
import type { MyDefaultJobs, NotifyChannel, NotifyChannelTestResult } from '../types'

const channel = ref<NotifyChannel | null>(null)
const loading = ref(false)
const saving = ref(false)
const testing = ref(false)
const dialogOpen = ref(false)
const message = ref('')
const error = ref('')
const testResult = ref<NotifyChannelTestResult | null>(null)
const defaultJobs = ref<MyDefaultJobs | null>(null)
const opening = ref(false)
const allOpened = computed(() => Boolean(defaultJobs.value?.jobs.length && defaultJobs.value.jobs.every((job) => job.opened)))

const form = reactive({ name: '我的飞书', webhook_url: '', secret: '', status: 'enabled', remark: '' })

const stateText = computed(() => {
  if (!channel.value) return '未配置'
  if (channel.value.status !== 'enabled') return '已停用'
  if (channel.value.cipher_invalid) return '密钥异常'
  if (!channel.value.has_webhook) return '未配置'
  return '已连接'
})
const stateClass = computed(() => {
  if (stateText.value === '已连接') return 'enabled'
  if (stateText.value === '已停用') return 'disabled'
  return stateText.value === '密钥异常' ? 'error' : 'warning'
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    channel.value = (await api.myNotifyChannel()).channel
    defaultJobs.value = await api.myDefaultJobs()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
}

function openDialog() {
  testResult.value = null
  message.value = ''
  error.value = ''
  Object.assign(form, {
    name: channel.value?.name || '我的飞书',
    webhook_url: '',
    secret: '',
    status: channel.value?.status || 'enabled',
    remark: channel.value?.remark || ''
  })
  dialogOpen.value = true
}

function closeDialog() {
  if (saving.value || testing.value) return
  dialogOpen.value = false
}

async function save() {
  saving.value = true
  error.value = ''
  try {
    channel.value = (
      await api.saveMyNotifyChannel({
        name: form.name,
        webhook_url: form.webhook_url || null,
        secret: form.secret || null,
        status: form.status,
        remark: form.remark || null
      })
    ).channel
    dialogOpen.value = false
    message.value = '已保存。你的定时任务报告、到价提醒和持仓消息都会推到这个群。'
  } catch (err) {
    error.value = err instanceof Error ? err.message : '保存失败'
  } finally {
    saving.value = false
  }
}

async function openJobs() {
  opening.value = true
  message.value = ''
  error.value = ''
  try {
    const result = await api.openMyDefaultJobs()
    defaultJobs.value = result
    message.value = result.created.length ? `已开通：${result.created.join('、')}。可以到“定时任务”页修改时间或关闭。` : '这些任务都已经开通了。'
  } catch (err) {
    error.value = err instanceof Error ? err.message : '开通失败'
  } finally {
    opening.value = false
  }
}

function jobTime(job: MyDefaultJobs['jobs'][number]) {
  if (job.run_time) return `交易日 ${job.run_time}`
  return job.interval_minutes ? `每 ${job.interval_minutes} 分钟` : '—'
}

async function runTest() {
  testing.value = true
  testResult.value = null
  error.value = ''
  try {
    testResult.value = await api.testMyNotifyChannel({ webhook_url: form.webhook_url || null, secret: form.secret || null })
  } catch (err) {
    testResult.value = { ok: false, error: err instanceof Error ? err.message : String(err), latency_ms: 0 }
  } finally {
    testing.value = false
  }
}

onMounted(load)
</script>

<template>
  <section class="model-hub settings-resource-hub">
    <div v-if="message" class="hub-message success" role="status">{{ message }}</div>
    <div v-if="error && !dialogOpen" class="hub-message error" role="alert">{{ error }}</div>

    <section class="hub-section">
      <header class="hub-section-title">
        <div>
          <h2>我的飞书推送</h2>
          <p>你的定时任务报告、到价提醒和持仓消息推到这里。只有你自己看得到这个配置。没配置时不推飞书，只进站内通知。</p>
        </div>
        <button class="quiet-action" type="button" :disabled="loading" @click="load">{{ loading ? '加载中…' : '刷新状态' }}</button>
      </header>

      <div class="provider-grid settings-card-grid">
        <article class="provider-card settings-resource-card notify-channel-card" :class="{ disabled: stateText === '已停用' }">
          <div class="provider-card-top">
            <div class="provider-monogram">飞</div>
            <span class="status-pill" :class="stateClass">{{ stateText }}</span>
          </div>
          <div class="settings-card-copy">
            <h3>{{ channel?.name || '我的飞书' }}</h3>
            <p>{{ channel?.webhook_masked || '还没有填 Webhook 地址' }}</p>
          </div>
          <div class="provider-facts settings-card-facts">
            <span><b>{{ channel?.has_webhook ? '已配置' : '未配置' }}</b> Webhook</span>
            <span><b>{{ channel?.has_secret ? '已签名' : '未签名' }}</b> 安全校验</span>
          </div>
          <div class="settings-card-detail">
            <span>最近成功</span>
            <code>{{ channel?.last_ok_at || '尚无成功记录' }}</code>
          </div>
          <div v-if="channel?.last_error" class="settings-card-alert" :title="channel.last_error">最近错误：{{ channel.last_error }}</div>
          <div class="provider-actions">
            <button class="icon-action" type="button" :disabled="loading" @click="openDialog">{{ channel ? '修改配置' : '开始配置' }}</button>
          </div>
        </article>
      </div>
    </section>

    <section v-if="defaultJobs?.available" class="hub-section">
      <header class="hub-section-title">
        <div>
          <h2>我的定时任务</h2>
          <p>配好飞书后开通。这些任务只看你的持仓，报告推到你自己的群。盘中短提醒和全自动选股不在默认里，需要的话到“定时任务”页单独建。</p>
        </div>
        <button class="quiet-action" type="button" :disabled="opening || allOpened || !defaultJobs.has_holdings" @click="openJobs">
          {{ allOpened ? '已全部开通' : opening ? '开通中…' : '开通这些任务' }}
        </button>
      </header>
      <p v-if="!defaultJobs.has_holdings" class="hub-message error">还没有持仓。先到持仓页录入，不然这些任务跑出来没有内容。</p>
      <ul class="my-job-list">
        <li v-for="job in defaultJobs.jobs" :key="job.kind">
          <div><b>{{ job.name }}</b><small>{{ jobTime(job) }}</small></div>
          <span class="status-pill" :class="job.enabled ? 'enabled' : job.opened ? 'disabled' : 'warning'">{{ job.enabled ? '已开通' : job.opened ? '已关闭' : '未开通' }}</span>
        </li>
      </ul>
    </section>

    <div v-if="dialogOpen" class="model-dialog-backdrop" @mousedown.self="closeDialog">
      <form class="model-dialog" @submit.prevent="save">
        <header class="dialog-header">
          <div><h2>配置我的飞书推送</h2></div>
          <button type="button" title="关闭" @click="closeDialog">×</button>
        </header>
        <div class="dialog-body">
          <div class="friendly-tip">
            <b>怎么拿到 Webhook</b>
            <span>在自己的飞书群里打开群设置 → 群机器人 → 添加机器人 → 自定义机器人，安全设置选“签名校验”，把 Webhook 地址和密钥复制到下面。地址和密钥只加密保存，页面不回显原文，留空表示保留已有值。</span>
          </div>
          <label><span>名称</span><input v-model="form.name" required /></label>
          <label><span>Webhook 地址</span><input v-model="form.webhook_url" type="password" placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/…" /></label>
          <label><span>签名校验密钥</span><input v-model="form.secret" type="password" placeholder="留空保留当前密钥" /></label>
          <label class="friendly-switch">
            <input v-model="form.status" type="checkbox" true-value="enabled" false-value="disabled" />
            <i></i><span><b>启用推送</b><small>停用后定时任务照常执行，结果只进站内通知</small></span>
          </label>
          <label><span>备注</span><textarea v-model="form.remark" rows="2"></textarea></label>
          <div v-if="testResult" class="test-result" :class="{ ok: testResult.ok, fail: !testResult.ok }">
            <strong>{{ testResult.ok ? testResult.message || '测试消息已发送，请到群里确认' : testResult.error || '测试失败' }}</strong>
            <span v-if="testResult.latency_ms">{{ testResult.latency_ms }}ms</span>
          </div>
          <div v-if="error" class="test-result fail"><strong>{{ error }}</strong></div>
        </div>
        <footer>
          <button type="button" :disabled="saving || testing" @click="closeDialog">取消</button><span></span>
          <button type="button" :disabled="testing || saving" @click="runTest">{{ testing ? '测试中…' : '测试推送' }}</button>
          <button class="primary-action" type="submit" :disabled="saving || testing">{{ saving ? '保存中…' : '保存' }}</button>
        </footer>
      </form>
    </div>
  </section>
</template>

<style scoped>
.my-job-list {
  list-style: none;
  margin: 0;
  padding: 0;
  border: 1px solid var(--line);
  border-radius: 8px;
}
.my-job-list li {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--line);
}
.my-job-list li:last-child {
  border-bottom: 0;
}
.my-job-list b {
  font-weight: 500;
}
.my-job-list small {
  display: block;
  margin-top: 2px;
  color: var(--muted);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
</style>
