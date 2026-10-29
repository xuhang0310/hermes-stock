<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { api } from '../api'
import type { ManagedUser, UserPayload } from '../types'
import UserLoginLogs from '../components/UserLoginLogs.vue'
import { beijingFromDb } from '../utils/datetime'

const props = defineProps<{ user: UserPayload }>()

const users = ref<ManagedUser[]>([])
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const notice = ref('')
const editorOpen = ref(false)
const resetOpen = ref(false)
const editingId = ref<number | null>(null)
const resetTarget = ref<ManagedUser | null>(null)
const resetPassword = ref('')
const activeTab = ref<'users' | 'login-logs'>('users')
const logUsername = ref('')

function openLoginLogs(username = '') {
  logUsername.value = username
  activeTab.value = 'login-logs'
}

const form = reactive({
  username: '',
  display_name: '',
  password: '',
  role: 'researcher' as ManagedUser['role'],
  is_enabled: true
})

const isSuperAdmin = computed(() => props.user.role === 'super_admin')
const roleOptions = computed<Array<{ value: ManagedUser['role']; label: string }>>(() => [
  ...(isSuperAdmin.value || editingUser.value?.role === 'super_admin'
    ? [{ value: 'super_admin' as const, label: '超级管理员' }]
    : []),
  ...(isSuperAdmin.value || editingUser.value?.role === 'admin'
    ? [
        { value: 'admin' as const, label: '管理员' }
      ]
    : []),
  { value: 'researcher', label: '研究用户' },
  { value: 'viewer', label: '只读用户' }
])
const editingUser = computed(() => users.value.find((item) => item.id === editingId.value) || null)

function roleLabel(role: ManagedUser['role']) {
  return {
    super_admin: '超级管理员',
    admin: '管理员',
    researcher: '研究用户',
    viewer: '只读用户'
  }[role]
}

function formatTime(value: string | null) {
  if (!value) return '从未登录'
  return beijingFromDb(value)
}

