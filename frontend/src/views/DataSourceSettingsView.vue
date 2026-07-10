<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { api } from '../api'
import type { DataSourceConfig } from '../types'

const sources = ref<DataSourceConfig[]>([])
const editingProvider = ref('')
const message = ref('')

const form = reactive({
  display_name: '',
  provider_type: 'market_data',
  base_url: '',
  api_key: '',
  status: 'enabled',
  remark: ''
})

async function load() {
  sources.value = await api.dataSources()
  if (!editingProvider.value && sources.value[0]) {
    edit(sources.value[0])
  }
}

function edit(row: DataSourceConfig) {
  editingProvider.value = row.provider
  Object.assign(form, {
    display_name: row.display_name,
    provider_type: row.provider_type,
    base_url: row.base_url || '',
    api_key: '',
    status: row.status,
    remark: row.remark || ''
  })
}

function selectEditingProvider() {
  const row = sources.value.find((item) => item.provider === editingProvider.value)
  if (row) {
    edit(row)
  }
}

async function save() {
  if (!editingProvider.value) return
  message.value = ''
  await api.saveDataSource(editingProvider.value, {
    ...form,
    api_key: form.api_key || null,
    base_url: form.base_url || null,
    remark: form.remark || null
  })
  message.value = `${form.display_name} 已保存，下一次问股工具调用会直接生效`
  form.api_key = ''
  await load()
}

function keySourceLabel(source: string) {
  if (source === 'database') return '数据库'
  if (source === 'environment') return '环境变量'
  return '未配置'
}

onMounted(load)
</script>

<template>
  <section class="management-grid">
    <form class="editor-panel" @submit.prevent="save">
      <header>
        <h2>数据源设置</h2>
      </header>
      <label>
        <span>当前数据源</span>
        <select v-model="editingProvider" @change="selectEditingProvider">
          <option v-for="item in sources" :key="item.provider" :value="item.provider">
            {{ item.display_name }}
          </option>
        </select>
      </label>
      <label>
        <span>显示名称</span>
        <input v-model="form.display_name" required />
      </label>
      <label>
        <span>类型</span>
        <select v-model="form.provider_type">
          <option value="market_data">market_data</option>
          <option value="search">search</option>
        </select>
      </label>
      <label>
        <span>Base URL</span>
        <input v-model="form.base_url" placeholder="https://api.tushare.pro" />
      </label>
      <label>
        <span>API Key / Token</span>
        <input v-model="form.api_key" type="password" placeholder="留空则保留数据库 Key 或继续使用环境变量" />
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
        <textarea v-model="form.remark" rows="4"></textarea>
      </label>
      <button type="submit" :disabled="!editingProvider">保存数据源</button>
      <p class="form-message">{{ message }}</p>
    </form>

    <section class="page-section">
      <header class="section-header">
        <div>
          <h2>问股数据源</h2>
          <p>TuShare 负责行情和日线，Tavily Search 负责新闻与消息面。Key 优先读取数据库，缺省时回退环境变量。</p>
        </div>
      </header>
      <div class="table-wrap">
        <table class="list-table">
          <thead>
            <tr>
              <th>数据源</th>
              <th>类型</th>
              <th>地址</th>
              <th>Key</th>
              <th>来源</th>
              <th>状态</th>
              <th>更新</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="!sources.length">
              <td colspan="8" class="empty-cell">暂无记录</td>
            </tr>
            <tr v-for="item in sources" :key="item.provider" :class="{ 'row-active': item.provider === editingProvider }">
              <td>{{ item.display_name }}</td>
              <td>{{ item.provider_type }}</td>
              <td class="nowrap">{{ item.base_url || '-' }}</td>
              <td>{{ item.api_key_masked || '-' }}</td>
              <td>{{ keySourceLabel(item.key_source) }}</td>
              <td><span class="job-status" :class="item.status === 'enabled' ? 'success' : 'none'">{{ item.status }}</span></td>
              <td class="nowrap">{{ item.updated_at || '-' }}</td>
              <td>
                <div class="cell-actions">
                  <button type="button" @click="edit(item)">编辑</button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </section>
</template>
