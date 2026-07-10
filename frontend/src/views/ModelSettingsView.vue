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
    remark: row.remark || ''
  })
  configTest.value = null
  configMessage.value = ''
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
  <section class="model-settings">
    <section class="model-status-strip page-section">
      <div>
        <span>默认模型</span>
        <strong>{{ activeModel ? `${activeModel.display_name} / ${activeModel.model_name}` : '未设置' }}</strong>
      </div>
      <div>
        <span>平台</span>
        <strong>{{ platforms.length }}</strong>
      </div>
      <div>
        <span>模型配置</span>
        <strong>{{ configs.length }}</strong>
      </div>
      <div>
        <span>可用平台</span>
        <strong>{{ enabledPlatforms.length }}</strong>
      </div>
    </section>

    <section class="model-settings-grid">
      <form class="editor-panel model-editor" @submit.prevent="savePlatform">
        <header>
          <h2>{{ editingPlatformId ? '编辑模型平台' : '新增模型平台' }}</h2>
          <button type="button" @click="resetPlatformForm">新建</button>
        </header>

        <label>
          <span>平台预设</span>
          <select v-model="selectedPresetId" @change="applyPreset()">
            <option v-for="preset in providerPresets" :key="preset.id" :value="preset.id">
              {{ preset.name }}
            </option>
          </select>
        </label>

        <label>
          <span>平台名称</span>
          <input v-model="platformForm.name" required placeholder="例如：阿里云百炼" />
        </label>

        <label>
          <span>接口类型</span>
          <input v-model="platformForm.provider_type" required />
        </label>

        <label>
          <span>Base URL</span>
          <input v-model="platformForm.base_url" required placeholder="https://.../v1" />
        </label>

        <label>
          <span>API Key</span>
          <input v-model="platformForm.api_key" type="password" placeholder="编辑时留空保留原 Key" />
        </label>

        <label>
          <span>状态</span>
          <select v-model="platformForm.status">
            <option value="enabled">启用</option>
            <option value="disabled">停用</option>
          </select>
        </label>

        <label>
          <span>备注</span>
          <textarea v-model="platformForm.remark" rows="3"></textarea>
        </label>

        <div class="form-actions">
          <button type="button" :disabled="testingPlatform || busy" @click="testPlatform">
            {{ testingPlatform ? '测试中' : '测试平台' }}
          </button>
          <button type="submit" :disabled="busy">保存平台</button>
        </div>

        <p v-if="platformMessage" class="form-message">{{ platformMessage }}</p>
        <div v-if="platformTest" class="test-result" :class="{ ok: platformTest.ok, fail: !platformTest.ok }">
          <strong>{{ testText(platformTest) }}</strong>
          <span v-if="platformTest.models?.length">模型：{{ platformTest.models.slice(0, 6).join(', ') }}</span>
        </div>
      </form>

      <form class="editor-panel model-editor" @submit.prevent="saveConfig">
        <header>
          <h2>{{ editingConfigId ? '编辑模型配置' : '新增模型配置' }}</h2>
          <button type="button" @click="resetConfigForm">新建</button>
        </header>

        <label>
          <span>所属平台</span>
          <select v-model="configForm.platform_id" @change="applyPlatformToConfig">
            <option :value="null">自定义直连</option>
            <option v-for="item in enabledPlatforms" :key="item.id" :value="item.id">
              {{ item.name }} · {{ keyState(item) }}
            </option>
          </select>
        </label>

        <div v-if="commonModels.length" class="model-chip-row">
          <button v-for="model in commonModels" :key="model.model_name" type="button" @click="fillModel(model)">
            {{ model.display_name }}
          </button>
        </div>

        <label>
          <span>显示名称</span>
          <input v-model="configForm.display_name" required placeholder="例如：百炼 Qwen Plus" />
        </label>

        <label>
          <span>模型 ID</span>
          <input v-model="configForm.model_name" required placeholder="qwen-plus / gpt-4.1-mini" />
        </label>

        <label>
          <span>Provider</span>
          <input v-model="configForm.provider_name" required />
        </label>

        <label>
          <span>生效 Base URL</span>
          <input v-model="configForm.base_url" :readonly="!!selectedPlatform" required />
        </label>

        <label>
          <span>模型专属 API Key</span>
          <input v-model="configForm.api_key" type="password" placeholder="通常留空，继承平台 Key" />
        </label>

        <div class="form-row">
          <label>
            <span>温度</span>
            <input v-model.number="configForm.temperature" type="number" min="0" max="2" step="0.1" />
          </label>
          <label>
            <span>Agent 步数</span>
            <input v-model.number="configForm.max_steps" type="number" min="1" max="20" />
          </label>
        </div>

        <label>
          <span>状态</span>
          <select v-model="configForm.status">
            <option value="enabled">启用</option>
            <option value="disabled">停用</option>
          </select>
        </label>

        <label class="checkbox-line">
          <input v-model="configForm.is_active" type="checkbox" />
          <span>设为默认模型</span>
        </label>

        <label>
          <span>备注</span>
          <textarea v-model="configForm.remark" rows="3"></textarea>
        </label>

        <div class="form-actions">
          <button type="button" :disabled="testingConfig || busy" @click="testConfig">
            {{ testingConfig ? '测试中' : '测试模型' }}
          </button>
          <button type="submit" :disabled="busy">保存模型</button>
        </div>

        <p v-if="configMessage" class="form-message">{{ configMessage }}</p>
        <div v-if="configTest" class="test-result" :class="{ ok: configTest.ok, fail: !configTest.ok }">
          <strong>{{ testText(configTest) }}</strong>
          <span v-if="configTest.response_preview">返回：{{ configTest.response_preview }}</span>
        </div>
      </form>
    </section>

    <section class="page-section model-list-panel">
      <header class="section-header">
        <div>
          <h2>模型平台</h2>
          <p>平台负责 Base URL 和公共 API Key，模型配置默认继承平台连接。</p>
        </div>
      </header>

      <div class="model-card-grid">
        <article v-for="item in platforms" :key="item.id" class="model-card" :class="{ disabled: item.status === 'disabled' }">
          <header>
            <strong>{{ item.name }}</strong>
            <span>{{ item.status === 'enabled' ? '启用' : '停用' }}</span>
          </header>
          <p>{{ item.provider_type }}</p>
          <code>{{ item.base_url }}</code>
          <div class="model-meta-row">
            <span>Key: {{ keyState(item) }}</span>
            <span>{{ platformModelCount(item.id) }} 个模型</span>
          </div>
          <div class="card-actions">
            <button type="button" @click="editPlatform(item)">编辑</button>
            <button type="button" @click="configForm.platform_id = item.id; applyPlatformToConfig()">添加模型</button>
          </div>
        </article>
      </div>
    </section>

    <section class="page-section model-list-panel">
      <header class="section-header">
        <div>
          <h2>模型配置</h2>
          <p>问股和数字人会话只会使用启用的模型配置。</p>
        </div>
        <button type="button" :disabled="!selectedPlatform || busy" @click="createCommonModels">创建当前平台常用模型</button>
      </header>

      <div class="model-table">
        <div class="model-table-head">
          <span>名称</span>
          <span>平台</span>
          <span>模型 ID</span>
          <span>默认</span>
          <span>Key</span>
          <span>状态</span>
          <span>操作</span>
        </div>
        <div v-for="item in configs" :key="item.id" class="model-table-row" :class="{ active: item.is_active }">
          <strong>{{ item.display_name }}</strong>
          <span>{{ item.platform_name || item.provider_name }}</span>
          <code>{{ item.model_name }}</code>
          <span>{{ item.is_active ? '默认' : '-' }}</span>
          <span>{{ keyState(item) }}</span>
          <span>{{ item.status === 'enabled' ? '启用' : '停用' }}</span>
          <div class="table-actions">
            <button type="button" @click="editConfig(item)">编辑</button>
            <button type="button" @click="testSavedConfig(item)">测试</button>
            <button type="button" :disabled="item.is_active || busy" @click="activateConfig(item.id)">设默认</button>
          </div>
        </div>
        <div v-if="!configs.length" class="session-empty">暂无模型配置</div>
      </div>
    </section>
  </section>
</template>
