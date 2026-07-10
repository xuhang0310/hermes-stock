<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { api } from '../api'
import type { NotifyChannel, NotifyChannelTestResult } from '../types'

const channels = ref<NotifyChannel[]>([])
const editingKey = ref('')
const message = ref('')
const testing = ref(false)
const testResult = ref<NotifyChannelTestResult | null>(null)

const form = reactive({
  name: '',
  webhook_url: '',
  secret: '',
  status: 'enabled',
  remark: ''
})

async function load() {
  try {
    channels.value = await api.notifyChannels()
  } catch (error) {
    message.value = `加载通道失败：${error instanceof Error ? error.message : String(error)}`
    return
  }
  if (!editingKey.value && channels.value[0]) {
    edit(channels.value[0])
  }
}

function edit(row: NotifyChannel) {
  editingKey.value = row.channel_key
  testResult.value = null
  Object.assign(form, {
    name: row.name,
    webhook_url: '',
    secret: '',
    status: row.status,
    remark: row.remark || ''
  })
}

function selectEditingChannel() {
  const row = channels.value.find((item) => item.channel_key === editingKey.value)
  if (row) {
    edit(row)
  }
}

async function save() {
  if (!editingKey.value) return
  message.value = ''
  try {
    await api.saveNotifyChannel(editingKey.value, {
      name: form.name,
      webhook_url: form.webhook_url || null,
      secret: form.secret || null,
      status: form.status,
      remark: form.remark || null
    })
  } catch (error) {
    message.value = `保存失败：${error instanceof Error ? error.message : String(error)}`
    return
  }
  message.value = `${form.name} 已保存。Webhook 与签名密钥已加密入库，下次推送直接生效。`
  form.webhook_url = ''
  form.secret = ''
  await load()
}

async function runTest() {
  if (!editingKey.value && !form.webhook_url) return
  testing.value = true
  testResult.value = null
  message.value = ''
  try {
    testResult.value = await api.testNotifyChannel({
      channel_key: editingKey.value || null,
      webhook_url: form.webhook_url || null,
      secret: form.secret || null
    })
  } catch (error) {
    testResult.value = { ok: false, error: error instanceof Error ? error.message : String(error), latency_ms: 0 }
  } finally {
    testing.value = false
  }
}

async function testRow(item: NotifyChannel) {
  message.value = `正在测试「${item.name}」...`
  testResult.value = null
  try {
    const result = await api.testNotifyChannel({ channel_key: item.channel_key })
    message.value = result.ok
      ? `「${item.name}」测试消息已发送，请到群里确认（${result.latency_ms}ms）`
      : `「${item.name}」测试失败：${result.error}`
  } catch (error) {
    message.value = `「${item.name}」测试失败：${error instanceof Error ? error.message : String(error)}`
  }
  await load()
}

function purposeLabel(purpose: string) {
  if (purpose === 'alert') return '告警群 · 零噪音'
  if (purpose === 'daily') return '日报群 · 可堆积'
  return purpose
}

onMounted(load)
</script>

<template>
  <section class="management-grid">
    <form class="editor-panel" @submit.prevent="save">
      <header>
        <h2>通知通道设置</h2>
      </header>
      <label>
        <span>当前通道</span>
        <select v-model="editingKey" @change="selectEditingChannel">
          <option v-for="item in channels" :key="item.channel_key" :value="item.channel_key">
            {{ item.name }}（{{ purposeLabel(item.purpose) }}）
          </option>
        </select>
      </label>
      <label>
        <span>显示名称</span>
        <input v-model="form.name" required />
      </label>
      <label>
        <span>Webhook 地址</span>
        <input
          v-model="form.webhook_url"
          type="password"
          placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/…，留空保留已保存值"
        />
      </label>
      <label>
        <span>签名密钥（可选）</span>
        <input v-model="form.secret" type="password" placeholder="机器人安全设置中的签名校验密钥，留空保留已保存值" />
      </label>
      <label>
        <span>状态</span>
        <select v-model="form.status">
          <option value="enabled">enabled</option>
          <option value="disabled">disabled</option>
        </select>
      </label>
      <label>
        <span>备注</span>
        <textarea v-model="form.remark" rows="3"></textarea>
      </label>
      <button type="submit" :disabled="!editingKey">保存通道</button>
      <button type="button" :disabled="testing || (!editingKey && !form.webhook_url)" @click="runTest">
        {{ testing ? '测试中…' : '测试推送' }}
      </button>
      <p class="form-message">{{ message }}</p>
      <p v-if="testResult" class="form-message">
        <template v-if="testResult.ok">✅ {{ testResult.message }}（{{ testResult.latency_ms }}ms）</template>
        <template v-else>❌ {{ testResult.error }}</template>
      </p>
    </form>

    <section class="page-section">
      <header class="section-header">
        <div>
          <h2>飞书推送通道</h2>
          <p>
            告警群只收 L0 警报与系统故障（零噪音纪律），日报群收简报、快评与晚间日报。
            Webhook 与签名密钥加密存储，接口只回传掩码。
          </p>
        </div>
      </header>
      <div class="table-wrap">
        <table class="list-table">
          <thead>
            <tr>
              <th>通道</th>
              <th>用途</th>
              <th>Webhook</th>
              <th>签名</th>
              <th>状态</th>
              <th>最近成功</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="!channels.length">
              <td colspan="7" class="empty-cell">暂无记录</td>
            </tr>
            <tr v-for="item in channels" :key="item.channel_key" :class="{ 'row-active': item.channel_key === editingKey }">
              <td>
                <span class="job-dot" :class="item.status === 'enabled' ? 'on' : 'off'" :title="item.status === 'enabled' ? '已启用' : '已停用'"></span>
                {{ item.name }}
                <span v-if="item.last_error" class="err-flag" :title="item.last_error">!</span>
              </td>
              <td>{{ purposeLabel(item.purpose) }}</td>
              <td class="nowrap">{{ item.webhook_masked || '未配置' }}</td>
              <td>{{ item.has_secret ? '已配置' : '未配置' }}</td>
              <td><span class="job-status" :class="item.status === 'enabled' ? 'success' : 'none'">{{ item.status }}</span></td>
              <td class="nowrap">{{ item.last_ok_at || '-' }}</td>
              <td>
                <div class="cell-actions">
                  <button type="button" @click="edit(item)">编辑</button>
                  <button type="button" @click="testRow(item)">测试</button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </section>
</template>
