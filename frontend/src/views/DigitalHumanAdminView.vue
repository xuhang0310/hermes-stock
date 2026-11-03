<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
import type { DigitalHuman, DigitalHumanCapabilities } from '../types'

type AdminDialog = 'profile' | 'capabilities' | null

const list = ref<DigitalHuman[]>([])
const loading = ref(false)
const error = ref('')
const message = ref('')
const dialog = ref<AdminDialog>(null)
const editingId = ref<number | null>(null)
const editForm = ref<DigitalHuman | null>(null)
const saving = ref(false)
const capabilities = ref<Record<number, DigitalHumanCapabilities>>({})
const capabilityId = ref<number | null>(null)
const loadingCaps = ref<number | null>(null)
const togglePending = ref<number | null>(null)

// 这些角色默认偏只读，开启写持仓时明确提示影响范围。
const READONLY_ROLES = ['risk-officer', 'market-sentinel', 'short-term-hunter']

const stats = computed(() => ({
  total: list.value.length,
  enabled: list.value.filter((item) => item.status === 'enabled').length,
  delegatable: list.value.filter((item) => item.delegation_enabled).length,
  feishu: list.value.filter((item) => item.feishu_bot_enabled).length
}))

const capabilityHuman = computed(() => list.value.find((item) => item.id === capabilityId.value) || null)
const activeCapabilities = computed(() => (capabilityId.value ? capabilities.value[capabilityId.value] : null))

async function load() {
  loading.value = true
  error.value = ''
  try {
    list.value = await api.digitalHumans('admin')
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载数字人失败'
  } finally {
    loading.value = false
  }
}

function fullPayload(item: DigitalHuman): Record<string, unknown> {
  return {
    code: item.code,
    name: item.name,
    avatar_tone: item.avatar_tone,
    target_roles: item.target_roles,
    description: item.description,
    system_prompt: item.system_prompt,
    quick_prompts: item.quick_prompts,
    status: item.status,
    sort_order: item.sort_order,
    can_write_positions: item.can_write_positions,
    delegation_enabled: item.delegation_enabled,
    delegation_description: item.delegation_description
  }
}

async function toggleWrite(item: DigitalHuman) {
  if (togglePending.value) return
  if (!item.can_write_positions && READONLY_ROLES.includes(item.code)) {
    if (!window.confirm(`${item.name} 默认承担只读分析职责。开启后它可以在对话中修改持仓记录，确认继续？`)) return
  }
  togglePending.value = item.id
  message.value = ''
  error.value = ''
  const previous = item.can_write_positions
  item.can_write_positions = !previous
  try {
    const saved = await api.saveDigitalHuman(fullPayload(item), item.id)
    const index = list.value.findIndex((row) => row.id === saved.id)
    if (index >= 0) list.value[index] = saved
    delete capabilities.value[item.id]
    message.value = `${saved.name} 已${saved.can_write_positions ? '允许写入持仓' : '切换为只读'}。`
  } catch (err) {
    item.can_write_positions = previous
    error.value = err instanceof Error ? err.message : '切换持仓权限失败'
  } finally {
    togglePending.value = null
  }
}

async function toggleStatus(item: DigitalHuman) {
  if (togglePending.value) return
  togglePending.value = item.id
  message.value = ''
  error.value = ''
  const previous = item.status
  item.status = item.status === 'enabled' ? 'disabled' : 'enabled'
  try {
    const saved = await api.saveDigitalHuman(fullPayload(item), item.id)
    const index = list.value.findIndex((row) => row.id === saved.id)
    if (index >= 0) list.value[index] = saved
    message.value = `${saved.name} 已${saved.status === 'enabled' ? '启用' : '停用'}。`
  } catch (err) {
    item.status = previous
    error.value = err instanceof Error ? err.message : '切换数字人状态失败'
  } finally {
    togglePending.value = null
  }
}

function openEditor(item: DigitalHuman) {
  editingId.value = item.id
  editForm.value = { ...item, quick_prompts: [...item.quick_prompts] }
  error.value = ''
  dialog.value = 'profile'
}

