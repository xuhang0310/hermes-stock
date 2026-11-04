"""分享页渲染：把推送正文（Markdown）变成手机上好读的自包含 HTML。

单一职责：只做“Markdown → 安全 HTML → 完整页面”，不碰数据库。

- 页面不带脚本、不加载任何外部资源，样式全部内联，飞书内置浏览器打开快。
- markdown-it 关掉原始 HTML，渲染结果再过一遍 nh3 白名单，防 XSS。
- 研报版式：抬头、结论、统计条、账本行，靠细线和字号分层。颜色取自网站（docs/design-system.md），
  token 取值与 styles.css 相同；抬头和放大的数字用内嵌的衬线数字字体，这是规范里写明的例外。
- 计划表变成账本行，长文字表变成标题加分行，其他表格横向滑动、首列固定。
"""
from __future__ import annotations

import copy
import html
import re
from datetime import date, datetime
from typing import NamedTuple

import nh3
from markdown_it import MarkdownIt
from markdown_it.common.utils import escapeHtml
from markdown_it.parser_inline import ParserInline
from markdown_it.rules_inline.state_inline import Scanned, StateInline
from markdown_it.token import Token

from backend.app.data.share_assets import FIGURE_FONT_DATA_URI

TOC_MIN_SECTIONS = 4  # 超过 3 个二级标题才给目录
_LONG_CELL_CHARS = 24  # 三列以上的表里有这么长的单元格，横向滑动就不好读了，改成分行

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
    "code", "pre", "blockquote", "ul", "ol", "li", "a", "span",
    "table", "thead", "tbody", "tr", "th", "td",
}
_ALLOWED_ATTRIBUTES = {
    "a": {"href"},
    "h2": {"id"},
    "ol": {"start"},
    "td": {"data-label", "data-tag"},
}
_ALLOWED_CLASSES = {
    "p": {"lead", "lead-big", "lead-end"},
    "table": {"plan", "lines"},
    "th": {"ar", "ac"},
    "td": {
        "ar", "ac", "fig", "dup",
        "c-name", "c-act", "c-price", "c-qty", "c-line",
        "act-buy", "act-sell", "act-stop", "act-none",
    },
    "span": {"nm", "cd", "now", "px", "lb", "big", "px-note", "fig"},
}
_ALLOWED_URL_SCHEMES = {"http", "https", "mailto", "tel"}

_ACTION_HEADER = re.compile(r"怎么做|操作|动作|方向|买卖")
_NAME_HEADER = re.compile(r"股票|标的|名称|证券")
_PRICE_HEADER = re.compile(r"价")
_QUANTITY_HEADER = re.compile(r"股数|数量")
_NO_ACTION = re.compile(r"不|别|暂缓|持有|拿着|观望|等待|买卖")
_SELL_ACTION = re.compile(r"卖|减仓|减半|减持|清仓|清掉")
_BUY_ACTION = re.compile(r"买|加仓|建仓")
_ACTION_TAGS = {"act-sell": "卖出", "act-buy": "买入", "act-stop": "止损"}
_CLAUSE_SPLIT = re.compile(r"[：:，,。；;]")

# “同力天启605286（21.57元）”“示例科技 22.35”：拆成名称、代码、现价
_NAME_CELL = re.compile(
    r"^(?P<name>[^\d（(]+?)\s*(?P<code>\d{6})?(?:\.[A-Z]{2})?\s*"
    r"(?:[（(]\s*(?:最新价|现价|收盘价)?\s*(?P<quoted>\d+(?:\.\d+)?)\s*元?\s*[）)]|\s(?P<bare>\d+\.\d+))?$"
)
_BOLD_PRICE = re.compile(r"(\d+(?:\.\d+)?)\s*元?")
_LIMIT_PRICE = re.compile(r"限价\s*(\d+\.\d+)")
_DECIMAL_PRICE = re.compile(r"(?<![\d.:])(\d+\.\d+)")
_WHOLE_PRICE = re.compile(r"\s*(?:限价)?\s*(\d{1,5})\s*元?\s*")
_PRICE_WORD = re.compile(r"(最高限价|限价|挂单价|卖价|买价|触发价|止损价|止盈价)\s*$")
_RESIDUE_PUNCTUATION = re.compile(r"[\s，,。；;：:元]")
_NUMERIC_CELL = re.compile(r"[-+]?[\d,]+(?:\.\d+)?\s*[%％元股]?")
_EMPTY_CELLS = frozenset({"", "—", "-", "--", "/"})
_LEAD_BIG_MAX_CHARS = 80  # 第一句太长就不放大，整段按普通正文


