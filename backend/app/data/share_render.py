"""分享页渲染：把推送正文（Markdown）变成手机上好读的自包含 HTML。

单一职责：只做“Markdown → 安全 HTML → 完整页面”，不碰数据库。

- 页面不带脚本、不加载任何外部资源，样式全部内联，飞书内置浏览器打开快。
- markdown-it 关掉原始 HTML，渲染结果再过一遍 nh3 白名单，防 XSS。
- 计划表和长文字表在窄屏上改成“一行一张卡片”，其他表格横向滑动、首列固定。
- 样式跟网站走（docs/design-system.md）：暗色暖墨金，token 名和取值与 styles.css 相同，正文同 .md-body。
"""
from __future__ import annotations

import html
import re

import nh3
from markdown_it import MarkdownIt
from markdown_it.common.utils import escapeHtml
from markdown_it.parser_inline import ParserInline
from markdown_it.rules_inline.state_inline import Scanned, StateInline
from markdown_it.token import Token

from backend.app.data.share_assets import LOGO_DATA_URI

TOC_MIN_SECTIONS = 4  # 超过 3 个二级标题才给目录
_LONG_CELL_CHARS = 24  # 三列以上的表里有这么长的单元格，横向滑动就不好读了，改用卡片

# CommonMark 不认“中文标点 + ** + 汉字”这样的加粗边界（如 **结论：**后文），页面会露出星号。
# 与前端 utils/markdown.ts 同一规则：与中日韩字符相邻时放宽 * 的开闭判定。
_CJK_CHAR = re.compile(
    "[⺀-⿿　-〿぀-ヿ㄀-ㄯ㆐-ㇿ㈀-䶿"
    "一-鿿豈-﫿︰-﹏＀-￯]"
)


class _CjkFriendlyInlineState(StateInline):
    def scanDelims(self, start: int, canSplitWord: bool) -> Scanned:  # noqa: N802 - 覆盖库方法
        scanned = super().scanDelims(start, canSplitWord)
        if self.src[start] != "*" or (scanned.can_open and scanned.can_close):
            return scanned
        before = self.src[start - 1] if start > 0 else ""
        end = start + scanned.length
        after = self.src[end] if end < self.posMax else ""
        if not _CJK_CHAR.match(before or " ") and not _CJK_CHAR.match(after or " "):
            return scanned
        return Scanned(
            scanned.can_open or (after != "" and not after.isspace()),
            scanned.can_close or (before != "" and not before.isspace()),
            scanned.length,
        )


class _CjkFriendlyParserInline(ParserInline):
    def parse(self, src, md, env, tokens):
        state = _CjkFriendlyInlineState(src, md, env, tokens)
        self.tokenize(state)
        for rule in self.ruler2.getRules(""):
            rule(state)
        return state.tokens


def _image_as_link(self, tokens, idx, options, env) -> str:
    # 页面不加载外部资源，图片和飞书卡片一样转成链接
    token = tokens[idx]
    return f'<a href="{escapeHtml(token.attrGet("src") or "")}">📷 {escapeHtml(token.content or "图片")}</a>'


def _build_markdown() -> MarkdownIt:
    markdown = MarkdownIt("js-default", {"breaks": True, "html": False})
    markdown.inline = _CjkFriendlyParserInline()
    markdown.add_render_rule("image", _image_as_link)
    return markdown


_MARKDOWN = _build_markdown()

_ALLOWED_TAGS = {
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "br", "hr", "strong", "em", "s", "del",
    "code", "pre", "blockquote", "ul", "ol", "li", "a",
    "table", "thead", "tbody", "tr", "th", "td",
}
_ALLOWED_ATTRIBUTES = {
    "a": {"href"},
    "h2": {"id"},
    "ol": {"start"},
    "td": {"data-label"},
}
_ALLOWED_CLASSES = {
    "p": {"lead"},
    "table": {"plan"},
    "th": {"ar", "ac"},
    "td": {"ar", "ac", "num", "act-buy", "act-sell", "act-stop"},
}
_ALLOWED_URL_SCHEMES = {"http", "https", "mailto", "tel"}

