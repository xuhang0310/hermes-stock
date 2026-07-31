<script setup lang="ts">
import { ref } from 'vue'

const REMEMBER_LOGIN_KEY = 'hermes_stock_remember_login'
const REMEMBERED_USERNAME_KEY = 'hermes_stock_remembered_username'

defineProps<{
  loading: boolean
  error: string
}>()

const emit = defineEmits<{
  login: [payload: { username: string; password: string; remember_me: boolean }]
}>()

const rememberMe = ref(window.localStorage.getItem(REMEMBER_LOGIN_KEY) !== 'false')
const username = ref(window.localStorage.getItem(REMEMBERED_USERNAME_KEY) || '')
const password = ref('')

function submit() {
  if (rememberMe.value) {
    window.localStorage.setItem(REMEMBER_LOGIN_KEY, 'true')
    window.localStorage.setItem(REMEMBERED_USERNAME_KEY, username.value.trim())
  } else {
    window.localStorage.setItem(REMEMBER_LOGIN_KEY, 'false')
    window.localStorage.removeItem(REMEMBERED_USERNAME_KEY)
  }
  emit('login', {
    username: username.value.trim(),
    password: password.value,
    remember_me: rememberMe.value
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
          <input v-model="username" name="username" autocomplete="username" />
        </label>
        <label>
          <span>密码</span>
          <input
            v-model="password"
            name="password"
            type="password"
            autocomplete="current-password"
          />
        </label>
        <label class="remember-login">
          <input v-model="rememberMe" type="checkbox" />
          <span>记住登录，30 天内免登录</span>
        </label>
        <p class="login-hint">账号会保存在本机，密码由浏览器密码管理器安全保存。</p>
        <button type="submit" :disabled="loading">{{ loading ? '登录中...' : '登录' }}</button>
        <p v-if="error" class="login-error">{{ error }}</p>
      </form>
    </section>
  </main>
</template>