class _PlanColumns(NamedTuple):
    name: int
    action: int | None
    price: int | None
    quantity: int | None


def _inline_text(token: Token) -> str:
    parts: list[str] = []
    for child in token.children or []:
        if child.type in ("text", "code_inline"):
            parts.append(child.content)
        elif child.type in ("softbreak", "hardbreak"):
            parts.append(" ")
    return "".join(parts).strip()


def _first_clause(text: str) -> str:
    return _CLAUSE_SPLIT.split(text.strip(), maxsplit=1)[0]


def _action_class(cell_text: str) -> str | None:
    """只看操作列的第一小句：后面常跟条件和理由（“拿着，跌破 20 止损”“不参与：不再买”）。"""
    clause = _first_clause(cell_text)
    if not clause or _NO_ACTION.search(clause):
        return None
    if "止损" in clause:
        return "act-stop"
    if _SELL_ACTION.search(clause):
        return "act-sell"
    if _BUY_ACTION.search(clause):
        return "act-buy"
    return None


def _neutral_tag(cell_text: str) -> str:
    """没有买卖动作的行，第一小句够短就当标签（拿着、等待、不参与）；各种说法的持有都归成“拿着”。"""
    clause = _first_clause(cell_text)
    if "持有" in clause or "拿着" in clause:
        return "拿着"
    return clause if 0 < len(clause) <= 4 and not re.search(r"\d", clause) else ""


def _html_token(content: str) -> Token:
    # 只有这里生成的标记能以原样进入输出：用户内容都先转义，最后还要过 nh3 白名单
    return Token(type="html_inline", tag="", nesting=0, content=content)


def _rewrite_name_cell(inline: Token) -> None:
    matched = _NAME_CELL.match(_inline_text(inline))
    if not matched or not (matched.group("code") or matched.group("quoted") or matched.group("bare")):
        return
    parts = [f'<span class="nm">{escapeHtml(matched.group("name"))}</span>']
    if matched.group("code"):
        parts.append(f'<span class="cd fig">{matched.group("code")}</span>')
    current = matched.group("quoted") or matched.group("bare")
    if current:
        parts.append(f'<span class="now">现价 <span class="fig">{current}</span></span>')
    inline.children = [_html_token("".join(parts))]


def _main_price(inline: Token) -> str | None:
    children = inline.children or []
    for index, child in enumerate(children[:-1]):
        if child.type == "strong_open" and children[index + 1].type == "text":
            matched = _BOLD_PRICE.fullmatch(children[index + 1].content.strip())
            if matched:
                return matched.group(1)
    text = _inline_text(inline)
    matched = _LIMIT_PRICE.search(text) or _DECIMAL_PRICE.search(text) or _WHOLE_PRICE.fullmatch(text)
    return matched.group(1) if matched else None


def _rewrite_price_cell(inline: Token) -> bool:
    """把价格放大显示；单元格里还有别的话（如“9:15集合竞价”）就整句留在下面，一个字都不丢。"""
    price = _main_price(inline)
    if price is None:
        return False
    text = _inline_text(inline)
    position = text.find(price)
    before, after = text[:position], text[position + len(price):]
    word = _PRICE_WORD.search(before)
    label = word.group(1) if word else ""
    residue = _RESIDUE_PUNCTUATION.sub("", (before[: word.start()] if word else before) + after)
    label_html = f'<span class="lb">{escapeHtml(label)}</span>' if label else ""
    content = f'<span class="px">{label_html}<span class="fig big">{price}</span></span>'
    if residue:
        content += f'<span class="px-note">{escapeHtml(text)}</span>'
    inline.children = [_html_token(content)]
    return True


