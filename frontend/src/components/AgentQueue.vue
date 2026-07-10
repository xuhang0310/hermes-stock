<script setup lang="ts">
import type { AgentItem } from '../types'

defineProps<{
  agents: AgentItem[]
}>()
</script>

<template>
  <div class="agent-list">
    <div v-for="agent in agents" :key="agent.agent_name" class="agent-row">
      <span class="status-dot" :class="agent.status"></span>
      <div>
        <strong>{{ agent.display_name }}</strong>
        <small>{{ agent.role_description }}</small>
      </div>
      <span class="agent-meta">{{ agent.enabled ? `${agent.report_count} 报告` : '未启用' }}</span>
    </div>
  </div>
</template>
