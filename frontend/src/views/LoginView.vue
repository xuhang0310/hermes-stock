<script setup lang="ts">
import { ref } from 'vue'

defineProps<{
  loading: boolean
  error: string
}>()

const emit = defineEmits<{
  login: [payload: { username: string; password: string }]
}>()

const username = ref('admin')
const password = ref('admin123456')

function submit() {
  emit('login', {
    username: username.value.trim(),
    password: password.value
  })
}
</script>

<template>
  <main class="login-screen">
    <section class="login-panel">
      <div class="brand login-brand">
        <div class="brand-logo-frame">
          <img src="/six-rabbits-logo.png" alt="SixRabbits" />
        </div>
        <div>
          <strong>SixRabbits Stock</strong>
          <small>A股多智能体研究台</small>
        </div>
      </div>

      <form class="login-form" @submit.prevent="submit">
        <label>
          <span>账号</span>
          <input v-model="username" autocomplete="username" />
        </label>
        <label>
          <span>密码</span>
          <input v-model="password" type="password" autocomplete="current-password" />
        </label>
        <button type="submit" :disabled="loading">{{ loading ? '登录中...' : '登录' }}</button>
        <p v-if="error" class="login-error">{{ error }}</p>
      </form>
    </section>
  </main>
</template>