def _plan_columns(headers: list[str]) -> _PlanColumns | None:
    """认出计划表：既说了怎么做又有价格或股数，或者同时有价格和股数。"""
    action = next((i for i, header in enumerate(headers) if _ACTION_HEADER.search(header)), None)
    has_price = any(_PRICE_HEADER.search(header) for header in headers)
    quantity = next((i for i, header in enumerate(headers) if _QUANTITY_HEADER.search(header)), None)
    if not ((action is not None and (has_price or quantity is not None)) or (has_price and quantity is not None)):
        return None
    name = next((i for i, header in enumerate(headers) if _NAME_HEADER.search(header)), 0)
    # 首列“股票（最新价）”里的价是现价，不是计划价
    price = next(
        (i for i, header in enumerate(headers) if i != name and _PRICE_HEADER.search(header)),
        None,
    )
    return _PlanColumns(name, action, price, quantity)


def _move_align_to_class(token: Token) -> None:
    # 内联 style 不进白名单，表格对齐改用 class
    style = str(token.attrs.pop("style", "") or "")
    if "right" in style:
        token.attrJoin("class", "ar")
    elif "center" in style:
        token.attrJoin("class", "ac")


def _labelled_line(cell: Token, headers: list[str], column: int) -> None:
    cell.attrJoin("class", "c-line")
    if column < len(headers) and headers[column]:
        cell.attrSet("data-label", headers[column])


def _decorate_plan_row(row: dict[int, tuple[Token, Token]], headers: list[str], columns: _PlanColumns) -> None:
    action = row.get(columns.action) if columns.action is not None else None
    quantity = row.get(columns.quantity) if columns.quantity is not None else None
    action_text = _inline_text(action[1]) if action else ""
    quantity_text = _inline_text(quantity[1]) if quantity else ""
    kind = _action_class(action_text or quantity_text)
    tag = _ACTION_TAGS.get(kind or "") or _neutral_tag(action_text)
    for column, (cell, inline) in row.items():
        cell.attrs.pop("style", None)
        if column == columns.name:
            cell.attrJoin("class", "c-name")
            _rewrite_name_cell(inline)
        elif column == columns.action:
            cell.attrJoin("class", "c-act")
            if quantity and action_text == tag:
                cell.attrJoin("class", "dup")  # 整格就是“拿着”，标签已经说了
        elif column == columns.quantity:
            cell.attrJoin("class", "c-qty")
        elif column == columns.price:
            if _inline_text(inline) in _EMPTY_CELLS:
                cell.attrJoin("class", "dup")
            elif _rewrite_price_cell(inline):
                cell.attrJoin("class", "c-price")
            else:
                _labelled_line(cell, headers, column)
        else:
            _labelled_line(cell, headers, column)
    # 没有股数列时标签只能贴在操作文字前面，“不参与 不参与：0股”这种就不贴了
    holder = quantity or (action if kind else None)
    if holder and tag:
        holder[0].attrSet("data-tag", tag)
        holder[0].attrJoin("class", kind or "act-none")


def _decorate_lines_row(row: dict[int, tuple[Token, Token]], headers: list[str]) -> None:
    for column, (cell, _inline) in row.items():
        cell.attrs.pop("style", None)
        if column == 0:
            cell.attrJoin("class", "c-name")
        else:
            _labelled_line(cell, headers, column)


def _decorate_plain_row(row: dict[int, tuple[Token, Token]]) -> None:
    for column, (cell, inline) in row.items():
        _move_align_to_class(cell)
        if column > 0 and _NUMERIC_CELL.fullmatch(_inline_text(inline)):
            cell.attrJoin("class", "fig")


