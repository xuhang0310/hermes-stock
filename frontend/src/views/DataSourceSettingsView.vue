<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { api } from '../api'
import type { DataSourceConfig } from '../types'

const sources = ref<DataSourceConfig[]>([])
const editingProvider = ref('')
const message = ref('')
const error = ref('')
const loading = ref(false)
const busy = ref(false)
const dialogOpen = ref(false)

const form = reactive({
  display_name: '',
  provider_type: 'market_data',
  base_url: '',
  api_key: '',
  additional_api_key: '',
  model: '',
  status: 'enabled',
  remark: ''
})

const stats = computed(() => ({
  total: sources.value.length,
  enabled: sources.value.filter((item) => item.status === 'enabled').length,
  ready: sources.value.filter((item) => item.status === 'enabled' && item.key_source !== 'missing').length,
  secured: sources.value.filter((item) => item.has_api_key).length
}))

const editingSource = computed(() => sources.value.find((item) => item.provider === editingProvider.value) || null)

async function load() {
  loading.value = true
  error.value = ''
  try {
    sources.value = await api.dataSources()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载数据源失败'
  } finally {
    loading.value = false
  }
}

function edit(row: DataSourceConfig) {
  editingProvider.value = row.provider
  message.value = ''
  error.value = ''
  Object.assign(form, {
    display_name: row.display_name,
    provider_type: row.provider_type,
    base_url: row.base_url || '',
    api_key: '',
    additional_api_key: '',
    model: row.model || '',
    status: row.status,
    remark: row.remark || ''
  })
  dialogOpen.value = true
}

function openFirstSource() {
  if (sources.value[0]) edit(sources.value[0])
}

function closeDialog() {
  if (busy.value) return
  dialogOpen.value = false
}

async function save() {
  if (!editingProvider.value) return
  message.value = ''
  error.value = ''
  busy.value = true
  try {
    await api.saveDataSource(editingProvider.value, {
      ...form,
      api_key: form.api_key || null,
      base_url: form.base_url || null,
      model: form.model || null,
      remark: form.remark || null
    })
    const savedName = form.display_name
    form.api_key = ''
    form.additional_api_key = ''
    await load()
    dialogOpen.value = false
    message.value = `${savedName} 已保存，新的配置会在下一次工具调用时生效。`
  } catch (err) {
    error.value = err instanceof Error ? err.message : '保存数据源失败'
  } finally {
    busy.value = false
  }
}

function keySourceLabel(source: string) {
  if (source === 'database+environment') return '数据库 + 环境变量'
  if (source === 'database') return '加密数据库'
  if (source === 'environment') return '环境变量'
  return '尚未配置'
}

function typeLabel(type: string) {
  if (type === 'market_data') return '市场行情'
  if (type === 'search') return '新闻搜索'
  if (type === 'embedding') return '向量嵌入'
  if (type === 'rerank') return '结果重排'
  return type
}

function sourceDescription(item: DataSourceConfig) {
  if (item.provider_type === 'market_data') return '提供 A 股行情、历史日线与基础证券数据'
  if (item.provider_type === 'search') return '为问股与报告补充实时新闻和公开信息'
  if (item.provider_type === 'embedding') return '将知识库与长期记忆转换为可检索向量'
  if (item.provider_type === 'rerank') return '对召回内容进行相关性重排，提升引用质量'
  return item.remark || '外部数据与能力服务'
}

function providerMark(item: DataSourceConfig) {
  if (item.provider_type === 'market_data') return '行'
  if (item.provider_type === 'search') return '搜'
  if (item.provider_type === 'embedding') return '向'
  if (item.provider_type === 'rerank') return '排'
  return item.display_name.slice(0, 1).toUpperCase()
}

function sourceState(item: DataSourceConfig) {
  if (item.status !== 'enabled') return '已停用'
  if (item.key_source === 'missing') return '待配置'
  return '运行中'
}

function sourceStateClass(item: DataSourceConfig) {
  if (item.status !== 'enabled') return 'disabled'
  if (item.key_source === 'missing') return 'warning'
  return 'enabled'
}

onMounted(load)
</script>

