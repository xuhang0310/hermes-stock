import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

const frontendRoot = fileURLToPath(new URL('../', import.meta.url))

// Node 里没有 DOM，DOMPurify 无法工作；替换成原样返回，只测 markdown-it 的渲染。
const dompurifyStub = {
  name: 'dompurify-stub',
  enforce: 'pre',
  resolveId: (id) => (id === 'dompurify' ? '\0dompurify-stub' : null),
  load: (id) => (id === '\0dompurify-stub' ? 'export default { sanitize: (html) => html }' : null)
}

async function loadMarkdown() {
  const server = await createServer({
    root: frontendRoot,
    appType: 'custom',
    plugins: [dompurifyStub],
    ssr: { noExternal: ['dompurify'] },
    optimizeDeps: { noDiscovery: true },
    server: { middlewareMode: true, hmr: false }
  })
  try {
    return await server.ssrLoadModule('/src/utils/markdown.ts')
  } finally {
    await server.close()
  }
}

test('bold next to Chinese punctuation renders instead of leaking asterisks', async () => {
  const { renderMarkdown } = await loadMarkdown()
  const cases = [
    ['**研究推断：**最有价值的催化', '<strong>研究推断：</strong>最有价值的催化'],
    ['价格**（57.32）**之上', '价格<strong>（57.32）</strong>之上'],
    ['**今日要点**：这批股票', '<strong>今日要点</strong>：这批股票'],
    ['不等于“**相对底部**”，更', '不等于“<strong>相对底部</strong>”，更'],
    ['1. **现在在底部区间。** 55-58 元', '<strong>现在在底部区间。</strong> 55-58 元']
  ]
  for (const [source, expected] of cases) {
    const html = renderMarkdown(source)
    assert.ok(html.includes(expected), `${source} => ${html}`)
    assert.ok(!html.includes('**'), `${source} leaked asterisks: ${html}`)
  }
})

test('ASCII emphasis and underscores keep CommonMark behaviour', async () => {
  const { renderMarkdown } = await loadMarkdown()
  assert.equal(renderMarkdown('2*3*4'), '<p>2<em>3</em>4</p>\n')
  assert.equal(renderMarkdown('snake_case_name'), '<p>snake_case_name</p>\n')
  assert.equal(renderMarkdown('a ** b ** c'), '<p>a ** b ** c</p>\n')
  assert.equal(renderMarkdown('涨幅 5 * 2 倍'), '<p>涨幅 5 * 2 倍</p>\n')
})
