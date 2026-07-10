<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { api } from '../api'
import type { ModelConfig, ModelPlatform } from '../types'

const configs = ref<ModelConfig[]>([])
const platforms = ref<ModelPlatform[]>([])
const editingId = ref<number | undefined>()
const message = ref('')

const form = reactive({
  platform_id: null as number | null,
  display_name: '',
  provider_name: 'custom',
  base_url: '',
  api_key: '',
  model_name: '',
  is_active: false,
  status: 'enabled',
  temperature: 0.2,
  max_steps: 6,
  remark: ''
})

function resetForm() {
  editingId.value = undefined
  Object.assign(form, {
    platform_id: null,
    display_name: '',
    provider_name: 'custom',
    base_url: '',
    api_key: '',
    model_name: '',
    is_active: false,
    status: 'enabled',
    temperature: 0.2,
    max_steps: 6,
    remark: ''
  })
}

async function load() {
  const [configPayload, platformPayload] = await Promise.all([api.modelConfigs(), api.modelPlatforms()])
  configs.value = configPayload
  platforms.value = platformPayload
}

function edit(row: ModelConfig) {
  editingId.value = row.id
  Object.assign(form, {
    platform_id: row.platform_id,
    display_name: row.display_name,
    provider_name: row.provider_name,
    base_url: row.base_url,
    api_key: '',
    model_name: row.model_name,
    is_active: row.is_active,
    status: row.status,
    temperature: row.temperature,
    max_steps: row.max_steps,
    remark: row.remark || ''
  })
}

function applyPlatform() {
  const platform = platforms.value.find((item) => item.id === form.platform_id)
  if (!platform) return
  form.provider_name = platform.name
  form.base_url = platform.base_url
}

async function save() {
  message.value = ''
  await api.saveModelConfig(
    {
      ...form,
      api_key: form.api_key || null,
      remark: form.remark || null
    },
    editingId.value
  )
  message.value = editingId.value ? '模型配置已更新' : '模型配置已创建'
  resetForm()
  await load()
}

async function activate(id: number) {
  await api.activateModelConfig(id)
  message.value = '默认模型已切换'
  await load()
}

onMounted(load)
</script>

<template>
  <section class="management-grid">
    <form class="editor-panel" @submit.prevent="save">
      <header>
        <h2>{{ editingId ? '编辑模型配置' : '新增模型配置' }}</h2>
        <button type="button" @click="resetForm">清空</button>
      </header>
      <label>
        <span>所属平台</span>
        <select v-model="form.platform_id" @change="applyPlatform">
          <option :value="null">不绑定平台</option>
          <option v-for="item in platforms" :key="item.id" :value="item.id">{{ item.name }}</option>
        </select>
      </label>
      <label>
        <span>显示名称</span>
        <input v-model="form.display_name" required />
      </label>
      <label>
        <span>模型名称</span>
        <input v-model="form.model_name" required placeholder="qwen3.7-plus / deepseek-v4-pro" />
      </label>
      <label>
        <span>Provider</span>
        <input v-model="form.provider_name" />
      </label>
      <label>
        <span>Base URL</span>
        <input v-model="form.base_url" required />
      </label>
      <label>
        <span>API Key</span>
        <input v-model="form.api_key" type="password" placeholder="留空则使用平台 Key 或保留原 Key" />
      </label>
      <div class="form-row">
        <label>
          <span>温度</span>
          <input v-model.number="form.temperature" type="number" min="0" max="2" step="0.1" />
        </label>
        <label>
          <span>ReAct步数</span>
          <input v-model.number="form.max_steps" type="number" min="1" max="20" />
        </label>
      </div>
      <label>
        <span>状态</span>
        <select v-model="form.status">
          <option value="enabled">enabled</option>
          <option value="disabled">disabled</option>
        </select>
      </label>
      <label class="checkbox-line">
        <input v-model="form.is_active" type="checkbox" />
        <span>设为当前默认模型</span>
      </label>
      <label>
        <span>备注</span>
        <textarea v-model="form.remark" rows="3"></textarea>
      </label>
      <button type="submit">保存配置</button>
      <p class="form-message">{{ message }}</p>
    </form>

    <section class="page-section">
      <header class="section-header">
        <div>
          <h2>模型配置</h2>
          <p>数字人中心可按会话切换模型；默认模型用于未指定模型的 ReAct 请求。</p>
        </div>
      </header>
      <div class="table-wrap">
        <table class="list-table">
          <thead>
            <tr>
              <th>名称</th>
              <th>平台</th>
              <th>模型</th>
              <th>默认</th>
              <th>Key</th>
              <th>状态</th>
              <th>步数</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="!configs.length">
              <td colspan="8" class="empty-cell">暂无记录</td>
            </tr>
            <tr v-for="item in configs" :key="item.id" :class="{ 'row-active': item.id === editingId }">
              <td>{{ item.display_name }}</td>
              <td>{{ item.platform_name || item.provider_name }}</td>
              <td class="nowrap">{{ item.model_name }}</td>
              <td>{{ item.is_active ? '★ 默认' : '-' }}</td>
              <td>{{ item.api_key_masked || '-' }}</td>
              <td><span class="job-status" :class="item.status === 'enabled' ? 'success' : 'none'">{{ item.status }}</span></td>
              <td>{{ item.max_steps }}</td>
              <td>
                <div class="cell-actions">
                  <button type="button" @click="edit(item)">编辑</button>
                  <button v-if="!item.is_active" type="button" @click="activate(item.id)">设默认</button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </section>
</template>
