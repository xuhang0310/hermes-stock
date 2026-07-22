<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { api } from '../api'
import type { AgentAttachment } from '../types'

const props = defineProps<{
  modelValue: AgentAttachment[]
  disabled?: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [value: AgentAttachment[]]
}>()

const input = ref<HTMLInputElement | null>(null)
const uploading = ref(false)
const error = ref('')
const maxFiles = 6
const maxTotalBytes = 32 * 1024 * 1024
const clipboardImageExtensions: Record<string, string> = {
  'image/jpeg': 'jpg',
  'image/png': 'png',
  'image/webp': 'webp'
}

function formatBytes(value: number) {
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${Math.ceil(value / 1024)} KB`
  return `${(value / (1024 * 1024)).toFixed(1)} MB`
}

function openPicker() {
  if (!props.disabled && !uploading.value) input.value?.click()
}

async function uploadFiles(files: File[]) {
  if (!files.length) return
  if (props.disabled || uploading.value) {
    error.value = uploading.value ? '附件正在上传，请稍后再试' : ''
    return
  }

  error.value = ''
  if (props.modelValue.length + files.length > maxFiles) {
    error.value = `每条消息最多上传 ${maxFiles} 个附件`
    return
  }
  const totalBytes = props.modelValue.reduce((sum, item) => sum + item.bytes, 0) + files.reduce((sum, file) => sum + file.size, 0)
  if (totalBytes > maxTotalBytes) {
    error.value = '单条消息的附件总大小不能超过 32MB'
    return
  }

  uploading.value = true
  const next = [...props.modelValue]
  try {
    for (const file of files) {
      const isImage = file.type.startsWith('image/')
      const limit = (isImage ? 8 : 20) * 1024 * 1024
      if (file.size > limit) throw new Error(`${file.name} 超过 ${isImage ? 8 : 20}MB`)

      const uploaded = await api.uploadAgentAttachment(file)
      next.push({ ...uploaded, preview_url: isImage ? URL.createObjectURL(file) : undefined })
      emit('update:modelValue', [...next])
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : '附件上传失败'
  } finally {
    uploading.value = false
  }
}

async function onFilesSelected(event: Event) {
  const element = event.target as HTMLInputElement
  const files = Array.from(element.files || [])
  element.value = ''
  await uploadFiles(files)
}

async function handlePaste(event: ClipboardEvent): Promise<boolean> {
  const imageItems = Array.from(event.clipboardData?.items || []).filter(
    (item) => item.kind === 'file' && item.type.startsWith('image/')
  )
  if (!imageItems.length) return false

  event.preventDefault()
  if (props.disabled || uploading.value) {
    error.value = uploading.value ? '附件正在上传，请稍后再试' : ''
    return true
  }

  const unsupported = imageItems.find((item) => !clipboardImageExtensions[item.type])
  if (unsupported) {
    error.value = '粘贴图片仅支持 PNG、JPG 和 WEBP 格式'
    return true
  }

  const stamp = new Date().toISOString().replace(/[-:TZ.]/g, '').slice(0, 14)
  const files = imageItems.flatMap((item, index) => {
    const blob = item.getAsFile()
    if (!blob) return []
    const extension = clipboardImageExtensions[item.type]
    return [new File([blob], `clipboard-${stamp}-${index + 1}.${extension}`, {
      type: item.type,
      lastModified: Date.now()
    })]
  })

  if (!files.length) {
    error.value = '未能读取剪贴板中的图片'
    return true
  }
  await uploadFiles(files)
  return true
}

defineExpose({ handlePaste })

function removeAttachment(index: number) {
  emit('update:modelValue', props.modelValue.filter((_, itemIndex) => itemIndex !== index))
}

watch(
  () => props.modelValue,
  (next, previous) => {
    for (const item of previous || []) {
      if (item.preview_url && !next.some((candidate) => candidate.path === item.path)) {
        URL.revokeObjectURL(item.preview_url)
      }
    }
  }
)

onBeforeUnmount(() => {
  for (const item of props.modelValue) {
    if (item.preview_url) URL.revokeObjectURL(item.preview_url)
  }
})
</script>

<template>
  <div class="chat-attachment-zone">
    <input
      ref="input"
      class="chat-attachment-input"
      type="file"
      multiple
      accept=".png,.jpg,.jpeg,.webp,.pdf,.docx,.pptx,.xlsx,.txt,.md,.csv,.json"
      :disabled="disabled || uploading"
      @change="onFilesSelected"
    />
    <button
      class="chat-attachment-add"
      type="button"
      :disabled="disabled || uploading"
      :aria-label="uploading ? '附件上传中' : '添加图片或附件'"
      title="添加图片或附件"
      @click="openPicker"
    >
      <span aria-hidden="true">{{ uploading ? '…' : '＋' }}</span>
    </button>
    <div v-if="modelValue.length" class="chat-attachment-list">
      <article v-for="(item, index) in modelValue" :key="item.path" class="chat-attachment-chip">
        <img v-if="item.preview_url" :src="item.preview_url" alt="" />
        <span v-else class="chat-attachment-file">{{ item.name.split('.').pop()?.slice(0, 4).toUpperCase() }}</span>
        <span><strong :title="item.name">{{ item.name }}</strong><small>{{ formatBytes(item.bytes) }}</small></span>
        <button type="button" title="移除附件" :disabled="disabled" @click="removeAttachment(index)">×</button>
      </article>
    </div>
    <small v-if="error" class="chat-attachment-error">{{ error }}</small>
  </div>
</template>