def _decorate_table(tokens: list[Token], start: int) -> int:
    """决定一张表怎么显示，返回表格结束的位置。

    计划表变成账本行（名称、动作、放大的计划价）；三列以上的长文字表变成标题加分行；
    其他表格保持表格，放不下时横向滑动。
    """
    close = next(index for index in range(start, len(tokens)) if tokens[index].type == "table_close")
    headers = [
        _inline_text(tokens[index + 1])
        for index in range(start, close)
        if tokens[index].type == "th_open"
    ]
    columns = _plan_columns(headers)
    has_long_cell = len(headers) >= 3 and any(
        tokens[index].type == "inline" and len(_inline_text(tokens[index])) > _LONG_CELL_CHARS
        for index in range(start, close)
    )
    if columns:
        tokens[start].attrSet("class", "plan")
    elif has_long_cell:
        tokens[start].attrSet("class", "lines")

    column = 0
    row: dict[int, tuple[Token, Token]] = {}
    for index in range(start, close):
        token = tokens[index]
        if token.type == "tr_open":
            column = 0
            row = {}
        elif token.type == "th_open":
            _move_align_to_class(token)
            column += 1
        elif token.type == "td_open":
            row[column] = (token, tokens[index + 1])
            column += 1
        elif token.type == "tr_close" and row:
            if columns:
                _decorate_plan_row(row, headers, columns)
            elif has_long_cell:
                _decorate_lines_row(row, headers)
            else:
                _decorate_plain_row(row)
    return close


def _split_lead(tokens: list[Token], position: int) -> None:
    """开头这段是结论：第一句放大，后面的话作为补充。"""
    opening, inline, closing = tokens[position], tokens[position + 1], tokens[position + 2]
    opening.attrJoin("class", "lead")
    children = inline.children or []
    line_break = next(
        (index for index, child in enumerate(children) if child.type in ("softbreak", "hardbreak")),
        None,
    )
    head = children if line_break is None else children[:line_break]
    head_text = "".join(child.content for child in head if child.type in ("text", "code_inline"))
    balanced = sum(c.nesting for c in head) == 0
    if len(head_text) <= _LEAD_BIG_MAX_CHARS and balanced:
        opening.attrJoin("class", "lead-big")
        if line_break is not None:
            rest_open = copy.copy(opening)
            rest_open.attrs = {"class": "lead-end"}
            rest_inline = copy.copy(inline)
            rest_inline.children = children[line_break + 1:]
            inline.children = head
            tokens[position + 3:position + 3] = [rest_open, rest_inline, copy.copy(closing)]
            return
    opening.attrJoin("class", "lead-end")


def _mark_lead(tokens: list[Token]) -> None:
    """正文开头的第一段是结论；开头不是段落（比如先来一张表）就不标。"""
    for position, token in enumerate(tokens):
        if token.level != 0:
            continue
        if token.type == "paragraph_open":
            _split_lead(tokens, position)
            return
        if token.type not in ("heading_open", "heading_close", "hr"):
            return


