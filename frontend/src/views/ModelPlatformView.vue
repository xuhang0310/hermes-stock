<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { api } from '../api'
import type { ModelPlatform } from '../types'

const platforms = ref<ModelPlatform[]>([])
const message = ref('')
const editingId = ref<number | undefined>()

const form = reactive({
  name: '',
  provider_type: 'OpenAI compatible',
  base_url: '',
  api_key: '',
  status: 'enabled',
  remark: ''
})

function resetForm() {
  editingId.value = undefined
  Object.assign(form, {
    name: '',
    provider_type: 'OpenAI compatible',
    base_url: '',
    api_key: '',
    status: 'enabled',
    remark: ''
  })
}

async function load() {
  platforms.value = await api.modelPlatforms()
}

function edit(row: ModelPlatform) {
  editingId.value = row.id
  Object.assign(form, {
    name: row.name,
    provider_type: row.provider_type,
    base_url: row.base_url,
    api_key: '',
    status: row.status,
    remark: row.remark || ''
  })
}

async function save() {
  message.value = ''
  await api.saveModelPlatform(
    {
      ...form,
      api_key: form.api_key || null,
      remark: form.remark || null
    },
    editingId.value
  )
  message.value = editingId.value ? '模型平台已更新' : '模型平台已创建'
  resetForm()
  await load()
}

onMounted(load)
</script>

<template>
  <section class="management-grid">
    <form class="editor-panel" @submit.prevent="save">
      <header>
        <h2>{{ editingId ? '编辑模型平台' : '新增模型平台' }}</h2>
        <button type="button" @click="resetForm">清空</button>
      </header>
      <label>
        <span>平台名称</span>
        <input v-model="form.name" required />
      </label>
      <label>
        <span>平台类型</span>
        <input v-model="form.provider_type" />
      </label>
      <label>
        <span>Base URL</span>
        <input v-model="form.base_url" required placeholder="https://.../v1" />
      </label>
      <label>
        <span>API Key</span>
        <input v-model="form.api_key" type="password" placeholder="留空则保留原 Key" />
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
      <button type="submit">保存平台</button>
      <p class="form-message">{{ message }}</p>
    </form>

    <section class="page-section">
      <header class="section-header">
        <div>
          <h2>模型平台</h2>
          <p>平台保存公共 Base URL 与 Key，模型配置可复用平台能力。</p>
        </div>
      </header>
      <div class="table-wrap">
        <table class="list-table">
          <thead>
            <tr>
              <th>名称</th>
              <th>类型</th>
              <th>地址</th>
              <th>Key</th>
              <th>状态</th>
              <th>更新</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="!platforms.length">
              <td colspan="7" class="empty-cell">暂无记录</td>
            </tr>
            <tr v-for="item in platforms" :key="item.id" :class="{ 'row-active': item.id === editingId }">
              <td>{{ item.name }}</td>
              <td>{{ item.provider_type }}</td>
              <td class="nowrap">{{ item.base_url }}</td>
              <td>{{ item.api_key_masked || '-' }}</td>
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
