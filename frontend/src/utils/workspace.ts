/**
 * 从回复的 ReAct 步骤里提取本次真正生成的工作区文件（ws_write_file 的 path 参数）。
 *
 * 下载按钮只在"确实生成了文件"时出现，且下载的是工作区里的真实文件——
 * 不是把聊天正文打包。普通问答没有 ws_write_file 动作，自然无按钮。
 */
export type GeneratedFile = { path: string; name: string }

export function extractGeneratedFiles(steps: Array<{ type: string; tool?: string; content: string }>): GeneratedFile[] {
  const files: GeneratedFile[] = []
  const seen = new Set<string>()
  for (const step of steps) {
    if (step.type !== 'action' || step.tool !== 'ws_write_file') continue
    try {
      const args = JSON.parse(step.content)
      const path = typeof args.path === 'string' ? args.path.trim() : ''
      if (path && !seen.has(path)) {
        seen.add(path)
        files.push({ path, name: path.split('/').pop() || path })
      }
    } catch {
      // action content 非 JSON（异常参数）时忽略该步
    }
  }
  return files
}