def _wrap_tables(cleaned: str) -> str:
    # 清洗后的表格标签没有其他属性，正文里的尖括号都已转义，可以放心按字面替换
    return (
        cleaned.replace('<table class="plan">', '<div class="ledger"><table class="plan">')
        .replace('<table class="lines">', '<div class="ledger"><table class="lines">')
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


# 研报版式：颜色取自网站的 token，靠细线和字号分层，不用卡片和阴影。
# 只有抬头、代码和放大的数字用衬线（.fig），中文和正文里的数字都用系统字体。
_PAGE_CSS = (
    f'@font-face{{font-family:"SR Figure";src:url({FIGURE_FONT_DATA_URI}) format("woff2");font-weight:500;font-display:swap}}'
    + """
:root{color-scheme:dark;--bg:#050505;--text:#f2f0e8;--muted:#9d9a8f;--line:#2a2925;--line-strong:#4c4430;
--gold:#d5b46a;--gold-2:#f1d48a;--gold-dim:#7d6736;--danger:#ff5d5d;--ok:#27c58a;--stop:#e6c36f;
--up:var(--danger);--down:var(--ok);--soft:#cfcabb;--body:#e5dfce;--figure:"SR Figure",Georgia,serif}
*{box-sizing:border-box}
html{background:var(--bg);-webkit-text-size-adjust:100%;scroll-behavior:smooth}
body{margin:0;min-height:100vh;border-top:2px solid var(--gold);background:var(--bg);color:var(--text);font-family:"PingFang SC","Microsoft YaHei UI","Microsoft YaHei","Noto Sans CJK SC",system-ui,sans-serif;font-size:14px;line-height:1.7;font-variant-numeric:tabular-nums}
.wrap{max-width:720px;margin:0 auto;padding:0 20px}
.fig{font-family:var(--figure);font-weight:500;font-variant-numeric:lining-nums tabular-nums}
.mast{display:flex;justify-content:space-between;align-items:center;gap:12px;height:40px;border-bottom:1px solid var(--line)}
.mark{color:var(--gold-2);font-size:12px;font-weight:500;letter-spacing:.18em;white-space:nowrap}
.mast-note{color:var(--muted);font-size:13px;white-space:nowrap}
.page{padding:0 0 24px}
.page-title{margin:24px 0 0;color:var(--text);font-size:30px;font-weight:500;line-height:1.25;letter-spacing:.01em}
.page-meta{margin:8px 0 0;color:var(--muted);font-size:13px;line-height:1.5}
.kpi{display:grid;grid-auto-flow:column;grid-auto-columns:minmax(0,1fr);margin-top:16px;border-bottom:1px solid var(--line)}
.kpi>div{padding:0 0 14px 12px;border-left:1px solid var(--line)}
.kpi>div:first-child{padding-left:0;border-left:0}
.kpi .fig{display:block;color:var(--text);font-size:32px;line-height:1.1}
.kpi .kl{display:block;margin-top:4px;color:var(--muted);font-size:12px;line-height:1.5;white-space:nowrap}
.toc{margin-top:16px;padding-bottom:12px;border-bottom:1px solid var(--line);font-size:13px}
.toc summary{color:var(--gold);letter-spacing:.14em;cursor:pointer}
.toc ol{margin:8px 0 0;padding-left:20px}
.toc li{margin:4px 0}
.toc a{color:var(--body);text-decoration:none}
.report{counter-reset:sec;color:var(--body);line-height:1.72;overflow-wrap:anywhere}
.report>:first-child{margin-top:20px}
.report h1,.report h2,.report h3,.report h4,.report h5,.report h6{margin:20px 0 8px;color:var(--gold-2);font-weight:500;line-height:1.35}
.report h1{font-size:18px}
.report h2{margin-top:28px;padding-bottom:8px;border-bottom:1px solid var(--line-strong);color:var(--text);font-size:16px;scroll-margin-top:12px}
.report h2::before{counter-increment:sec;content:counter(sec,decimal-leading-zero);margin-right:8px;color:var(--gold);font-family:var(--figure);font-weight:500;font-variant-numeric:lining-nums}
.report h3{font-size:15px}
.report h4,.report h5,.report h6{font-size:14px}
.report p{margin:8px 0}
.report strong{color:#f4e4b8;font-weight:700}
.report .lead{margin:20px 0 0;padding-top:16px;border-top:1px solid var(--line-strong)}
.report .lead::before{content:"结论";display:block;margin-bottom:8px;color:var(--gold);font-size:12px;font-weight:400;letter-spacing:.14em}
.report .lead-big{color:var(--text);font-size:18px;font-weight:500;line-height:1.6}
.report .lead-end{margin-bottom:0;padding-bottom:16px;border-bottom:1px solid var(--line-strong)}
.report .lead-end:not(.lead){margin-top:10px;color:var(--soft);line-height:1.75}
.report ul,.report ol{margin:8px 0;padding-left:22px}
.report li{margin:4px 0}
.report hr{margin:20px 0;border:0;border-top:1px solid var(--line)}
.report a{color:var(--gold-2)}
.report blockquote{margin:12px 0;padding:0 0 0 12px;border-left:1px solid var(--gold-dim);color:var(--soft)}
.report code{padding:1px 4px;background:#171510;color:var(--gold-2);font-size:12px}
.report pre{margin:12px 0;padding:8px 0;border-top:1px solid var(--line);border-bottom:1px solid var(--line);overflow-x:auto;overflow-wrap:normal;line-height:1.55}
.report pre code{padding:0;background:none;color:var(--body)}
.table-wrap{margin:12px 0;overflow-x:auto;-webkit-overflow-scrolling:touch;border-top:1px solid var(--line-strong);border-bottom:1px solid var(--line-strong)}
table{width:100%;border-collapse:separate;border-spacing:0;font-size:13px;line-height:1.55;overflow-wrap:normal}
th,td{min-width:4.5em;padding:8px;text-align:left;vertical-align:top;border-bottom:1px solid var(--line)}
tbody tr:last-child td{border-bottom:0}
th{color:var(--muted);font-size:12px;font-weight:400}
th:first-child,td:first-child{position:sticky;left:0;max-width:9em;padding-left:0;background:var(--bg)}
td.fig{color:var(--text);font-size:14px;white-space:nowrap}
.ar{text-align:right}
.ac{text-align:center}
.ledger{margin:0 0 8px}
.plan,.lines,.plan tbody,.lines tbody{display:block;width:100%}
.plan thead,.lines thead{display:none}
.plan tr{display:grid;grid-template-columns:minmax(0,1fr) auto;column-gap:8px;align-items:end;padding:14px 0;border-bottom:1px solid var(--line)}
.lines tr{display:block;padding:14px 0;border-bottom:1px solid var(--line)}
.plan td,.lines td{display:block;position:static;min-width:0;max-width:none;padding:0;border:0;background:none}
.plan td.dup{display:none}
.plan td.c-name,.lines td.c-name{display:flex;flex-wrap:wrap;grid-column:1/-1;grid-row:1;align-items:baseline;gap:0 6px;color:var(--text);font-size:15px;font-weight:500}
.cd{color:var(--muted);font-size:13px;font-weight:400}
.now{margin-left:auto;color:var(--muted);font-size:12px;font-weight:400;white-space:nowrap}
.now .fig{color:var(--soft);font-size:14px}
.plan td.c-qty{grid-column:1;grid-row:2;margin-top:8px;color:var(--text);font-size:14px;font-weight:500}
.plan td.c-price{display:contents}
.px{display:flex;grid-column:2;grid-row:2;align-items:baseline;gap:6px;margin-top:8px;white-space:nowrap}
.lb{color:var(--muted);font-size:12px}
.big{color:var(--gold-2);font-size:24px;line-height:1}
.px-note{grid-column:1/-1;grid-row:4;margin-top:4px;color:var(--muted);font-size:13px;line-height:1.55}
.plan td.c-act{grid-column:1/-1;grid-row:3;margin-top:8px;color:var(--muted);font-size:13px;line-height:1.55}
.plan td.c-line,.lines td.c-line{grid-column:1/-1;margin-top:4px;color:var(--muted);font-size:13px;line-height:1.55}
.c-line::before{content:attr(data-label) "：";color:var(--muted)}
.lines td.c-line{color:var(--body)}
td[data-tag]::before{content:attr(data-tag);display:inline-block;margin-right:8px;padding:0 6px;border:1px solid var(--tag-edge);border-radius:2px;color:var(--tag);font-size:12px;font-weight:400;line-height:20px}
.act-sell{--tag:var(--down);--tag-edge:rgba(39,197,138,.5)}
.act-buy{--tag:var(--up);--tag-edge:rgba(255,93,93,.5)}
.act-stop{--tag:var(--stop);--tag-edge:rgba(230,195,111,.5)}
.act-none{--tag:var(--muted);--tag-edge:var(--line-strong)}
.plan-link{display:flex;justify-content:center;align-items:center;gap:8px;min-height:44px;margin-top:24px;border:1px solid var(--gold-dim);border-radius:2px;color:var(--gold-2);font-size:13px;font-weight:500;text-decoration:none}
.page-foot{margin-top:24px;padding-top:12px;border-top:1px solid var(--line);color:var(--muted);font-size:12px;line-height:1.6}
.notice{padding:72px 0;text-align:center}
.notice h1{margin:0 0 8px;color:var(--text);font-size:16px;font-weight:500}
.notice p{margin:0;color:var(--muted);font-size:13px}
"""
)

_ARROW = (
    '<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.4" '
    'aria-hidden="true"><path d="M2 7h10M8 3l4 4-4 4"></path></svg>'
)
_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
_PLAN_STAT_LABELS = (("sell", "笔卖出"), ("buy", "笔买入"), ("stop", "条止损线"), ("hold", "只拿着"))


def page_shell(title: str, body: str, *, mast_note: str = "", extra_css: str = "") -> str:
    note = f'<span class="mast-note fig">{html.escape(mast_note)}</span>' if mast_note else ""
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow">'
        '<meta name="color-scheme" content="dark"><meta name="theme-color" content="#050505">'
        f"<title>{html.escape(title)}</title><style>{_PAGE_CSS}{extra_css}</style></head>"
        f'<body><div class="wrap"><div class="mast"><span class="mark fig">SIXRABBITS RESEARCH</span>{note}</div>'
        f"{body}</div></body></html>"
    )