_ACTION_HEADER = re.compile(r"怎么做|操作|动作|方向|买卖")
_PRICE_HEADER = re.compile(r"价")
_QUANTITY_HEADER = re.compile(r"股数|数量")
_NO_ACTION = re.compile(r"不|别|暂缓|持有|拿着|观望|等待|买卖")
_SELL_ACTION = re.compile(r"卖|减仓|减半|减持|清仓|清掉")
_BUY_ACTION = re.compile(r"买|加仓|建仓")


def _inline_text(token: Token) -> str:
    parts: list[str] = []
    for child in token.children or []:
        if child.type in ("text", "code_inline"):
            parts.append(child.content)
        elif child.type in ("softbreak", "hardbreak"):
            parts.append(" ")
    return "".join(parts).strip()


def _action_class(cell_text: str) -> str | None:
    """只看操作列的第一小句：后面常跟条件和理由（“拿着，跌破 20 止损”“不参与：不再买”）。"""
    clause = re.split(r"[：:，,。；;]", cell_text.strip(), maxsplit=1)[0]
    if not clause or _NO_ACTION.search(clause):
        return None
    if "止损" in clause:
        return "act-stop"
    if _SELL_ACTION.search(clause):
        return "act-sell"
    if _BUY_ACTION.search(clause):
        return "act-buy"
    return None


def _move_align_to_class(token: Token) -> None:
    # 内联 style 不进白名单，表格对齐改用 class
    style = str(token.attrs.pop("style", "") or "")
    if "right" in style:
        token.attrJoin("class", "ar")
    elif "center" in style:
        token.attrJoin("class", "ac")


def _decorate_table(tokens: list[Token], start: int) -> int:
    """给一张表加手机卡片需要的标记，返回表格结束的位置。"""
    close = next(index for index in range(start, len(tokens)) if tokens[index].type == "table_close")
    headers = [
        _inline_text(tokens[index + 1])
        for index in range(start, close)
        if tokens[index].type == "th_open"
    ]
    action_column = next(
        (column for column, header in enumerate(headers) if _ACTION_HEADER.search(header)),
        None,
    )
    has_price = any(_PRICE_HEADER.search(header) for header in headers)
    has_quantity = any(_QUANTITY_HEADER.search(header) for header in headers)
    is_plan = (action_column is not None and (has_price or has_quantity)) or (has_price and has_quantity)
    has_long_cell = len(headers) >= 3 and any(
        tokens[index].type == "inline" and len(_inline_text(tokens[index])) > _LONG_CELL_CHARS
        for index in range(start, close)
    )
    as_cards = is_plan or has_long_cell
    if as_cards:
        tokens[start].attrSet("class", "plan")
    # 首列是卡片标题，不算价格或股数列
    numeric_columns = {
        column
        for column, header in enumerate(headers)
        if is_plan and column > 0 and (_PRICE_HEADER.search(header) or _QUANTITY_HEADER.search(header))
    }

    column = 0
    for index in range(start, close):
        token = tokens[index]
        if token.type == "tr_open":
            column = 0
        elif token.type in ("th_open", "td_open"):
            _move_align_to_class(token)
            if token.type == "td_open":
                label = headers[column] if column < len(headers) else ""
                if as_cards and label:
                    token.attrSet("data-label", label)
                if column in numeric_columns:
                    token.attrJoin("class", "num")
                if is_plan and column == action_column:
                    action_class = _action_class(_inline_text(tokens[index + 1]))
                    if action_class:
                        token.attrJoin("class", action_class)
            column += 1
    return close


def _mark_lead(tokens: list[Token]) -> None:
    """正文开头的第一段是结论，加底色；开头不是段落（比如先来一张表）就不标。"""
    for token in tokens:
        if token.level != 0:
            continue
        if token.type == "paragraph_open":
            token.attrJoin("class", "lead")
            return
        if token.type not in ("heading_open", "heading_close", "hr"):
            return


