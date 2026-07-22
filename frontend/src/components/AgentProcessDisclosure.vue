<script setup lang="ts">
defineProps<{
  completed: boolean
  expanded: boolean
  stepCount: number
}>()

defineEmits<{
  toggle: []
}>()
</script>

<template>
  <section class="agent-process-disclosure" :class="{ completed, expanded }">
    <button
      v-if="completed"
      type="button"
      class="agent-process-toggle"
      :aria-expanded="expanded"
      @click="$emit('toggle')"
    >
      <span class="agent-process-chevron" aria-hidden="true"></span>
      <span class="agent-process-toggle-copy">
        <strong>思考与工具过程</strong>
        <small>{{ stepCount }} 个步骤</small>
      </span>
      <span class="agent-process-toggle-action">{{ expanded ? '收起' : '展开' }}</span>
    </button>
    <div v-show="!completed || expanded" class="agent-process-content">
      <slot></slot>
    </div>
  </section>
</template>
