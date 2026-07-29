<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import type { ApprovalPolicy } from '../types'

const props = defineProps<{
  modelValue: ApprovalPolicy
  disabled?: boolean
  execAvailable?: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [value: ApprovalPolicy]
}>()

const root = ref<HTMLElement | null>(null)
const open = ref(false)
const options: Array<{ value: ApprovalPolicy; label: string; short: string; description: string }> = [
  {
    value: 'ask',
    label: '请求批准',
    short: '请求批准',
    description: '执行代码、修改文件或写入业务数据前先向你确认'
  },
  {
    value: 'smart',
    label: '智能批准',
    short: '智能批准',
    description: '自动处理普通操作，仅高风险操作需要确认'
  },
  {
    value: 'full',
    label: '完全访问',
    short: '完全访问',
    description: '本次会话内自动执行，仍受隔离容器和工作区边界保护'
  }
]

const current = computed(() => options.find((item) => item.value === props.modelValue) || options[0])

function choose(value: ApprovalPolicy) {
  emit('update:modelValue', value)
  open.value = false
}

function closeFromOutside(event: PointerEvent) {
  if (root.value && !root.value.contains(event.target as Node)) open.value = false
}

onMounted(() => document.addEventListener('pointerdown', closeFromOutside))
onBeforeUnmount(() => document.removeEventListener('pointerdown', closeFromOutside))
</script>

<template>
  <div ref="root" class="approval-policy-control" @keydown.esc="open = false">
    <button
      class="approval-policy-trigger"
      type="button"
      :disabled="disabled"
      :aria-expanded="open"
      :aria-label="`工具权限：${current.short}`"
      title="设置 Agent 工具权限策略"
      @click="open = !open"
    >
      <span class="approval-policy-shield" aria-hidden="true">◇</span>
      <strong>{{ current.short }}</strong>
      <span class="approval-policy-caret" aria-hidden="true">⌄</span>
    </button>

    <div v-if="open" class="approval-policy-menu" role="menu">
      <header><strong>Agent 工具权限</strong><span>当前会话</span></header>
      <button
        v-for="option in options"
        :key="option.value"
        type="button"
        role="menuitemradio"
        :aria-checked="modelValue === option.value"
        :class="{ selected: modelValue === option.value }"
        @click="choose(option.value)"
      >
        <span class="approval-policy-radio"><i></i></span>
        <span><strong>{{ option.label }}</strong><small>{{ option.description }}</small></span>
        <b v-if="modelValue === option.value" aria-label="已选择">✓</b>
      </button>
      <footer>
        {{ execAvailable ? 'Python 在独立 Docker 沙箱中运行' : 'Python 沙箱当前不可用，授权策略不会绕过此限制' }}
      </footer>
    </div>
  </div>
</template>