def _wrap_tables(cleaned: str) -> str:
    # 清洗后的表格标签没有其他属性，正文里的尖括号都已转义，可以放心按字面替换
    return (
        cleaned.replace('<table class="plan">', '<div class="table-wrap cards"><table class="plan">')
        .replace("<table>", '<div class="table-wrap"><table>')
        .replace("</table>", "</table></div>")
    )


def render_markdown(source: str) -> tuple[str, list[tuple[str, str]]]:
    """返回 (安全的正文 HTML, [(锚点, 二级标题)])。"""
    env: dict = {}
    tokens = _MARKDOWN.parse((source or "").strip(), env)
    sections: list[tuple[str, str]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.type == "table_open":
            index = _decorate_table(tokens, index)
        elif token.type == "heading_open" and token.tag == "h2":
            anchor = f"s{len(sections) + 1}"
            token.attrSet("id", anchor)
            sections.append((anchor, _inline_text(tokens[index + 1])))
        index += 1
    _mark_lead(tokens)
    rendered = _MARKDOWN.renderer.render(tokens, _MARKDOWN.options, env)
    cleaned = nh3.clean(
        rendered,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        allowed_classes=_ALLOWED_CLASSES,
        url_schemes=_ALLOWED_URL_SCHEMES,
        link_rel="noopener noreferrer nofollow",
    )
    return _wrap_tables(cleaned), sections


_PAGE_CSS = """
:root{color-scheme:dark;--bg:#050505;--bg-2:#0b0b0c;--surface:#111113;--surface-2:#171719;--surface-3:#1d1c18;
--text:#f2f0e8;--muted:#9d9a8f;--muted-2:#6d6a61;--line:#2a2925;--line-strong:#4c4430;
--gold:#d5b46a;--gold-2:#f1d48a;--gold-dim:#7d6736;--cyan:#2bd4d4;--danger:#ff5d5d;--warning:#d5a642;--ok:#27c58a;
--up:var(--danger);--down:var(--ok);--r-ctrl:4px;--r-panel:8px;--r-pill:999px}
*{box-sizing:border-box}
html{background:var(--bg);-webkit-text-size-adjust:100%;scroll-behavior:smooth}
body{margin:0;min-height:100vh;background-color:var(--bg);background-image:linear-gradient(135deg,rgba(213,180,106,.08),transparent 28%),linear-gradient(315deg,rgba(43,212,212,.06),transparent 32%);background-attachment:fixed;color:var(--text);font-family:"Microsoft YaHei UI","Microsoft YaHei","PingFang SC","Noto Sans CJK SC",system-ui,sans-serif;font-size:14px;line-height:1.6;font-variant-numeric:tabular-nums}
.topbar{border-bottom:1px solid var(--line);background:linear-gradient(180deg,rgba(213,180,106,.08),transparent),#080809}
.brand{display:flex;align-items:center;gap:8px;max-width:720px;margin:0 auto;padding:8px 16px}
.brand img{display:block;width:28px;height:28px;border:1px solid var(--gold-dim);border-radius:var(--r-ctrl);background:#000}
.brand strong{color:var(--gold-2);font-size:13px;letter-spacing:.02em}
.brand small{color:var(--muted);font-size:12px}
.page{max-width:720px;margin:0 auto;padding:16px 16px 32px}
.page-head{padding-bottom:12px;border-bottom:1px solid var(--line)}
.page-title{margin:0;color:var(--text);font-size:20px;font-weight:500;line-height:1.3}
.page-meta{margin:4px 0 0;color:var(--muted);font-size:12px;line-height:1.5}
.plan-link{display:flex;align-items:center;justify-content:center;min-height:44px;margin-top:12px;border:1px solid var(--gold-dim);border-radius:var(--r-ctrl);background:var(--surface-2);color:var(--gold-2);font-size:13px;font-weight:600;text-decoration:none}
.toc{margin:12px 0 0;padding:8px 12px;border:1px solid var(--line);border-radius:var(--r-panel);background:var(--surface);font-size:13px}
.toc summary{color:var(--gold-2);font-weight:500;cursor:pointer}
.toc ol{margin:8px 0 4px;padding-left:20px}
.toc li{margin:4px 0}
.toc a{color:#e5dfce;text-decoration:none}
.report{color:#e5dfce;font-size:14px;line-height:1.72;overflow-wrap:anywhere}
.report>:first-child{margin-top:16px}
.report h1,.report h2,.report h3,.report h4,.report h5,.report h6{margin:20px 0 8px;color:var(--gold-2);font-weight:600;line-height:1.35}
.report h1{font-size:18px}
.report h2{padding-bottom:5px;border-bottom:1px solid rgba(213,180,106,.18);font-size:17px;scroll-margin-top:12px}
.report h3{font-size:16px}
.report h4,.report h5,.report h6{font-size:14px}
.report p{margin:8px 0}
.report strong{color:#f4e4b8;font-weight:700}
.report .lead{margin:16px 0;padding:12px;border:1px solid var(--line-strong);border-radius:var(--r-panel);background:rgba(213,180,106,.08);box-shadow:inset 2px 0 var(--gold)}
.report ul,.report ol{margin:8px 0;padding-left:22px}
.report li{margin:4px 0}
.report hr{margin:20px 0;border:0;border-top:1px solid var(--line)}
.report a{color:var(--cyan);text-decoration:none}
.report blockquote{margin:12px 0;padding:8px 12px;border-left:3px solid rgba(43,212,212,.45);border-radius:0 var(--r-ctrl) var(--r-ctrl) 0;background:rgba(43,212,212,.055);color:#c9e7e7}
.report code{padding:1px 5px;border:1px solid rgba(213,180,106,.18);border-radius:var(--r-ctrl);background:rgba(213,180,106,.09);color:var(--gold-2);font-size:12px}
.report pre{margin:12px 0;padding:8px 12px;border:1px solid var(--line);border-radius:var(--r-panel);background:#060606;overflow-x:auto;overflow-wrap:normal;line-height:1.55}
.report pre code{padding:0;border:0;background:none;color:#e8dec5}
.table-wrap{margin:12px 0;overflow-x:auto;-webkit-overflow-scrolling:touch;border:1px solid var(--line-strong);border-radius:var(--r-panel);background:rgba(7,7,8,.55)}
table{width:100%;border-collapse:separate;border-spacing:0;font-size:13px;line-height:1.55;overflow-wrap:normal}
th,td{min-width:4.5em;padding:8px;text-align:left;vertical-align:top;border-bottom:1px solid var(--line)}
tbody tr:last-child td{border-bottom:0}
th{background:#171510;color:var(--gold-2);font-weight:500}
th:first-child,td:first-child{position:sticky;left:0;max-width:9em;box-shadow:1px 0 0 var(--line)}
td:first-child{background:var(--bg-2)}
.ar{text-align:right}
.ac{text-align:center}
.num{color:#f4e4b8;font-weight:600}
.act-buy{color:var(--up);font-weight:600}
.act-sell{color:var(--down);font-weight:600}
.act-stop{background:rgba(213,166,66,.1);color:#e6c36f;font-weight:600}
.page-foot{margin-top:24px;padding-top:12px;border-top:1px solid var(--line);color:var(--muted);font-size:12px;line-height:1.5}
.notice{padding:72px 16px;text-align:center}
.notice h1{margin:0 0 8px;color:var(--text);font-size:14px;font-weight:500}
.notice p{margin:0;color:var(--muted);font-size:12px}
@media (max-width:680px){
.table-wrap.cards{overflow:visible;border:0;border-radius:0;background:none}
.plan,.plan tbody,.plan tr,.plan td{display:block;width:100%}
.plan thead{display:none}
.plan tr{margin-bottom:8px;padding:12px;border:1px solid var(--line);border-radius:var(--r-panel);background:var(--surface)}
.plan tbody tr:last-child{margin-bottom:0}
.plan td,.plan tbody tr:last-child td{min-width:0;max-width:none;padding:3px 0;border:0;box-shadow:none;text-align:left}
.plan td:first-child{position:static;margin-bottom:6px;padding:0 0 8px;border-bottom:1px solid var(--line);background:none;color:var(--gold-2);font-size:14px;font-weight:600}
.plan td:empty{display:none}
.plan td[data-label]::before{content:attr(data-label) "：";color:var(--muted);font-weight:400}
.plan td:first-child::before{content:none}
.plan .act-stop{width:auto;margin:3px -6px;padding:3px 6px;border-radius:var(--r-ctrl)}
}
"""


_TOPBAR = (
    f'<div class="topbar"><div class="brand"><img src="{LOGO_DATA_URI}" alt="">'
    "<strong>SixRabbits</strong><small>A股研究台</small></div></div>"
)


def page_shell(title: str, body: str, *, extra_css: str = "") -> str:
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow">'
        '<meta name="color-scheme" content="dark"><meta name="theme-color" content="#050505">'
        f"<title>{html.escape(title)}</title><style>{_PAGE_CSS}{extra_css}</style></head>"
        f"<body>{_TOPBAR}{body}</body></html>"
    )


