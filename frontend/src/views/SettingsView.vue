<script setup lang="ts">
import { computed, ref } from 'vue'
import AgentQueue from '../components/AgentQueue.vue'
import DataSourceSettingsView from './DataSourceSettingsView.vue'
import ModelSettingsView from './ModelSettingsView.vue'
import NotifyChannelSettingsView from './NotifyChannelSettingsView.vue'
import type { AgentItem, HealthPayload, UserPayload } from '../types'

type SettingsTab = 'models' | 'dataSources' | 'notifyChannels' | 'system'

const props = defineProps<{
  health: HealthPayload | null
  user: UserPayload
  agents: AgentItem[]
}>()

const activeTab = ref<SettingsTab>('models')

const canManageChannels = computed(
  () => props.user.permissions.includes('all') || props.user.permissions.includes('model:manage')
)

const tabs = computed<Array<{ key: SettingsTab; label: string }>>(() => [
  { key: 'models', label: '模型与平台' },
  { key: 'dataSources', label: '数据源' },
  ...(canManageChannels.value ? [{ key: 'notifyChannels' as SettingsTab, label: '通知通道' }] : []),
  { key: 'system', label: '系统状态' }
])
</script>

<template>
  <section class="settings-center">
    <header class="settings-tabs page-section">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        type="button"
        :class="{ active: activeTab === tab.key }"
        @click="activeTab = tab.key"
      >
        {{ tab.label }}
      </button>
    </header>

    <ModelSettingsView v-if="activeTab === 'models'" />
    <DataSourceSettingsView v-else-if="activeTab === 'dataSources'" />
    <NotifyChannelSettingsView v-else-if="activeTab === 'notifyChannels'" />

    <section v-else class="page-section settings-grid">
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
