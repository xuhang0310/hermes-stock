<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
import type { DigitalHuman, DigitalHumanCapabilities } from '../types'

const list = ref<DigitalHuman[]>([])
const loading = ref(false)
const error = ref('')
const editingId = ref<number | null>(null)
const editForm = ref<DigitalHuman | null>(null)
const saving = ref(false)
const capabilities = ref<Record<number, DigitalHumanCapabilities>>({})
const loadingCaps = ref<number | null>(null)
const togglePending = ref<number | null>(null)

// 设计上偏只读分析的角色，开写持仓时弹一次知情确认
const READONLY_ROLES = ['risk-officer', 'market-sentinel', 'short-term-hunter']

const stats = computed(() => ({
  total: list.value.length,
  enabled: list.value.filter((d) => d.status === 'enabled').length,
  canWrite: list.value.filter((d) => d.can_write_positions).length,
  feishu: list.value.filter((d) => d.feishu_bot_enabled).length,
}))

async function load() {
  loading.value = true
  error.value = ''
  try {
    list.value = await api.digitalHumans()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
}

function fullPayload(d: DigitalHuman): Record<string, unknown> {
  // PUT 全量：回填所有字段 + 翻转后的 can_write_positions，避免误清 system_prompt
  return {
    code: d.code,
    name: d.name,
    avatar_tone: d.avatar_tone,
    target_roles: d.target_roles,
    description: d.description,
    system_prompt: d.system_prompt,
    quick_prompts: d.quick_prompts,
    status: d.status,
    sort_order: d.sort_order,
    can_write_positions: d.can_write_positions,
  }
}

async function toggleWrite(d: DigitalHuman) {
  if (togglePending.value) return
  if (!d.can_write_positions && READONLY_ROLES.includes(d.code)) {
    if (!window.confirm(`${d.name} 设计上偏只读分析，开启写持仓会让它在对话里能改持仓记录，确认开启？`)) return
  }
  togglePending.value = d.id
  const old = d.can_write_positions
  d.can_write_positions = !old // 乐观翻转
  try {
    await api.saveDigitalHuman(fullPayload(d), d.id)
  } catch (err) {
    d.can_write_positions = old // 回滚
    error.value = err instanceof Error ? err.message : '切换失败'
  } finally {
    togglePending.value = null
  }
}

function openEditor(d: DigitalHuman) {
  editingId.value = d.id
  editForm.value = { ...d, quick_prompts: [...d.quick_prompts] }
}

function closeEditor() {
  editingId.value = null
  editForm.value = null
}

function onQuickPromptsInput(event: Event) {
  if (!editForm.value) return
  editForm.value.quick_prompts = (event.target as HTMLTextAreaElement).value
    .split('\n')
    .map((s) => s.trim())
    .filter(Boolean)
}

async function saveProfile() {
  if (!editForm.value) return
  saving.value = true
  error.value = ''
  try {
    const saved = await api.saveDigitalHuman(fullPayload(editForm.value), editForm.value.id ?? undefined)
    const idx = list.value.findIndex((x) => x.id === saved.id)
    if (idx >= 0) list.value[idx] = saved
    closeEditor()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '保存失败'
  } finally {
    saving.value = false
  }
}

async function loadCapabilities(d: DigitalHuman) {
  if (capabilities.value[d.id]) {
    delete capabilities.value[d.id] // 再次点击收起
    return
  }
  loadingCaps.value = d.id
  try {
    capabilities.value[d.id] = await api.digitalHumanCapabilities(d.id)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载能力失败'
  } finally {
    loadingCaps.value = null
  }
}

onMounted(load)
</script>

<template>
  <section class="page-section">
    <header class="section-header">
      <div>
        <h2>数字人管理</h2>
        <p>数字人画像与能力开关（写持仓等）。写持仓开关只是第一道门，聊天时仍要求用户具备 data:write 且经预览确认。</p>
      </div>
      <button type="button" :disabled="loading" @click="load">{{ loading ? '加载中' : '刷新' }}</button>
    </header>

    <div class="stat-row">
      <span>数字人 {{ stats.total }}</span>
      <span>启用 {{ stats.enabled }}</span>
      <span>可写持仓 {{ stats.canWrite }}</span>
      <span>飞书绑定 {{ stats.feishu }}</span>
    </div>

    <p v-if="error" class="error-text">{{ error }}</p>

    <div class="dh-table">
      <div class="dh-head">
        <span>名称</span>
        <span>code</span>
        <span>状态</span>
        <span>写持仓</span>
        <span>飞书</span>
        <span>操作</span>
      </div>
      <div v-for="d in list" :key="d.id" class="dh-row">
        <span><strong>{{ d.name }}</strong></span>
        <span class="mono">{{ d.code }}</span>
        <span>{{ d.status === 'enabled' ? '启用' : '停用' }}</span>
        <span>
          <button
            type="button"
            class="toggle"
            :class="{ on: d.can_write_positions }"
            :disabled="!!togglePending"
            :title="d.can_write_positions ? '已开启写持仓（仍需用户 data:write + 预览确认）' : '点击开启写持仓'"
            @click="toggleWrite(d)"
          >{{ d.can_write_positions ? '✓ 开' : '✗ 关' }}</button>
        </span>
        <span>{{ d.feishu_bot_enabled ? '✅' : '—' }}</span>
        <span class="cell-actions">
          <button type="button" @click="openEditor(d)">编辑</button>
          <button type="button" :disabled="loadingCaps === d.id" @click="loadCapabilities(d)">
            {{ capabilities[d.id] ? '收起能力' : '能力' }}
          </button>
        </span>

        <section v-if="editingId === d.id && editForm" class="dh-edit">
          <h3>编辑 {{ editForm.name }}</h3>
          <label>名称<input v-model="editForm.name" /></label>
          <label>code（已存在记录置灰，改坏会断工具/调度映射）
            <input :value="editForm.code" disabled />
          </label>
          <label>风格<input v-model="editForm.avatar_tone" /></label>
          <label>目标角色<input v-model="editForm.target_roles" /></label>
          <label>描述<input v-model="editForm.description" /></label>
          <label>系统提示词<textarea v-model="editForm.system_prompt" rows="6" /></label>
          <label>快捷问题（每行一条）<textarea :value="editForm.quick_prompts.join('\n')" @input="onQuickPromptsInput" rows="4" /></label>
          <label>排序<input type="number" v-model.number="editForm.sort_order" /></label>
          <label>状态<select v-model="editForm.status"><option value="enabled">enabled</option><option value="disabled">disabled</option></select></label>
          <label class="checkbox-line"><input type="checkbox" v-model="editForm.can_write_positions" /> 允许写持仓（仍需用户 data:write + 预览确认）</label>
          <div class="cell-actions">
            <button type="button" :disabled="saving" @click="saveProfile">{{ saving ? '保存中' : '保存' }}</button>
            <button type="button" @click="closeEditor">取消</button>
          </div>
        </section>

        <section v-if="capabilities[d.id]" class="dh-cap">
          <h4>工具清单</h4>
          <p v-if="capabilities[d.id].can_write_positions" class="hint">写持仓工具已开启（仍受 data:write + 预览确认门控）</p>
          <div class="cap-group">
            <h5>公共工具</h5>
            <ul><li v-for="t in capabilities[d.id].common_tools" :key="t.name"><strong>{{ t.display_name }}</strong> — {{ t.description }}</li></ul>
          </div>
          <div class="cap-group">
            <h5>角色专属</h5>
            <ul><li v-for="t in capabilities[d.id].role_tools" :key="t.name"><strong>{{ t.display_name }}</strong> — {{ t.description }}</li></ul>
          </div>
          <div v-if="capabilities[d.id].write_tools.length" class="cap-group">
            <h5>写持仓工具</h5>
            <ul><li v-for="t in capabilities[d.id].write_tools" :key="t.name"><strong>{{ t.display_name }}</strong> — {{ t.description }}</li></ul>
          </div>
        </section>
      </div>
    </div>
  </section>
</template>

<style scoped>
.stat-row { display: flex; gap: 16px; padding: 8px 0; font-size: 13px; color: var(--muted); }
.dh-table { display: grid; gap: 2px; }
.dh-head, .dh-row { display: grid; grid-template-columns: 1.4fr 1fr 0.6fr 0.7fr 0.5fr 1.1fr; gap: 8px; align-items: start; padding: 8px; }
.dh-head { font-size: 12px; color: var(--muted); border-bottom: 1px solid var(--border); }
.dh-row { border-bottom: 1px solid var(--border); }
.mono { font-family: monospace; font-size: 12px; }
.toggle { padding: 2px 10px; border-radius: 6px; border: 1px solid var(--border); background: transparent; cursor: pointer; font-size: 12px; }
.toggle.on { border-color: #6db985; background: rgba(109, 185, 133, 0.14); color: #6db985; }
.toggle:disabled { cursor: not-allowed; opacity: 0.5; }
.cell-actions { display: flex; gap: 6px; }
.cell-actions button { font-size: 12px; padding: 2px 8px; }
.dh-edit, .dh-cap { grid-column: 1 / -1; display: grid; gap: 8px; padding: 12px; background: var(--panel, rgba(12,12,13,0.5)); border-radius: 8px; margin-top: 6px; }
.dh-edit h3, .dh-cap h4 { margin: 0 0 4px; }
.dh-edit label { display: grid; gap: 4px; font-size: 12px; }
.dh-edit input, .dh-edit textarea, .dh-edit select { width: 100%; }
.checkbox-line { flex-direction: row; align-items: center; gap: 6px; }
.cap-group h5 { margin: 8px 0 4px; font-size: 12px; color: var(--muted); }
.cap-group ul { margin: 0; padding-left: 16px; font-size: 12px; }
.cap-group li { margin: 2px 0; }
.hint { font-size: 12px; color: #d5b46a; }
.error-text { color: #e06c6c; font-size: 13px; }
</style>
