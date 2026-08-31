<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { api } from '../api'
import type { ModelConfig, ModelPlatform } from '../types'

type PresetModel = {
  display_name: string
  model_name: string
}

type ProviderPreset = {
  id: string
  name: string
  provider_type: string
  base_url: string
  models: PresetModel[]
}

type TestResult = {
  ok?: boolean
  message?: string
  error?: string
  latency_ms?: number
  status_code?: number
  response_preview?: string
  models?: string[]
}

const providerPresets: ProviderPreset[] = [
  {
    id: 'dashscope',
    name: '阿里云百炼',
    provider_type: 'DashScope OpenAI compatible',
    base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    models: [
      { display_name: 'Qwen Plus', model_name: 'qwen-plus' },
      { display_name: 'Qwen Max', model_name: 'qwen-max' },
      { display_name: 'DeepSeek V3', model_name: 'deepseek-v3' }
    ]
  },
  {
    id: 'openai',
    name: 'OpenAI',
    provider_type: 'OpenAI',
    base_url: 'https://api.openai.com/v1',
    models: [
      { display_name: 'GPT-4.1 Mini', model_name: 'gpt-4.1-mini' },
      { display_name: 'GPT-4.1', model_name: 'gpt-4.1' }
    ]
  },
  {
    id: 'deepseek',
    name: 'DeepSeek',
    provider_type: 'OpenAI compatible',
    base_url: 'https://api.deepseek.com/v1',
    models: [
      { display_name: 'DeepSeek Chat', model_name: 'deepseek-chat' },
      { display_name: 'DeepSeek Reasoner', model_name: 'deepseek-reasoner' }
    ]
  },
  {
    id: 'moonshot',
    name: 'Moonshot',
    provider_type: 'OpenAI compatible',
    base_url: 'https://api.moonshot.cn/v1',
    models: [
      { display_name: 'Kimi K2', model_name: 'kimi-k2-0711-preview' },
      { display_name: 'Moonshot V1 8K', model_name: 'moonshot-v1-8k' }
    ]
  },
  {
    id: 'zhipu',
    name: '智谱 GLM',
    provider_type: 'OpenAI compatible',
    base_url: 'https://open.bigmodel.cn/api/paas/v4',
    models: [
      { display_name: 'GLM-4 Plus', model_name: 'glm-4-plus' },
      { display_name: 'GLM-4 Flash', model_name: 'glm-4-flash' }
    ]
  },
  {
    id: 'volcengine',
    name: '火山方舟',
    provider_type: 'OpenAI compatible',
    base_url: 'https://ark.cn-beijing.volces.com/api/v3',
    models: [{ display_name: 'Doubao Pro', model_name: 'doubao-pro-32k' }]
  }
]

const platforms = ref<ModelPlatform[]>([])
const configs = ref<ModelConfig[]>([])
const selectedPresetId = ref('dashscope')
const editingPlatformId = ref<number | undefined>()
const editingConfigId = ref<number | undefined>()
const platformMessage = ref('')
const configMessage = ref('')
const platformTest = ref<TestResult | null>(null)
const configTest = ref<TestResult | null>(null)
const testingPlatform = ref(false)
const testingConfig = ref(false)
const busy = ref(false)
const dialog = ref<'platform' | 'model' | null>(null)
const showAdvanced = ref(false)
const platformStep = ref(1)

const platformForm = reactive({
  name: '',
  provider_type: 'OpenAI compatible',
  base_url: '',
  api_key: '',
  status: 'enabled',
  remark: ''
})

const configForm = reactive({
  platform_id: null as number | null,
  display_name: '',
  provider_name: 'custom',
  base_url: '',
  api_key: '',
  model_name: '',
  is_active: false,
  status: 'enabled',
  temperature: 0.2,
  max_steps: 6,
  thinking_mode: 'auto',
  remark: ''
})

const enabledPlatforms = computed(() => platforms.value.filter((item) => item.status !== 'disabled'))
const selectedPlatform = computed(() => platforms.value.find((item) => item.id === configForm.platform_id) || null)
const activeModel = computed(() => configs.value.find((item) => item.is_active) || null)
const selectedPreset = computed(() => providerPresets.find((item) => item.id === selectedPresetId.value) || providerPresets[0])
const selectedPlatformPreset = computed(() => {
  const platform = selectedPlatform.value
  if (!platform) return selectedPreset.value
  return (
    providerPresets.find((item) => platform.base_url.startsWith(item.base_url) || platform.name.includes(item.name)) ||
    selectedPreset.value
  )
})
const commonModels = computed(() => selectedPlatformPreset.value.models)

