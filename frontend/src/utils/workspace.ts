/**
 * 判断一条助手回复是否值得给"存入工作区"入口。
 *
 * 工作区是研究产物层（报告/笔记/分析），不该给寒暄闲聊挂存盘按钮。
 * 判据：用过工具（做了研究）｜或内容像文档（markdown 标题/表格/代码块/多条列表）。
 * 闲聊回复既没调工具、也没结构，自然不显示。
 */
export function isWorkspaceSaveworthy(content: string, usedTools: boolean): boolean {
  const c = (content || '').trim()
  if (!c) return false
  if (usedTools) return true // 调过工具 = 做了研究/取数
  if (/(^|\n)#{1,6}\s/.test(c)) return true // markdown 标题
  if (/\n[ \t]*\|.+\|/.test(c)) return true // 表格行
  if (c.includes('```')) return true // 代码块
  const bullets = (c.match(/(^|\n)[ \t]*([-*]|\d+\.)[ \t]/g) || []).length
  return bullets >= 3 // ≥3 条列表 = 像清单/报告
}

/** 把一段 markdown 文本作为 .md 文件直接下载（客户端 Blob，无需服务端，点开即用编辑器/Word 打开）。 */
export function downloadMarkdown(content: string, baseName: string): void {
  const name =
    (baseName || '')
      .trim()
      .slice(0, 40)
      .replace(/[^\w一-龥-]+/g, '_')
      .replace(/^_+|_+$/g, '') || 'report'
  const blob = new Blob([content], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${name}.md`
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}