def _display_title(title: str) -> str:
    # 卡片标题带日期（“早盘挂单清单 | 2026-11-04”），页面把日期放到标题下面那行
    return re.sub(r"\s*[|｜]\s*\d{4}-\d{2}-\d{2}$", "", title).strip() or title


def _short_time(value: str) -> str:
    matched = re.fullmatch(r"\d{4}-(\d{2}-\d{2} \d{2}:\d{2})(?::\d{2})?", value.strip())
    return matched.group(1) if matched else value.strip()


def render_share_page(*, title: str, meta: dict, content_md: str, expires_on: str) -> str:
    """完整的分享页。expires_on 是展示用的到期日期（北京时间）。"""
    body, sections = render_markdown(content_md)
    generated_at = _short_time(str(meta.get("generated_at") or ""))
    subtitle = " · ".join(
        part for part in (generated_at, str(meta.get("digital_human") or "").strip()) if part
    )
    toc = ""
    if len(sections) >= TOC_MIN_SECTIONS:
        items = "".join(
            f'<li><a href="#{anchor}">{html.escape(text)}</a></li>' for anchor, text in sections
        )
        toc = f'<details class="toc"><summary>目录</summary><ol>{items}</ol></details>'
    foot_parts = [f"生成于 {generated_at}" if generated_at else "", f"链接有效期至 {expires_on}"]
    foot = " · ".join(part for part in foot_parts if part)
    meta_line = f'<p class="page-meta">{html.escape(subtitle)}</p>' if subtitle else ""
    plan_url = str(meta.get("plan_url") or "")
    plan_link = (
        f'<a class="plan-link" href="{html.escape(plan_url)}">看今天的计划进度</a>'
        if plan_url.startswith(("https://", "http://"))
        else ""
    )
    return page_shell(
        title,
        '<main class="page"><header class="page-head">'
        f'<h1 class="page-title">{html.escape(_display_title(title))}</h1>{meta_line}{plan_link}</header>'
        f'{toc}<article class="report">{body}</article>'
        f'<footer class="page-foot">{html.escape(foot)}</footer></main>',
    )


def render_notice_page(heading: str, detail: str) -> str:
    """链接过期、作废或不存在时的提示页。"""
    return page_shell(
        heading,
        f'<main class="page notice"><h1>{html.escape(heading)}</h1><p>{html.escape(detail)}</p></main>',
    )
