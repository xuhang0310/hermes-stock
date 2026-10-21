import DOMPurify from 'dompurify'
import MarkdownIt from 'markdown-it'

const markdown = new MarkdownIt({
  breaks: true,
  html: false,
  linkify: true,
  typographer: false
})

// CommonMark 不认“中文标点 + ** + 汉字”这样的加粗边界（如 **研究推断：**最有价值），页面会露出星号。
// 与中日韩字符相邻时放宽 * 的开闭判定；英文、数字和 _ 仍按原规则。
const CJK_CHAR = /[\u2e80-\u2fff\u3000-\u303f\u3040-\u30ff\u3100-\u312f\u3190-\u31ff\u3200-\u4dbf\u4e00-\u9fff\uf900-\ufaff\ufe30-\ufe4f\uff00-\uffef]/

class CjkFriendlyInlineState extends markdown.inline.State {
  scanDelims(start: number, canSplitWord: boolean) {
    const scanned = super.scanDelims(start, canSplitWord)
    if (this.src.charCodeAt(start) !== 0x2a || (scanned.can_open && scanned.can_close)) return scanned
    const before = start > 0 ? this.src.charAt(start - 1) : ''
    const end = start + scanned.length
    const after = end < this.posMax ? this.src.charAt(end) : ''
    if (!CJK_CHAR.test(before) && !CJK_CHAR.test(after)) return scanned
    return {
      ...scanned,
      can_open: scanned.can_open || (after !== '' && !/\s/.test(after)),
      can_close: scanned.can_close || (before !== '' && !/\s/.test(before))
    }
  }
}

markdown.inline.State = CjkFriendlyInlineState

const defaultLinkOpen =
  markdown.renderer.rules.link_open ||
  ((tokens, idx, options, _env, self) => self.renderToken(tokens, idx, options))

markdown.renderer.rules.link_open = (tokens, idx, options, env, self) => {
  const token = tokens[idx]
  const href = token.attrGet('href') || ''
  if (/^https?:\/\//i.test(href)) {
    token.attrSet('target', '_blank')
    token.attrSet('rel', 'noopener noreferrer')
  }
  return defaultLinkOpen(tokens, idx, options, env, self)
}

export function renderMarkdown(source: string) {
  const rendered = markdown.render(source || '')
  return DOMPurify.sanitize(rendered, {
    ADD_ATTR: ['target', 'rel'],
    ALLOWED_URI_REGEXP: /^(?:(?:https?|mailto|tel):|[^a-z]|[a-z+.-]+(?:[^a-z+.-:]|$))/i
  })
}