<template>
  <section class="model-hub settings-resource-hub">
    <header class="model-hub-hero">
      <div>
        <span class="eyebrow">DATA CONNECTIONS</span>
        <h1>数据源</h1>
        <p>集中管理行情、搜索、向量与重排服务，配置修改无需重启应用。</p>
      </div>
      <button class="primary-action" type="button" :disabled="loading || !sources.length" @click="openFirstSource">
        <b>⚙</b> 配置数据源
      </button>
    </header>

    <section v-if="sources.length" class="active-model-card settings-overview-card">
      <div class="active-model-orb">DS</div>
      <div>
        <span>数据链路状态</span>
        <strong>{{ stats.ready }} 个数据源已就绪</strong>
        <code>行情、联网搜索、知识库向量与召回重排统一在此维护</code>
      </div>
      <i>{{ stats.enabled }} / {{ stats.total }} 已启用</i>
    </section>
    <section v-else-if="!loading" class="empty-callout">
      <span>还没有可配置的数据源</span>
      <p>服务端尚未返回数据源定义，请检查初始化状态。</p>
    </section>

    <div v-if="message" class="hub-message success" role="status">{{ message }}</div>
    <div v-if="error" class="hub-message error" role="alert">{{ error }}</div>

    <section class="hub-section">
      <header class="hub-section-title">
        <div>
          <h2>服务连接</h2>
          <p>{{ stats.ready }} 个可用，{{ stats.secured }} 个密钥已安全保存</p>
        </div>
        <button class="quiet-action" type="button" :disabled="loading" @click="load">{{ loading ? '加载中…' : '刷新状态' }}</button>
      </header>

      <div class="provider-grid settings-card-grid">
        <article
          v-for="item in sources"
          :key="item.provider"
          class="provider-card settings-resource-card"
          :class="{ disabled: item.status === 'disabled' }"
        >
          <div class="provider-card-top">
            <div class="provider-monogram">{{ providerMark(item) }}</div>
            <span class="status-pill" :class="sourceStateClass(item)">{{ sourceState(item) }}</span>
          </div>
          <div class="settings-card-copy">
            <h3>{{ item.display_name }}</h3>
            <p>{{ sourceDescription(item) }}</p>
          </div>
          <div class="provider-facts settings-card-facts">
            <span><b>{{ typeLabel(item.provider_type) }}</b> 类型</span>
            <span><b>{{ item.provider === 'tavily' ? `${item.key_count} 个` : keySourceLabel(item.key_source) }}</b> 密钥</span>
          </div>
          <div class="settings-card-detail">
            <span>{{ item.model ? '当前模型' : '接口地址' }}</span>
            <code :title="item.model || item.base_url || ''">{{ item.model || item.base_url || '未填写' }}</code>
          </div>
          <div class="provider-actions">
            <button type="button" @click="edit(item)">配置连接</button>
            <small>{{ item.updated_at ? `更新于 ${item.updated_at}` : '尚未更新' }}</small>
          </div>
        </article>
      </div>
    </section>

    <div v-if="dialogOpen && editingSource" class="model-dialog-backdrop" @mousedown.self="closeDialog">
      <form class="model-dialog" @submit.prevent="save">
        <header class="dialog-header">
          <div><small>DATA SOURCE</small><h2>配置 {{ editingSource.display_name }}</h2></div>
          <button type="button" title="关闭" @click="closeDialog">×</button>
        </header>
        <div class="dialog-body">
          <div class="friendly-tip">
            <b>{{ sourceDescription(editingSource) }}</b>
            <span v-if="editingSource.provider === 'tavily'">当前已配置 {{ editingSource.key_count }} 个 Key。追加备用 Key 不会覆盖现有配置，额度不足时会自动切换。</span>
            <span v-else>API Key 会安全保存。留空表示保留现有密钥，数据库配置优先于环境变量。</span>
          </div>
          <div class="form-row">
            <label><span>显示名称</span><input v-model="form.display_name" required /></label>
            <label><span>服务类型</span><select v-model="form.provider_type"><option value="market_data">市场行情</option><option value="search">新闻搜索</option><option value="embedding">向量嵌入</option><option value="rerank">结果重排</option></select></label>
          </div>
          <label v-if="editingSource.provider === 'tavily'"><span>新增备用 API Key</span><input v-model="form.additional_api_key" type="password" autocomplete="new-password" placeholder="粘贴新 Key；留空则不修改现有 Key 池" /><small>支持重复追加，系统会自动去重。</small></label>
          <label v-else><span>API Key / Token</span><input v-model="form.api_key" type="password" placeholder="留空保留已保存值或继续使用环境变量" /></label>
          <label><span>接口地址（Base URL）</span><input v-model="form.base_url" placeholder="https://api.example.com/v1" /></label>
          <label><span>模型名称 <small>向量和重排服务需要填写</small></span><input v-model="form.model" placeholder="例如 BAAI/bge-m3、text-embedding-v3" /></label>
          <label class="friendly-switch">
            <input v-model="form.status" type="checkbox" true-value="enabled" false-value="disabled" />
            <i></i><span><b>启用这个数据源</b><small>关闭后相关工具将不再调用此服务</small></span>
          </label>
          <label><span>备注</span><textarea v-model="form.remark" rows="3"></textarea></label>
          <div v-if="error" class="test-result fail"><strong>{{ error }}</strong></div>
        </div>
        <footer>
          <button type="button" :disabled="busy" @click="closeDialog">取消</button><span></span>
          <button class="primary-action" type="submit" :disabled="busy">{{ busy ? '保存中…' : '保存数据源' }}</button>
        </footer>
      </form>
    </div>
  </section>
</template>