def kpi_strip(cells: list[tuple[int, str]]) -> str:
    items = "".join(
        f'<div><span class="fig">{value}</span><span class="kl">{html.escape(label)}</span></div>'
        for value, label in cells
    )
    return f'<div class="kpi">{items}</div>'


def long_date(day: date) -> str:
    return f"{day.year} 年 {day.month} 月 {day.day} 日 {_WEEKDAYS[day.weekday()]}"


def _display_title(title: str) -> str:
    # 卡片标题带日期（“早盘挂单清单 | 2026-11-04”），页面把日期放到标题下面那行
    return re.sub(r"\s*[|｜]\s*\d{4}-\d{2}-\d{2}$", "", title).strip() or title


def _generated_at(meta: dict) -> datetime | None:
    value = str(meta.get("generated_at") or "").strip()
    for layout in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, layout)
        except ValueError:
            continue
    return None


def _plan_stats_strip(stats: object) -> str:
    if not isinstance(stats, dict):
        return ""
    cells = [(int(stats.get(key) or 0), label) for key, label in _PLAN_STAT_LABELS]
    return kpi_strip(cells) if any(value for value, _ in cells) else ""


def _after_lead(body: str, block: str) -> str:
    """结论要最先看到：统计条和目录都排在它后面。正文开头没有结论就放在最前面。"""
    if not block:
        return body
    matched = re.search(r'<p class="[^"]*lead-end[^"]*">.*?</p>', body, flags=re.S)
    return f"{body[:matched.end()]}{block}{body[matched.end():]}" if matched else block + body