async function loadUsers() {
  loading.value = true
  error.value = ''
  try {
    users.value = await api.users()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '用户列表加载失败'
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editingId.value = null
  Object.assign(form, {
    username: '', display_name: '', password: '', role: 'researcher', is_enabled: true
  })
  error.value = ''
  editorOpen.value = true
}

function openEdit(user: ManagedUser) {
  editingId.value = user.id
  Object.assign(form, {
    username: user.username,
    display_name: user.display_name,
    password: '',
    role: user.role,
    is_enabled: user.is_enabled
  })
  error.value = ''
  editorOpen.value = true
}

async function saveUser() {
  if (saving.value) return
  error.value = ''
  notice.value = ''
  saving.value = true
  try {
    if (editingId.value) {
      await api.updateUser(editingId.value, {
        display_name: form.display_name.trim(),
        role: form.role,
        is_enabled: form.is_enabled
      })
      notice.value = '用户资料已更新，权限变更会使其旧登录失效。'
    } else {
      await api.createUser({
        username: form.username.trim(),
        display_name: form.display_name.trim(),
        password: form.password,
        role: form.role
      })
      notice.value = '用户创建成功。该用户首次登录时持仓、自选和定时任务均为空。'
    }
    editorOpen.value = false
    activeTab.value = 'users'
    await loadUsers()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '保存失败'
  } finally {
    saving.value = false
  }
}

function openReset(user: ManagedUser) {
  resetTarget.value = user
  resetPassword.value = ''
  error.value = ''
  resetOpen.value = true
}

async function submitReset() {
  if (!resetTarget.value || saving.value) return
  saving.value = true
  error.value = ''
  try {
    await api.resetUserPassword(resetTarget.value.id, resetPassword.value)
    resetOpen.value = false
    notice.value = `已重置 ${resetTarget.value.display_name} 的密码，其现有登录已失效。`
  } catch (err) {
    error.value = err instanceof Error ? err.message : '密码重置失败'
  } finally {
    saving.value = false
  }
}

onMounted(loadUsers)
</script>

<template>
  <section class="user-admin page-section">
    <header class="user-admin__header">
      <div>
        <small>USER ACCESS</small>
        <h2>用户与数据空间</h2>
        <p>功能、模型与数据源全局复用；持仓、自选、会话和定时任务按用户独立保存。</p>
      </div>
      <button class="primary" type="button" @click="openCreate">新建用户</button>
    </header>

    <nav class="user-admin__tabs" aria-label="用户管理视图">
      <button type="button" :class="{ active: activeTab === 'users' }" :aria-pressed="activeTab === 'users'" @click="activeTab = 'users'">用户列表</button>
      <button type="button" :class="{ active: activeTab === 'login-logs' }" :aria-pressed="activeTab === 'login-logs'" @click="openLoginLogs()">登录日志</button>
    </nav>

    <UserLoginLogs v-if="activeTab === 'login-logs'" :key="logUsername" :username="logUsername" />
    <template v-else>
    <div class="isolation-note">
      <b>新用户从空白空间开始</b>
      <span>不会复制现有用户的持仓、自选股票、定时任务或任务执行记录。</span>
    </div>
    <p v-if="notice" class="feedback success">{{ notice }}</p>
    <p v-if="error && !editorOpen && !resetOpen" class="feedback error">{{ error }}</p>

    <div class="user-table-wrap">
      <table class="user-table">
        <thead>
          <tr><th>用户</th><th>角色</th><th>个人数据空间</th><th>最近登录</th><th>状态</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-if="loading"><td colspan="6" class="empty">正在加载用户…</td></tr>
          <tr v-else-if="!users.length"><td colspan="6" class="empty">暂无用户</td></tr>
          <tr v-for="item in users" :key="item.id">
            <td><b>{{ item.display_name }}</b><span>@{{ item.username }}</span></td>
            <td><span class="role-chip">{{ roleLabel(item.role) }}</span></td>
            <td><span class="isolated-chip">独立</span><small>持仓 · 自选 · 定时任务</small></td>
            <td>{{ formatTime(item.last_login_at) }}</td>
            <td><span :class="['status-dot', { off: !item.is_enabled }]">{{ item.is_enabled ? '已启用' : '已停用' }}</span></td>
            <td class="actions">
              <button type="button" @click="openLoginLogs(item.username)">登录日志</button>
              <button type="button" @click="openEdit(item)">编辑</button>
              <button type="button" @click="openReset(item)">重置密码</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    </template>

    <div v-if="editorOpen" class="user-modal" @mousedown.self="editorOpen = false">
      <form class="user-dialog" @submit.prevent="saveUser">
        <header><div><small>{{ editingId ? 'EDIT USER' : 'CREATE USER' }}</small><h3>{{ editingId ? '编辑用户' : '新建独立用户' }}</h3></div><button type="button" @click="editorOpen = false">×</button></header>
        <label>用户名<input v-model="form.username" :disabled="Boolean(editingId)" minlength="3" maxlength="80" required placeholder="例如 zhangsan" /></label>
        <label>显示名称<input v-model="form.display_name" minlength="1" maxlength="120" required placeholder="例如 张三" /></label>
        <label v-if="!editingId">初始密码<input v-model="form.password" type="password" minlength="8" maxlength="120" required autocomplete="new-password" placeholder="至少 8 位" /></label>
        <label>角色<select v-model="form.role" :disabled="editingUser?.id === user.id"><option v-for="role in roleOptions" :key="role.value" :value="role.value">{{ role.label }}</option></select></label>
        <label v-if="editingId" class="switch-row"><input v-model="form.is_enabled" type="checkbox" :disabled="editingUser?.id === user.id" /><span>允许该用户登录</span></label>
        <div class="data-boundary"><b>该账号独享</b><span>持仓、自选、聊天会话、Agent 任务、定时任务及执行记录</span><b>全局共享</b><span>功能、模型配置、行情和数据源配置</span></div>
        <p v-if="error" class="feedback error">{{ error }}</p>
        <footer><button type="button" @click="editorOpen = false">取消</button><button class="primary" type="submit" :disabled="saving">{{ saving ? '保存中…' : '保存' }}</button></footer>
      </form>
    </div>

    <div v-if="resetOpen && resetTarget" class="user-modal" @mousedown.self="resetOpen = false">
      <form class="user-dialog compact" @submit.prevent="submitReset">
        <header><div><small>RESET PASSWORD</small><h3>重置 {{ resetTarget.display_name }} 的密码</h3></div><button type="button" @click="resetOpen = false">×</button></header>
        <p>保存后该用户当前所有登录会立即失效，需要使用新密码重新登录。</p>
        <label>新密码<input v-model="resetPassword" type="password" minlength="8" maxlength="120" required autocomplete="new-password" placeholder="至少 8 位" /></label>
        <p v-if="error" class="feedback error">{{ error }}</p>
        <footer><button type="button" @click="resetOpen = false">取消</button><button class="primary" type="submit" :disabled="saving">确认重置</button></footer>
      </form>
    </div>
  </section>
</template>

<style scoped>
.user-admin { min-height: 520px; }
.user-admin__header { display: flex; align-items: flex-start; justify-content: space-between; gap: 24px; margin-bottom: 20px; }
.user-admin__header small, .user-dialog small { color: #d9b85f; letter-spacing: .12em; }
.user-admin__header h2, .user-dialog h3 { margin: 5px 0 6px; }
.user-admin__header p, .user-dialog p { margin: 0; color: #89949f; }
.user-admin__tabs { display: flex; gap: 6px; margin-bottom: 20px; padding-bottom: 10px; border-bottom: 1px solid #252c32; }
.user-admin__tabs button { background: transparent; border-color: transparent; color: #89949f; }
.user-admin__tabs button.active { color: #e4c574; background: #262115; border-color: #51442a; }
button { border: 1px solid #303842; border-radius: 7px; background: #151a1f; color: #d8dde2; padding: 9px 14px; cursor: pointer; }
button.primary { border-color: #d5b45c; background: #d5b45c; color: #17130b; font-weight: 700; }
button:disabled { cursor: not-allowed; opacity: .5; }
.isolation-note { display: flex; align-items: center; gap: 14px; padding: 13px 16px; border: 1px solid #2f3f3a; border-radius: 8px; background: #111a17; color: #aab5b0; margin-bottom: 16px; }
.isolation-note b { color: #74c69d; white-space: nowrap; }
.user-table-wrap { overflow-x: auto; border: 1px solid #252c32; border-radius: 9px; }
.user-table { width: 100%; border-collapse: collapse; min-width: 960px; }
.user-table th { padding: 12px 14px; background: #11161a; color: #8f9aa3; text-align: left; font-size: 12px; font-weight: 600; }
.user-table td { padding: 14px; border-top: 1px solid #242a30; color: #c8ced3; vertical-align: middle; }
.user-table th:nth-child(4), .user-table td:nth-child(4) { width: 140px; white-space: nowrap; }
.user-table th:nth-child(5), .user-table td:nth-child(5) { width: 76px; white-space: nowrap; }
.user-table th:nth-child(6), .user-table td:nth-child(6) { width: 225px; white-space: nowrap; }
.user-table td:first-child b, .user-table td:first-child span { display: block; }
.user-table td:first-child span { margin-top: 3px; color: #77828c; font-size: 12px; }
.role-chip, .isolated-chip, .status-dot { display: inline-flex; align-items: center; border-radius: 999px; padding: 4px 9px; font-size: 12px; }
.role-chip { border: 1px solid #4b4027; background: #211c11; color: #dec16d; }
.isolated-chip { background: #12241d; color: #72c49a; margin-right: 8px; }
.user-table td:nth-child(3) small { color: #73808a; }
.status-dot { gap: 6px; color: #72c49a; }
.status-dot::before { content: ''; width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
.status-dot.off { color: #7f8991; }
.actions { display: flex; min-width: 150px; gap: 8px; }
.actions button { padding: 6px 9px; font-size: 12px; }
.empty { text-align: center; color: #74808a; padding: 36px !important; }
.feedback { padding: 10px 12px; border-radius: 6px; }
.feedback.success { border: 1px solid #315442; background: #112019; color: #78c89d; }
.feedback.error { border: 1px solid #64353b; background: #261519; color: #f08b96; }
.user-modal { position: fixed; inset: 0; z-index: 1100; display: grid; place-items: center; padding: 24px; background: rgba(0, 0, 0, .72); backdrop-filter: blur(5px); }
.user-dialog { width: min(560px, 100%); display: grid; gap: 16px; padding: 22px; border: 1px solid #343c43; border-radius: 12px; background: #12171b; box-shadow: 0 24px 80px rgba(0, 0, 0, .48); }
.user-dialog.compact { width: min(480px, 100%); }
.user-dialog header, .user-dialog footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.user-dialog header > button { padding: 4px 10px; font-size: 22px; border: 0; background: transparent; }
.user-dialog label { display: grid; gap: 7px; color: #aab3ba; font-size: 13px; }
.user-dialog input, .user-dialog select { width: 100%; box-sizing: border-box; border: 1px solid #303840; border-radius: 7px; background: #090d10; color: #e4e8eb; padding: 10px 11px; outline: none; }
.user-dialog input:focus, .user-dialog select:focus { border-color: #b99a4a; box-shadow: 0 0 0 3px rgba(185, 154, 74, .12); }
.user-dialog .switch-row { display: flex; align-items: center; gap: 9px; }
.user-dialog .switch-row input { width: 16px; height: 16px; }
.data-boundary { display: grid; grid-template-columns: 90px 1fr; gap: 9px 12px; padding: 13px; border-radius: 7px; background: #0d1215; color: #8d989f; font-size: 12px; }
.data-boundary b { color: #d5b45c; }
.user-dialog footer { justify-content: flex-end; padding-top: 4px; }
@media (max-width: 760px) { .user-admin__header { flex-direction: column; } .isolation-note { align-items: flex-start; flex-direction: column; gap: 5px; } }
</style>
