<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import AgentQueue from '../components/AgentQueue.vue'
import DataSourceSettingsView from './DataSourceSettingsView.vue'
import DigitalHumanAdminView from './DigitalHumanAdminView.vue'
import ModelSettingsView from './ModelSettingsView.vue'
import NotifyChannelSettingsView from './NotifyChannelSettingsView.vue'
import LogsView from './LogsView.vue'
import UserManagementView from './UserManagementView.vue'
import McpAccessView from './McpAccessView.vue'
import type { AgentItem, HealthPayload, UserPayload } from '../types'

type SettingsTab = 'models' | 'dataSources' | 'notifyChannels' | 'digitalHumans' | 'users' | 'logs' | 'system' | 'mcp'

const props = defineProps<{
  health: HealthPayload | null
  user: UserPayload
  agents: AgentItem[]
}>()

const activeTab = ref<SettingsTab>('models')

const canViewAdminSettings = computed(() => ['admin', 'super_admin'].includes(props.user.role))
const canManageMcp = computed(() => props.user.permissions.some(
  permission => ['all', 'data:read', 'data:write', 'schedule:manage'].includes(permission)
))

const canManageChannels = computed(
  () => props.user.permissions.includes('all') || props.user.permissions.includes('model:manage')
)
const canViewSystem = computed(
  () => props.user.permissions.includes('all') || props.user.permissions.includes('system:view')
)
const canManageDigitalHumans = computed(
  () => props.user.permissions.includes('all') || props.user.permissions.includes('digital_human:manage')
)
const canManageUsers = computed(
  () => props.user.permissions.includes('all') || props.user.permissions.includes('user:manage')
)

const tabs = computed<Array<{ key: SettingsTab; label: string }>>(() => [
  ...(canViewAdminSettings.value ? [
    { key: 'models' as SettingsTab, label: '模型与平台' },
    { key: 'dataSources' as SettingsTab, label: '数据源' },
    ...(canManageChannels.value ? [{ key: 'notifyChannels' as SettingsTab, label: '通知通道' }] : []),
    ...(canManageDigitalHumans.value ? [{ key: 'digitalHumans' as SettingsTab, label: '数字人管理' }] : []),
    ...(canManageUsers.value ? [{ key: 'users' as SettingsTab, label: '用户管理' }] : [])
  ] : []),
  ...(canManageMcp.value ? [{ key: 'mcp' as SettingsTab, label: 'MCP 接入' }] : []),
  ...(canViewAdminSettings.value ? [
    ...(canViewSystem.value ? [{ key: 'logs' as SettingsTab, label: '运行日志' }] : []),
    { key: 'system' as SettingsTab, label: '系统状态' }
  ] : [])
])

function syncTabFromLocation() {
  const requested = new URLSearchParams(window.location.search).get('tab')
  const tab = tabs.value.find(item => item.key === requested)
  activeTab.value = tab?.key || tabs.value[0]?.key || 'mcp'
}

function selectTab(tab: SettingsTab) {
  if (!tabs.value.some(item => item.key === tab)) return
  activeTab.value = tab
  const url = new URL(window.location.href)
  url.pathname = '/settings'
  url.searchParams.set('tab', tab)
  window.history.pushState({}, '', `${url.pathname}${url.search}${url.hash}`)
}

watch(tabs, syncTabFromLocation, { immediate: true })
onMounted(() => window.addEventListener('popstate', syncTabFromLocation))
onBeforeUnmount(() => window.removeEventListener('popstate', syncTabFromLocation))
</script>

<template>
  <section class="settings-center">
    <header class="settings-tabs page-section">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        type="button"
        :class="{ active: activeTab === tab.key }"
        @click="selectTab(tab.key)"
      >
        {{ tab.label }}
      </button>
    </header>

    <McpAccessView v-if="activeTab === 'mcp' && canManageMcp" :user="user" />
    <ModelSettingsView v-else-if="activeTab === 'models' && canViewAdminSettings" />
    <DataSourceSettingsView v-else-if="activeTab === 'dataSources' && canViewAdminSettings" />
    <NotifyChannelSettingsView v-else-if="activeTab === 'notifyChannels' && canViewAdminSettings && canManageChannels" />
    <DigitalHumanAdminView v-else-if="activeTab === 'digitalHumans' && canViewAdminSettings && canManageDigitalHumans" />
    <UserManagementView v-else-if="activeTab === 'users' && canViewAdminSettings && canManageUsers" :user="user" />
    <LogsView v-else-if="activeTab === 'logs' && canViewAdminSettings && canViewSystem" />

    <section v-else-if="activeTab === 'system' && canViewAdminSettings" class="page-section settings-grid">
      <header class="section-header span-all">
        <div>
          <h2>系统状态</h2>
          <p>当前账号、数据库连接与 Agent 队列概览。</p>
        </div>
      </header>
      <div class="plain-panel">
        <h3>当前账号</h3>
        <p>{{ user.display_name }} / {{ user.username }}</p>
        <p>{{ user.role }} · {{ user.permissions.join(', ') }}</p>
      </div>
      <div class="plain-panel">
        <h3>数据库</h3>
        <p>{{ health?.database }}</p>
        <p>Engine: {{ health?.journal_mode }} | integrity: {{ health?.integrity }}</p>
      </div>
      <div class="plain-panel span-all">
        <h3>Agent 状态</h3>
        <AgentQueue :agents="agents" />
      </div>
    </section>
  </section>
</template>