function resetPlatformForm() {
  editingPlatformId.value = undefined
  platformTest.value = null
  platformMessage.value = ''
  Object.assign(platformForm, {
    name: selectedPreset.value.name,
    provider_type: selectedPreset.value.provider_type,
    base_url: selectedPreset.value.base_url,
    api_key: '',
    status: 'enabled',
    remark: ''
  })
}

function openNewPlatform() {
  selectedPresetId.value = 'dashscope'
  resetPlatformForm()
  platformStep.value = 1
  dialog.value = 'platform'
}

function openNewModel(platformId?: number) {
  resetConfigForm()
  if (platformId) configForm.platform_id = platformId
  applyPlatformToConfig()
  showAdvanced.value = false
  dialog.value = 'model'
}

function closeDialog() {
  dialog.value = null
  platformTest.value = null
  configTest.value = null
}

function resetConfigForm() {
  editingConfigId.value = undefined
  configTest.value = null
  configMessage.value = ''
  Object.assign(configForm, {
    platform_id: enabledPlatforms.value[0]?.id || null,
    display_name: '',
    provider_name: enabledPlatforms.value[0]?.name || 'custom',
    base_url: enabledPlatforms.value[0]?.base_url || '',
    api_key: '',
    model_name: '',
    is_active: !configs.value.some((item) => item.is_active),
    status: 'enabled',
    temperature: 0.2,
    max_steps: 6,
    thinking_mode: 'auto',
    remark: ''
  })
}

async function load() {
  const [platformPayload, configPayload] = await Promise.all([api.modelPlatforms(), api.modelConfigs()])
  platforms.value = platformPayload
  configs.value = configPayload
  if (!platformForm.name) resetPlatformForm()
  if (!configForm.platform_id && enabledPlatforms.value[0]) resetConfigForm()
}

function applyPreset(id = selectedPresetId.value) {
  const preset = providerPresets.find((item) => item.id === id)
  if (!preset) return
  selectedPresetId.value = id
  Object.assign(platformForm, {
    name: preset.name,
    provider_type: preset.provider_type,
    base_url: preset.base_url,
    status: 'enabled'
  })
  platformTest.value = null
}

function editPlatform(row: ModelPlatform) {
  editingPlatformId.value = row.id
  const preset = providerPresets.find((item) => row.base_url.startsWith(item.base_url) || row.name.includes(item.name))
  if (preset) selectedPresetId.value = preset.id
  Object.assign(platformForm, {
    name: row.name,
    provider_type: row.provider_type,
    base_url: row.base_url,
    api_key: '',
    status: row.status,
    remark: row.remark || ''
  })
  platformTest.value = null
  platformMessage.value = ''
  platformStep.value = 2
  dialog.value = 'platform'
}

function editConfig(row: ModelConfig) {
  editingConfigId.value = row.id
  Object.assign(configForm, {
    platform_id: row.platform_id,
    display_name: row.display_name,
    provider_name: row.provider_name,
    base_url: row.base_url,
    api_key: '',
    model_name: row.model_name,
    is_active: row.is_active,
    status: row.status,
    temperature: row.temperature,
    max_steps: row.max_steps,
    thinking_mode: row.thinking_mode || 'auto',
    remark: row.remark || ''
  })
  configTest.value = null
  configMessage.value = ''
  showAdvanced.value = false
  dialog.value = 'model'
}

function applyPlatformToConfig() {
  const platform = selectedPlatform.value
  if (!platform) {
    configForm.provider_name = 'custom'
    return
  }
  configForm.provider_name = platform.name
  configForm.base_url = platform.base_url
  configTest.value = null
}

function fillModel(model: PresetModel) {
  const platform = selectedPlatform.value
  configForm.model_name = model.model_name
  configForm.display_name = platform ? `${platform.name} ${model.display_name}` : model.display_name
  if (platform) {
    configForm.provider_name = platform.name
    configForm.base_url = platform.base_url
  }
  configTest.value = null
}

function platformPayload() {
  return {
    name: platformForm.name.trim(),
    provider_type: platformForm.provider_type.trim() || 'OpenAI compatible',
    base_url: platformForm.base_url.trim().replace(/\/+$/, ''),
    api_key: platformForm.api_key || null,
    status: platformForm.status,
    remark: platformForm.remark || null
  }
}

