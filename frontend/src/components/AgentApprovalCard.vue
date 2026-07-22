<script setup lang="ts">
import type { AgentToolApproval } from '../types'

defineProps<{
  approval: AgentToolApproval
  busy?: boolean
  prominent?: boolean
}>()

defineEmits<{
  decide: [decision: 'approved' | 'denied']
}>()
</script>

<template>
  <section
    class="agent-approval-card"
    :class="[approval.status, { prominent }]"
    :role="approval.status === 'pending' ? 'alert' : undefined"
    :aria-live="approval.status === 'pending' ? 'assertive' : undefined"
  >
    <div class="agent-approval-icon">!</div>
    <div class="agent-approval-copy">
      <span>{{ approval.risk_level === 'high' ? '高风险工具请求' : '工作区操作请求' }}</span>
      <strong>{{ approval.display_name }}</strong>
      <code>{{ approval.arguments_preview }}</code>
    </div>
    <div v-if="approval.status === 'pending'" class="agent-approval-actions">
      <button class="reject" type="button" title="拒绝本次工具调用" :disabled="busy" @click="$emit('decide', 'denied')">
        拒绝执行
      </button>
      <button class="approve" type="button" title="仅批准当前这一次工具调用" :disabled="busy" @click="$emit('decide', 'approved')">
        {{ busy ? '正在提交...' : '批准并继续' }}
      </button>
    </div>
    <span v-else class="agent-approval-result">
      {{ approval.status === 'approved' ? '已批准' : approval.status === 'denied' ? '已拒绝' : approval.status === 'expired' ? '已过期' : '已取消' }}
    </span>
  </section>
</template>