function closeDialog() {
  if (saving.value) return
  dialog.value = null
  editingId.value = null
  editForm.value = null
  capabilityId.value = null
}

function onQuickPromptsInput(event: Event) {
  if (!editForm.value) return
  editForm.value.quick_prompts = (event.target as HTMLTextAreaElement).value
    .split('\n')
    .map((value) => value.trim())
    .filter(Boolean)
}

async function saveProfile() {
  if (!editForm.value) return
  saving.value = true
  error.value = ''
  message.value = ''
  try {
    const saved = await api.saveDigitalHuman(fullPayload(editForm.value), editForm.value.id)
    const index = list.value.findIndex((item) => item.id === saved.id)
    if (index >= 0) list.value[index] = saved
    delete capabilities.value[saved.id]
    message.value = `${saved.name} 的画像与权限已更新。`
    dialog.value = null
    editingId.value = null
    editForm.value = null
  } catch (err) {
    error.value = err instanceof Error ? err.message : '保存数字人失败'
  } finally {
    saving.value = false
  }
}

async function openCapabilities(item: DigitalHuman) {
  capabilityId.value = item.id
  dialog.value = 'capabilities'
  error.value = ''
  if (capabilities.value[item.id]) return
  loadingCaps.value = item.id
  try {
    capabilities.value[item.id] = await api.digitalHumanCapabilities(item.id)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载能力清单失败'
  } finally {
    loadingCaps.value = null
  }
}

function roleLabel(targetRoles: string) {
  return targetRoles === '*' ? '全部角色可用' : targetRoles.split(',').map((item) => item.trim()).filter(Boolean).join(' · ') || '未指定角色'
}

function toolCount(value: DigitalHumanCapabilities | null) {
  if (!value) return 0
  return value.common_tools.length + value.role_tools.length + value.write_tools.length + (value.workspace_tools?.length || 0)
}

onMounted(load)
</script>