def render_share_page(*, title: str, meta: dict, content_md: str, expires_on: str) -> str:
    """完整的分享页。expires_on 是展示用的到期日期（北京时间）。"""
    body, sections = render_markdown(content_md)
    generated = _generated_at(meta)
    toc = ""
    if len(sections) >= TOC_MIN_SECTIONS:
        items = "".join(
            f'<li><a href="#{anchor}">{html.escape(text)}</a></li>' for anchor, text in sections
        )
        toc = f'<details class="toc"><summary>目录</summary><ol>{items}</ol></details>'
    body = _after_lead(body, _plan_stats_strip(meta.get("plan_stats")) + toc)
    subtitle = "　".join(
        part
        for part in (
            long_date(generated) if generated else "",
            f"{generated:%H:%M}" if generated else "",
            str(meta.get("digital_human") or "").strip(),
        )
        if part
    )
    meta_line = f'<p class="page-meta">{html.escape(subtitle)}</p>' if subtitle else ""
    plan_url = str(meta.get("plan_url") or "")
    plan_link = (
        f'<a class="plan-link" href="{html.escape(plan_url)}">看今天的计划进度{_ARROW}</a>'
        if plan_url.startswith(("https://", "http://"))
        else ""
    )
    made = f"生成于 {generated:%m-%d %H:%M}，" if generated else ""
    return page_shell(
        title,
        f'<main class="page"><h1 class="page-title">{html.escape(_display_title(title))}</h1>{meta_line}'
        f'<article class="report">{body}</article>{plan_link}'
        f'<footer class="page-foot">{html.escape(made)}链接有效期至 {html.escape(expires_on)}。</footer></main>',
        mast_note=f"No. {generated:%m%d-%H%M}" if generated else "",
    )


def render_notice_page(heading: str, detail: str) -> str:
    """链接过期、作废或不存在时的提示页。"""
    return page_shell(
        heading,
        f'<main class="page notice"><h1>{html.escape(heading)}</h1><p>{html.escape(detail)}</p></main>',
    )