function configPayload() {
  return {
    platform_id: configForm.platform_id,
    display_name: configForm.display_name.trim(),
    provider_name: configForm.provider_name.trim() || selectedPlatform.value?.name || 'custom',
    base_url: (selectedPlatform.value?.base_url || configForm.base_url).trim().replace(/\/+$/, ''),
    api_key: configForm.api_key || null,
    model_name: configForm.model_name.trim(),
    is_active: configForm.is_active,
    status: configForm.status,
    temperature: Number(configForm.temperature),
    max_steps: Number(configForm.max_steps),
    thinking_mode: configForm.thinking_mode,
    remark: configForm.remark || null
  }
}

async function savePlatform() {
  platformMessage.value = ''
  platformTest.value = null
  busy.value = true
  try {
    await api.saveModelPlatform(platformPayload(), editingPlatformId.value)
    platformMessage.value = editingPlatformId.value ? '平台已更新' : '平台已创建'
    await load()
    resetPlatformForm()
    dialog.value = null
  } catch (err) {
    platformMessage.value = err instanceof Error ? err.message : '保存平台失败'
  } finally {
    busy.value = false
  }
}

async function testPlatform() {
  testingPlatform.value = true
  platformMessage.value = ''
  try {
    platformTest.value = (await api.testModelPlatform(platformPayload(), editingPlatformId.value)) as TestResult
  } catch (err) {
    platformTest.value = { ok: false, error: err instanceof Error ? err.message : '平台测试失败' }
  } finally {
    testingPlatform.value = false
  }
}

async function saveConfig() {
  configMessage.value = ''
  configTest.value = null
  busy.value = true
  try {
    await api.saveModelConfig(configPayload(), editingConfigId.value)
    configMessage.value = editingConfigId.value ? '模型配置已更新' : '模型配置已创建'
    await load()
    resetConfigForm()
    dialog.value = null
  } catch (err) {
    configMessage.value = err instanceof Error ? err.message : '保存模型配置失败'
  } finally {
    busy.value = false
  }
}

async function testConfig() {
  testingConfig.value = true
  configMessage.value = ''
  try {
    configTest.value = (await api.testModelConfig(configPayload(), editingConfigId.value)) as TestResult
  } catch (err) {
    configTest.value = { ok: false, error: err instanceof Error ? err.message : '模型测试失败' }
  } finally {
    testingConfig.value = false
  }
}

async function activateConfig(id: number) {
  configMessage.value = ''
  busy.value = true
  try {
    await api.activateModelConfig(id)
    configMessage.value = '默认模型已切换'
    await load()
  } catch (err) {
    configMessage.value = err instanceof Error ? err.message : '切换默认模型失败'
  } finally {
    busy.value = false
  }
}

async function testSavedConfig(row: ModelConfig) {
  editingConfigId.value = row.id
  editConfig(row)
  await testConfig()
}

async function createCommonModels() {
  const platform = selectedPlatform.value
  if (!platform) {
    configMessage.value = '请先选择一个平台'
    return
  }
  busy.value = true
  configMessage.value = ''
  let created = 0
  try {
    for (const model of commonModels.value) {
      const exists = configs.value.some((item) => item.platform_id === platform.id && item.model_name === model.model_name)
      if (exists) continue
      await api.saveModelConfig({
        platform_id: platform.id,
        display_name: `${platform.name} ${model.display_name}`,
        provider_name: platform.name,
        base_url: platform.base_url,
        api_key: null,
        model_name: model.model_name,
        is_active: !configs.value.some((item) => item.is_active) && created === 0,
        status: 'enabled',
        temperature: 0.2,
        max_steps: 6,
        remark: `${platform.name} 常用模型`
      })
      created += 1
    }
    await load()
    configMessage.value = created ? `已创建 ${created} 个常用模型` : '常用模型已存在'
  } catch (err) {
    configMessage.value = err instanceof Error ? err.message : '创建常用模型失败'
  } finally {
    busy.value = false
  }
}

function testText(result: TestResult | null) {
  if (!result) return ''
  const head = result.ok ? result.message || '测试通过' : result.error || '测试失败'
  const latency = typeof result.latency_ms === 'number' ? ` · ${result.latency_ms}ms` : ''
  const status = result.status_code ? ` · HTTP ${result.status_code}` : ''
  return `${head}${status}${latency}`
}

function platformModelCount(id: number) {
  return configs.value.filter((item) => item.platform_id === id).length
}

function keyState(row: ModelPlatform | ModelConfig) {
  return row.has_api_key ? row.api_key_masked || '已配置' : '未配置'
}

onMounted(load)
</script>