<template>
  <section class="model-hub digital-human-admin-hub">
    <header class="model-hub-hero">
      <div>
        <span class="eyebrow">DIGITAL WORKFORCE</span>
        <h1>数字人管理</h1>
        <p>统一维护角色画像、工具能力、数据权限与飞书接入状态。</p>
      </div>
      <button class="primary-action" type="button" :disabled="loading" @click="load"><b>↻</b> {{ loading ? '刷新中…' : '刷新状态' }}</button>
    </header>

    <section v-if="list.length" class="active-model-card settings-overview-card">
      <div class="active-model-orb">DH</div>
      <div>
        <span>数字人团队状态</span>
        <strong>{{ stats.enabled }} 位数字人正在服务</strong>
        <code>工具权限与画像独立配置，写操作仍受用户权限和预览确认双重保护</code>
      </div>
      <i>{{ stats.feishu }} 个飞书机器人已绑定</i>
    </section>
    <section v-else-if="!loading" class="empty-callout">
      <span>还没有数字人角色</span>
      <p>请先完成数字人初始化，再在这里维护画像和能力。</p>
    </section>

    <div class="hub-metrics" aria-label="数字人统计">
      <div><span>角色总数</span><strong>{{ stats.total }}</strong></div>
      <div><span>当前启用</span><strong>{{ stats.enabled }}</strong></div>
      <div><span>可调度成员</span><strong>{{ stats.delegatable }}</strong></div>
      <div><span>飞书绑定</span><strong>{{ stats.feishu }}</strong></div>
    </div>

    <div v-if="message" class="hub-message success" role="status">{{ message }}</div>
    <div v-if="error && !dialog" class="hub-message error" role="alert">{{ error }}</div>

    <section class="hub-section">
      <header class="hub-section-title">
        <div><h2>数字人团队</h2><p>查看职责边界，并按角色调整画像和工具权限</p></div>
      </header>
      <div class="friendly-model-list settings-entity-list digital-human-list">
        <article v-for="item in list" :key="item.id" :class="{ disabled: item.status === 'disabled' }">
          <div class="model-icon entity-avatar">{{ item.name.slice(0, 1) }}</div>
          <div class="model-identity entity-identity">
            <strong>{{ item.name }}</strong>
            <span :title="item.description">{{ item.description || '尚未填写角色描述' }}</span>
            <code>{{ item.code }} · {{ roleLabel(item.target_roles) }}</code>
          </div>
          <div class="entity-badges">
            <span :class="{ accent: item.can_write_positions }">{{ item.can_write_positions ? '可写持仓' : '只读分析' }}</span>
            <span v-if="item.delegation_enabled" class="accent">可被总调度调用</span>
            <span v-if="item.feishu_bot_enabled">飞书已绑定</span>
          </div>
          <span class="model-health"><i></i>{{ item.status === 'enabled' ? '可用' : '停用' }}</span>
          <div class="model-row-actions">
            <button type="button" :disabled="loadingCaps === item.id" @click="openCapabilities(item)">{{ loadingCaps === item.id ? '加载中…' : '能力' }}</button>
            <button type="button" :disabled="!!togglePending" @click="toggleWrite(item)">{{ item.can_write_positions ? '改为只读' : '允许写持仓' }}</button>
            <button type="button" @click="openEditor(item)">编辑</button>
            <button type="button" :disabled="!!togglePending" @click="toggleStatus(item)">{{ item.status === 'enabled' ? '停用' : '启用' }}</button>
          </div>
        </article>
        <div v-if="loading && !list.length" class="friendly-empty">正在加载数字人团队…</div>
      </div>
    </section>

    <div v-if="dialog" class="model-dialog-backdrop" @mousedown.self="closeDialog">
      <form v-if="dialog === 'profile' && editForm" class="model-dialog model-dialog--wide" @submit.prevent="saveProfile">
        <header class="dialog-header">
          <div><small>DIGITAL HUMAN PROFILE</small><h2>编辑 {{ editForm.name }}</h2></div>
          <button type="button" title="关闭" @click="closeDialog">×</button>
        </header>
        <div class="dialog-body digital-human-form">
          <div class="friendly-tip"><b>角色画像与权限</b><span>Code 已被工具和调度引用，因此不可修改；其他配置保存后立即生效。</span></div>
          <div class="form-row">
            <label><span>名称</span><input v-model="editForm.name" required /></label>
            <label><span>Code</span><input :value="editForm.code" disabled /></label>
          </div>
          <div class="form-row">
            <label><span>表达风格</span><input v-model="editForm.avatar_tone" placeholder="例如：理性、审慎、简洁" /></label>
            <label><span>可见角色</span><input v-model="editForm.target_roles" placeholder="* 或 admin,researcher" /></label>
          </div>
          <label><span>角色描述</span><input v-model="editForm.description" /></label>
          <label>
            <span>
              角色提示词
              <button
                v-if="editForm.default_system_prompt && editForm.system_prompt !== editForm.default_system_prompt"
                type="button"
                class="prompt-reset-button"
                @click="editForm.system_prompt = editForm.default_system_prompt"
              >恢复默认</button>
            </span>
            <textarea v-model="editForm.system_prompt" rows="12"></textarea>
            <p class="rhythm-prompt-hint">这就是这个数字人的角色说明：擅长什么、回答要讲到什么，保存后下一次对话生效。查最新价、联网补充、说人话这些通用规则系统会自动加上，不用写进来。</p>
          </label>
          <label><span>快捷问题 <small>每行一条</small></span><textarea :value="editForm.quick_prompts.join('\n')" rows="4" @input="onQuickPromptsInput"></textarea></label>
          <div class="form-row">
            <label><span>显示顺序</span><input v-model.number="editForm.sort_order" type="number" /></label>
            <label><span>状态</span><select v-model="editForm.status"><option value="enabled">启用</option><option value="disabled">停用</option></select></label>
          </div>
          <label class="friendly-switch">
            <input v-model="editForm.can_write_positions" type="checkbox" /><i></i>
            <span><b>允许写入持仓</b><small>聊天时仍要求用户具备 data:write 权限，并完成预览确认</small></span>
          </label>
          <label class="friendly-switch">
            <input v-model="editForm.delegation_enabled" type="checkbox" /><i></i>
            <span><b>允许总调度调用</b><small>启用后，总调度可派发独立研究任务；子任务只读运行，不能继续派发。</small></span>
          </label>
          <label v-if="editForm.delegation_enabled">
            <span>Team 能力说明</span>
            <input v-model="editForm.delegation_description" placeholder="例如：擅长盘中异动、量价关系与市场情绪分析" />
          </label>
          <div v-if="error" class="test-result fail"><strong>{{ error }}</strong></div>
        </div>
        <footer><button type="button" :disabled="saving" @click="closeDialog">取消</button><span></span><button class="primary-action" type="submit" :disabled="saving">{{ saving ? '保存中…' : '保存画像' }}</button></footer>
      </form>

      <section v-else-if="dialog === 'capabilities' && capabilityHuman" class="model-dialog model-dialog--wide">
        <header class="dialog-header">
          <div><small>TOOL CAPABILITIES</small><h2>{{ capabilityHuman.name }} 的能力</h2></div>
          <button type="button" title="关闭" @click="closeDialog">×</button>
        </header>
        <div class="dialog-body capability-dialog-body">
          <div class="friendly-tip">
            <b>{{ activeCapabilities ? `共 ${toolCount(activeCapabilities)} 项工具能力` : '正在读取能力清单' }}</b>
            <span>{{ capabilityHuman.can_write_positions ? '已开放写持仓工具，实际执行仍受用户权限与预览确认保护。' : '当前为只读分析角色，不开放写持仓工具。' }}</span>
          </div>
          <div v-if="loadingCaps === capabilityHuman.id" class="friendly-empty">正在加载工具清单…</div>
          <template v-else-if="activeCapabilities">
            <section class="capability-group">
              <header><h3>公共工具</h3><span>{{ activeCapabilities.common_tools.length }}</span></header>
              <ul><li v-for="tool in activeCapabilities.common_tools" :key="tool.name"><strong>{{ tool.display_name }}</strong><span>{{ tool.description }}</span><code>{{ tool.name }}</code></li></ul>
            </section>
            <section class="capability-group">
              <header><h3>角色专属</h3><span>{{ activeCapabilities.role_tools.length }}</span></header>
              <p v-if="activeCapabilities.role_fallback_code">沿用 {{ activeCapabilities.role_fallback_code }} 工具包</p>
              <ul><li v-for="tool in activeCapabilities.role_tools" :key="tool.name"><strong>{{ tool.display_name }}</strong><span>{{ tool.description }}</span><code>{{ tool.name }}</code></li></ul>
            </section>
            <section v-if="activeCapabilities.write_tools.length" class="capability-group">
              <header><h3>写持仓工具</h3><span>{{ activeCapabilities.write_tools.length }}</span></header>
              <ul><li v-for="tool in activeCapabilities.write_tools" :key="tool.name"><strong>{{ tool.display_name }}</strong><span>{{ tool.description }}</span><code>{{ tool.name }}</code></li></ul>
            </section>
            <section v-if="activeCapabilities.workspace_tools?.length" class="capability-group">
              <header><h3>工作区工具</h3><span>{{ activeCapabilities.workspace_tools.length }}</span></header>
              <ul><li v-for="tool in activeCapabilities.workspace_tools" :key="tool.name"><strong>{{ tool.display_name }}</strong><span>{{ tool.description }}</span><code>{{ tool.name }}</code></li></ul>
            </section>
          </template>
          <div v-if="error" class="test-result fail"><strong>{{ error }}</strong></div>
        </div>
        <footer><span></span><button class="primary-action" type="button" @click="closeDialog">完成</button></footer>
      </section>
    </div>
  </section>
</template>
