<script setup lang="ts">
import type { SystemLog } from '../types'

defineProps<{
  logs: SystemLog[]
}>()
</script>

<template>
  <div class="log-panel">
    <div v-if="logs.length === 0" class="empty-cell">暂无日志</div>
    <div v-for="log in logs" :key="log.id" class="log-line">
      <span>{{ log.created_at }}</span>
      <strong :class="`log-${log.level.toLowerCase()}`">{{ log.level }}</strong>
      <code>{{ log.module }}</code>
      <p>{{ log.message }}</p>
    </div>
  </div>
</template>