<template>
  <section class="model-hub">
    <header class="model-hub-hero">
      <div>
        <span class="eyebrow">AI CONNECTIONS</span>
        <h1>模型服务</h1>
        <p>先连接服务商，再选择要使用的模型。密钥只需配置一次。</p>
      </div>
      <button class="primary-action" type="button" @click="openNewPlatform"><b>＋</b> 连接新平台</button>
    </header>

    <section v-if="activeModel" class="active-model-card">
      <div class="active-model-orb">AI</div>
      <div>
        <span>当前默认模型</span>
        <strong>{{ activeModel.display_name }}</strong>
        <code>{{ activeModel.platform_name || activeModel.provider_name }} · {{ activeModel.model_name }}</code>
      </div>
      <i>运行中</i>
    </section>
    <section v-else class="empty-callout">
      <span>还没有可用模型</span>
      <p>连接一个模型平台，几分钟内即可开始使用问股和数字人。</p>
      <button type="button" @click="openNewPlatform">开始配置</button>
    </section>

    <section class="hub-section">
      <header class="hub-section-title">
        <div><h2>已连接的平台</h2><p>{{ enabledPlatforms.length }} 个可用，共 {{ platforms.length }} 个</p></div>
      </header>
      <div class="provider-grid">
        <article v-for="item in platforms" :key="item.id" class="provider-card" :class="{ disabled: item.status === 'disabled' }">
          <div class="provider-card-top">
            <div class="provider-monogram">{{ item.name.slice(0, 1).toUpperCase() }}</div>
            <span class="status-pill" :class="item.status">{{ item.status === 'enabled' ? '已连接' : '已停用' }}</span>
          </div>
          <div><h3>{{ item.name }}</h3><p>{{ item.provider_type }}</p></div>
          <div class="provider-facts">
            <span><b>{{ platformModelCount(item.id) }}</b> 个模型</span>
            <span><b>{{ item.has_api_key ? '••••' : '—' }}</b> 密钥</span>
          </div>
          <div class="provider-actions">
            <button type="button" @click="openNewModel(item.id)">＋ 添加模型</button>
            <button class="icon-action" type="button" title="编辑平台" @click="editPlatform(item)">•••</button>
          </div>
        </article>
        <button class="provider-add-card" type="button" @click="openNewPlatform">
          <b>＋</b><span>连接新平台</span><small>OpenAI、DeepSeek、百炼等</small>
        </button>
      </div>
    </section>

    <section class="hub-section">
      <header class="hub-section-title">
        <div><h2>可用模型</h2><p>聊天和分析时可以选择这些模型</p></div>
        <button class="quiet-action" type="button" :disabled="!enabledPlatforms.length" @click="openNewModel()">＋ 添加模型</button>
      </header>
      <div class="friendly-model-list">
        <article v-for="item in configs" :key="item.id" :class="{ active: item.is_active, disabled: item.status === 'disabled' }">
          <div class="model-icon">✦</div>
          <div class="model-identity"><strong>{{ item.display_name }}</strong><span>{{ item.platform_name || item.provider_name }} · {{ item.model_name }}</span></div>
          <span v-if="item.is_active" class="default-badge">默认</span>
          <span class="model-health"><i></i>{{ item.status === 'enabled' ? '可用' : '停用' }}</span>
          <div class="model-row-actions">
            <button type="button" @click="testSavedConfig(item)">测试</button>
            <button v-if="!item.is_active" type="button" :disabled="busy" @click="activateConfig(item.id)">设为默认</button>
            <button type="button" @click="editConfig(item)">编辑</button>
          </div>
        </article>
        <div v-if="!configs.length" class="friendly-empty">平台连接成功后，点击“添加模型”即可完成最后一步。</div>
      </div>
    </section>

    <div v-if="dialog" class="model-dialog-backdrop" @mousedown.self="closeDialog">
      <form v-if="dialog === 'platform'" class="model-dialog" @submit.prevent="savePlatform">
        <header class="dialog-header"><div><small>{{ editingPlatformId ? '编辑连接' : `第 ${platformStep} 步，共 2 步` }}</small><h2>{{ editingPlatformId ? '编辑平台' : platformStep === 1 ? '选择模型平台' : '填写连接信息' }}</h2></div><button type="button" @click="closeDialog">×</button></header>
        <div v-if="platformStep === 1 && !editingPlatformId" class="provider-picker">
          <button v-for="preset in providerPresets" :key="preset.id" type="button" :class="{ selected: selectedPresetId === preset.id }" @click="applyPreset(preset.id); platformStep = 2">
            <b>{{ preset.name.slice(0, 1) }}</b><span>{{ preset.name }}</span><small>{{ preset.models[0]?.display_name }}</small>
          </button>
          <button type="button" @click="platformStep = 2"><b>↗</b><span>其他兼容平台</span><small>手动填写接口地址</small></button>
        </div>
        <div v-else class="dialog-body">
          <div class="friendly-tip"><b>连接 {{ platformForm.name }}</b><span>API Key 可在服务商控制台创建，我们会加密保存。</span></div>
          <label><span>给这个平台起个名字</span><input v-model="platformForm.name" required placeholder="例如：公司的百炼账号" /></label>
          <label><span>API Key</span><input v-model="platformForm.api_key" type="password" :placeholder="editingPlatformId ? '留空表示不修改' : '粘贴服务商提供的 API Key'" /></label>
          <details><summary>高级设置</summary><label><span>接口地址（Base URL）</span><input v-model="platformForm.base_url" required /></label><label><span>接口类型</span><input v-model="platformForm.provider_type" required /></label><label><span>备注</span><textarea v-model="platformForm.remark" rows="2" /></label></details>
          <div v-if="platformTest" class="test-result" :class="{ ok: platformTest.ok, fail: !platformTest.ok }"><strong>{{ testText(platformTest) }}</strong></div>
        </div>
        <footer v-if="platformStep === 2 || editingPlatformId"><button v-if="!editingPlatformId" type="button" @click="platformStep = 1">上一步</button><span></span><button type="button" :disabled="testingPlatform" @click="testPlatform">{{ testingPlatform ? '正在测试…' : '测试连接' }}</button><button class="primary-action" type="submit" :disabled="busy">{{ busy ? '保存中…' : '保存平台' }}</button></footer>
      </form>

      <form v-else class="model-dialog model-dialog--compact" @submit.prevent="saveConfig">
        <header class="dialog-header"><div><small>MODEL SETUP</small><h2>{{ editingConfigId ? '编辑模型' : '添加模型' }}</h2></div><button type="button" @click="closeDialog">×</button></header>
        <div class="dialog-body">
          <label><span>使用哪个平台？</span><select v-model="configForm.platform_id" required @change="applyPlatformToConfig"><option v-for="item in enabledPlatforms" :key="item.id" :value="item.id">{{ item.name }}（{{ keyState(item) }}）</option></select></label>
          <div v-if="commonModels.length" class="quick-models"><span>常用模型，点一下自动填写</span><div><button v-for="model in commonModels" :key="model.model_name" type="button" @click="fillModel(model)">{{ model.display_name }}</button></div></div>
          <label><span>模型名称 <small>展示给用户看</small></span><input v-model="configForm.display_name" required placeholder="例如：Qwen Plus 日常分析" /></label>
          <label><span>模型 ID <small>服务商提供的调用名称</small></span><input v-model="configForm.model_name" required placeholder="例如：qwen-plus" /></label>
          <label class="friendly-switch"><input v-model="configForm.is_active" type="checkbox" /><i></i><span><b>设为默认模型</b><small>新对话将优先使用它</small></span></label>
          <button class="advanced-toggle" type="button" @click="showAdvanced = !showAdvanced">{{ showAdvanced ? '收起' : '展开' }}高级参数 <span>⌄</span></button>
          <div v-if="showAdvanced" class="advanced-panel"><div class="form-row"><label><span>创造性（温度）</span><input v-model.number="configForm.temperature" type="number" min="0" max="2" step="0.1" /></label><label><span>最大工具步数</span><input v-model.number="configForm.max_steps" type="number" min="1" max="20" /></label><label><span>推理强度 <small>支持深度思考的模型</small></span><select v-model="configForm.thinking_mode"><option value="auto">跟随模型默认</option><option value="on">开启深度思考</option><option value="off">关闭深度思考</option></select></label></div><label><span>模型专属 Key <small>通常留空，继承平台</small></span><input v-model="configForm.api_key" type="password" /></label><label><span>备注</span><textarea v-model="configForm.remark" rows="2" /></label></div>
          <div v-if="configTest" class="test-result" :class="{ ok: configTest.ok, fail: !configTest.ok }"><strong>{{ testText(configTest) }}</strong><span v-if="configTest.response_preview">{{ configTest.response_preview }}</span></div>
        </div>
        <footer><button type="button" @click="closeDialog">取消</button><span></span><button type="button" :disabled="testingConfig" @click="testConfig">{{ testingConfig ? '正在测试…' : '测试模型' }}</button><button class="primary-action" type="submit" :disabled="busy">{{ busy ? '保存中…' : '保存模型' }}</button></footer>
      </form>
    </div>
  </section>
</template>
