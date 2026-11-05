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
import itertools
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
    "p": {"lead", "lead-big", "lead-end", "fine"},
    "ul": {"stocks"},
    "table": {"plan", "lines"},
    "th": {"ar", "ac"},
    "td": {
        "ar", "ac", "fig", "dup",
        "c-name", "c-act", "c-solo", "c-price", "c-qty", "c-line",
        "act-buy", "act-sell", "act-stop", "act-none",
    },
    "span": {
        "nm", "cd", "now", "px", "lb", "big", "px-note", "fig", "key", "star",
        "chg", "up", "down", "flat", "tag", "row-tag", "act-note", "wide", "sl-head", "sl-note",
        "act-buy", "act-sell", "act-stop", "act-none",
    },
}
_ALLOWED_URL_SCHEMES = {"http", "https", "mailto", "tel"}

_ACTION_HEADER = re.compile(r"怎么做|怎么执行|怎么处理|接下来|有没有碰到|碰到|操作|动作|方向|买卖")
_NAME_HEADER = re.compile(r"股票|标的|名称|证券")
_CURRENT_PRICE_HEADER = re.compile(r"^\s*(?:现价|最新价|收盘价|昨收)")  # “加权成本→收盘价”不算
_NOT_PLAN_PRICE_HEADER = re.compile(r"现价|最新价|收盘价|昨收|成本价")
_CHANGE_HEADER = re.compile(r"涨跌|涨幅|跌幅")
_GENERIC_PRICE_HEADERS = frozenset({"价格", "价位"})
_PRICE_HEADER = re.compile(r"价|昨收")
_QUANTITY_HEADER = re.compile(r"股数|数量")
_NO_ACTION = re.compile(r"不|别|暂缓|持有|拿着|观望|等待|买卖")
_SELL_ACTION = re.compile(r"卖|减仓|减半|减持|清仓|清掉|退出")
_BUY_ACTION = re.compile(r"买|加仓|建仓")
_ACTION_TAGS = {"act-sell": "卖出", "act-buy": "买入", "act-stop": "止损"}
_CLAUSE_SPLIT = re.compile(r"[：:，,。；;／/]")  # “反弹卖16.95／止损15.98”是卖单，止损只是备用出口

# 股票格的各种写法：“同力天启605286（21.57元）”“民德电子（300656）”“示例科技 22.35”
# “★澜起科技（688008，方正）｜153.29｜+2.19%”“#1 兴业银行 601166.SH；19.93元”
_STOCK_CELL = re.compile(
    r"^(?P<star>★)?\s*(?:(?P<rank>#\d+)\s*)?(?P<name>[^\d（(｜|；;,，#★][^\d（(｜|；;]*)"
    r"(?P<code>\d{6})?(?:\.[A-Z]{2})?\s*(?:[（(](?P<inner>[^）)]*)[）)])?\s*(?P<rest>.*)$"
)
_STOCK_SPLIT = re.compile(r"[，,、；;｜|\s]+")
_SIX_DIGIT_CODE = re.compile(r"(\d{6})(?:\.[A-Z]{2})?")
_QUOTED_PRICE = re.compile(r"(?:最新价|现价|收盘价)?\s*(\d+(?:\.\d+)?)\s*元?")
_CHANGE_VALUE = re.compile(r"[-+−–]?\d+(?:\.\d+)?[%％]")
_PRICE_AND_CHANGE_SPLIT = re.compile(r"[\s/／｜|，,]+")
# 理由里写明的计划价：“反弹卖价19.92”“已达到35.40减仓条件”。止损价不算，它留在文字里高亮
_PROSE_PLAN_PRICE = re.compile(
    r"(?:卖价|卖出价|买价|买入价|限价|挂单价|减仓价|卖|买)\s*(\d+\.\d+)|(\d+\.\d+)\s*元?\s*(?:减仓|退出|卖出|买入|限价|反弹卖|卖价|买价)"
)
_KEY_NUMBER = re.compile(r"\d+\.\d{2,}")
_DATA_NOTE = re.compile(r"行情更新|价格单位|数量单位|数据截至|数据日期|更新于|均为|可用现金")
_DISCLAIMER = re.compile(r"不构成投资建议|仅供研究")
_SENTENCE_END = re.compile(r"[。！？!?]")
_BOLD_PRICE = re.compile(r"(\d+(?:\.\d+)?)\s*元?")
_LIMIT_PRICE = re.compile(r"限价\s*(\d+\.\d+)")
_DECIMAL_PRICE = re.compile(r"(?<![\d.:])(\d+\.\d+)")
_WHOLE_PRICE = re.compile(r"\s*(?:限价)?\s*(\d{1,5})\s*元?\s*")
_PRICE_WORD = re.compile(r"(最高限价|限价|挂单价|卖价|买价|触发价|止损价|止盈价|涨到|跌到|跌破)\s*$")
# 数字人的工作留痕，读者用不上：存档路径、版本和校验值。旧任务写进了正文，页面上去掉
_PROCESS_BLOCK = re.compile(r"<details>.*?</details>", flags=re.S)
_PROCESS_LINE = re.compile(r"^(?:已(?:保存|存)[至到在：:]?.*notes/.*|SHA-256[：:].*)$", flags=re.M)
_RESIDUE_PUNCTUATION = re.compile(r"[\s，,。；;：:元]")
_NUMERIC_CELL = re.compile(r"[-+]?[\d,]+(?:\.\d+)?\s*[%％元股]?")
_EMPTY_CELLS = frozenset({"", "—", "-", "--", "/"})
_LEAD_BIG_MAX_CHARS = 80  # 第一句太长就不放大，整段按普通正文
# 说明性的话够短才算小字；再长多半夹着操作要求，要当正文读。整段加粗的通常就是一句说明，放宽一点
_FINE_PRINT_MAX_CHARS = 80
_FINE_PRINT_PLAIN_MAX_CHARS = 50


class _PlanColumns(NamedTuple):
    name: int
    action: int | None
    price: int | None
    quantity: int | None
    current: int | None
    change: int | None


class _StockHead(NamedTuple):
    name: str
    star: bool
    rank: str
    code: str
    labels: tuple[str, ...]
    current: str
    change: str
    current_label: str = "现价"


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
    """逐小句找第一个表态：“拿着，跌破 20 止损”“不参与：不再买”以开头为准，不上买卖色；
    “较13:43明显拉升，仍高于151.00退出限价”开头只是描述，往后找到的才算。"""
    for clause in _CLAUSE_SPLIT.split(cell_text.strip()):
        if _NO_ACTION.search(clause):
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
    if "持有" in cell_text or "拿着" in cell_text:
        return "拿着"
    clause = _first_clause(cell_text)
    return clause if 0 < len(clause) <= 4 and not re.search(r"\d", clause) else ""


def _html_token(content: str) -> Token:
    # 只有这里生成的标记能以原样进入输出：用户内容都先转义，最后还要过 nh3 白名单
    return Token(type="html_inline", tag="", nesting=0, content=content)


def _parse_stock(text: str) -> _StockHead | None:
    """拆开股票格。除了名称、代码、账户、现价、涨跌幅还有别的话，就不是单纯的股票格，返回 None。"""
    matched = _STOCK_CELL.match(text.strip())
    if not matched:
        return None
    code = matched.group("code") or ""
    current = change = ""
    labels: list[str] = []
    for part in _STOCK_SPLIT.split(matched.group("inner") or ""):
        if not part:
            continue
        six_digit = _SIX_DIGIT_CODE.fullmatch(part)
        quoted = _QUOTED_PRICE.fullmatch(part)
        if six_digit:
            code = six_digit.group(1)  # 括号里的 6 位数是代码，不是现价
        elif quoted:
            current = quoted.group(1)
        elif _CHANGE_VALUE.fullmatch(part):
            change = part
        else:
            labels.append(part)
    for part in _STOCK_SPLIT.split(matched.group("rest") or ""):
        if not part:
            continue
        quoted = _QUOTED_PRICE.fullmatch(part)
        if quoted:
            current = quoted.group(1)
        elif _CHANGE_VALUE.fullmatch(part):
            change = part
        else:
            return None
    if not (code or current or change):
        return None
    return _StockHead(
        name=matched.group("name").strip(),
        star=bool(matched.group("star")),
        rank=matched.group("rank") or "",
        code=code,
        labels=tuple(labels),
        current=current,
        change=change,
    )


def _change_class(change: str) -> str:
    value = change.replace("−", "-").replace("–", "-").rstrip("%％")
    if value.startswith("-") and float(value) != 0:
        return "down"
    return "up" if float(value) != 0 else "flat"


def _stock_head_html(head: _StockHead) -> str:
    parts = ['<span class="star">★</span>'] if head.star else []
    if head.rank:
        parts.append(f'<span class="cd">{escapeHtml(head.rank)}</span>')
    parts.append(f'<span class="nm">{escapeHtml(head.name)}</span>')
    if head.code:
        parts.append(f'<span class="cd fig">{head.code}</span>')
    parts.extend(f'<span class="cd">{escapeHtml(label)}</span>' for label in head.labels)
    if head.current or head.change:
        current = f'{head.current_label} <span class="fig">{head.current}</span>' if head.current else ""
        change = (
            f' <span class="chg {_change_class(head.change)} fig">{escapeHtml(head.change)}</span>'
            if head.change
            else ""
        )
        parts.append(f'<span class="now">{current}{change}</span>')
    return "".join(parts)


def _price_and_change(text: str) -> tuple[str, str, bool]:
    """从“21.11 / -2.13%”这种格子里取现价和涨跌幅；最后一项表示有没有别的话留下。"""
    current = change = ""
    leftover = False
    for part in _PRICE_AND_CHANGE_SPLIT.split(text.strip()):
        if not part:
            continue
        quoted = _QUOTED_PRICE.fullmatch(part)
        if quoted and not current:
            current = quoted.group(1)
        elif _CHANGE_VALUE.fullmatch(part) and not change:
            change = part
        else:
            leftover = True
    return current, change, leftover


def _rewrite_name_cell(inline: Token, current: str = "", change: str = "", current_label: str = "现价") -> bool:
    """名称格写成一行：名称、代码、现价、涨跌幅；现价和涨跌幅可能来自单独的列。"""
    text = _inline_text(inline)
    head = _parse_stock(text)
    if head is None and (current or change):
        head = _StockHead(text, False, "", "", (), "", "")
    if head is None:
        return False
    if current:
        head = head._replace(current=current, current_label=current_label)
    if change:
        head = head._replace(change=change)
    inline.children = [_html_token(_stock_head_html(head))]
    return True


def _highlight_numbers(children: list[Token]) -> list[Token]:
    """理由里的价格换成衬线金色，一眼能看到；只换纯文字，其余原样保留。"""
    result: list[Token] = []
    for child in children:
        if child.type != "text" or not _KEY_NUMBER.search(child.content):
            result.append(child)
            continue
        pieces: list[str] = []
        cursor = 0
        for matched in _KEY_NUMBER.finditer(child.content):
            pieces.append(escapeHtml(child.content[cursor:matched.start()]))
            pieces.append(f'<span class="fig key">{matched.group(0)}</span>')
            cursor = matched.end()
        pieces.append(escapeHtml(child.content[cursor:]))
        result.append(_html_token("".join(pieces)))
    return result


def _prose_price_html(text: str) -> str:
    matched = _PROSE_PLAN_PRICE.search(text)
    if not matched:
        return ""
    price = matched.group(1) or matched.group(2)
    return f'<span class="px"><span class="lb">计划价</span><span class="fig big">{price}</span></span>'


def _note_opening(price_html: str, tag_html: str, note_class: str) -> str:
    """有计划价时：标签和大价格一行，理由单独一整行；没有时标签和理由排在一行。"""
    if price_html:
        tag_slot = f'<span class="row-tag">{tag_html}</span>' if tag_html else ""
        return f'{tag_slot}{price_html}<span class="{note_class}">'
    return f'<span class="{note_class} wide">{tag_html}'


def _note_children(inline_children: list[Token], text: str, kind: str | None, tag: str) -> list[Token]:
    """没有股数列的行：标签加理由一行，右边放大理由里写明的计划价。"""
    price_html = _prose_price_html(text)
    tag_html = ""
    if tag and (kind or not text.startswith(tag)):
        tag_html = f'<span class="tag {kind or "act-none"}">{escapeHtml(tag)}</span>'
    return [
        _html_token(_note_opening(price_html, tag_html, "act-note")),
        *_highlight_numbers(inline_children),
        _html_token("</span>"),
    ]


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


def _rewrite_price_cell(inline: Token, header: str = "") -> bool:
    """把价格放大显示；单元格里还有别的话（如“9:15集合竞价”）就整句留在下面，一个字都不丢。"""
    price = _main_price(inline)
    if price is None:
        return False
    text = _inline_text(inline)
    position = text.find(price)
    before, after = text[:position], text[position + len(price):]
    word = _PRICE_WORD.search(before)
    # 格子里只有数字时，用“反弹卖价”“计划价”这种短表头当标签
    fallback = header if len(header) <= 4 and header not in _GENERIC_PRICE_HEADERS else ""
    label = word.group(1) if word else fallback
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
    current = next((i for i, header in enumerate(headers) if i > 0 and _CURRENT_PRICE_HEADER.search(header)), None)
    change = next((i for i, header in enumerate(headers) if _CHANGE_HEADER.search(header)), None)
    quantity = next((i for i, header in enumerate(headers) if _QUANTITY_HEADER.search(header)), None)
    named = next((i for i, header in enumerate(headers) if _NAME_HEADER.search(header)), None)
    if not (
        (action is not None and (has_price or quantity is not None))
        or (has_price and quantity is not None)
        or (named is not None and current is not None)  # 一行一只的看盘表：股票、现价、涨跌幅
    ):
        return None
    name = named if named is not None else 0
    # 首列“股票（最新价）”和“现价”列都是现在的价格，不能放大成金色的计划价
    price = next(
        (
            i
            for i, header in enumerate(headers)
            if i != name and _PRICE_HEADER.search(header) and not _NOT_PLAN_PRICE_HEADER.search(header)
        ),
        None,
    )
    return _PlanColumns(name, action, price, quantity, current, change)


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
    def text_of(column: int | None) -> str:
        return _inline_text(row[column][1]) if column is not None and column in row else ""

    action = row.get(columns.action) if columns.action is not None else None
    quantity = row.get(columns.quantity) if columns.quantity is not None else None
    action_text = text_of(columns.action)
    kind = _action_class(action_text or text_of(columns.quantity))
    tag = _ACTION_TAGS.get(kind or "") or _neutral_tag(action_text)
    # 现价和涨跌幅并进名称那一行；有一格解析不干净就保留原样，不丢字
    numeric = {
        column: _price_and_change(text_of(column))
        for column in {columns.current, columns.change} - {None}
        if column in row
    }
    current_value = next((parts[0] for parts in numeric.values() if parts[0] and not parts[2]), "")
    change_value = next((parts[1] for parts in numeric.values() if parts[1] and not parts[2]), "")
    merged = False
    if columns.name in row:
        cell, inline = row[columns.name]
        cell.attrs.pop("style", None)
        cell.attrJoin("class", "c-name")
        current_header = headers[columns.current] if columns.current is not None else ""
        # 表头写的是昨收，就不能显示成现价
        current_label = "昨收" if "昨收" in current_header else ("收盘" if "收盘" in current_header else "现价")
        merged = _rewrite_name_cell(inline, current_value, change_value, current_label)
    for column, (cell, inline) in row.items():
        cell.attrs.pop("style", None)
        if column == columns.name:
            continue
        if column in (columns.current, columns.change):
            parts = numeric.get(column)
            if merged and parts and not parts[2] and (parts[0] or parts[1]):
                cell.attrJoin("class", "dup")  # 已经并进名称那一行
            elif merged and not text_of(column):
                cell.attrJoin("class", "dup")
            else:
                _labelled_line(cell, headers, column)
        elif column == columns.action:
            if quantity is None:
                cell.attrJoin("class", "c-act")
                cell.attrJoin("class", "c-solo")
                inline.children = _note_children(inline.children or [], action_text, kind, tag)
            else:
                cell.attrJoin("class", "c-act")
                if action_text == tag:
                    cell.attrJoin("class", "dup")  # 整格就是“拿着”，标签已经说了
                inline.children = _highlight_numbers(inline.children or [])
        elif column == columns.quantity:
            cell.attrJoin("class", "c-qty")
        elif column == columns.price:
            if _inline_text(inline) in _EMPTY_CELLS:
                cell.attrJoin("class", "dup")
            elif _rewrite_price_cell(inline, headers[column] if column < len(headers) else ""):
                cell.attrJoin("class", "c-price")
            else:
                _labelled_line(cell, headers, column)
        else:
            _labelled_line(cell, headers, column)
    if quantity and tag:
        quantity[0].attrSet("data-tag", tag)
        quantity[0].attrJoin("class", kind or "act-none")


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


def _without_empty_text(children: list[Token]) -> list[Token]:
    # markdown-it 会在粗体前后留下空的文本节点，判断结构前要去掉
    return [child for child in children if child.type != "text" or child.content]


def _is_whole_bold(children: list[Token]) -> bool:
    return (
        len(children) >= 3
        and children[0].type == "strong_open"
        and children[-1].type == "strong_close"
        and sum(1 for child in children if child.type == "strong_open") == 1
    )


def _split_at_first_sentence(children: list[Token]) -> tuple[list[Token], list[Token]] | None:
    """在最外层文字的第一个句号处切开；后面没话了就不切。"""
    depth = 0
    for index, child in enumerate(children):
        depth += child.nesting
        if child.type != "text" or depth != 0:
            continue
        matched = _SENTENCE_END.search(child.content)
        if not matched:
            continue
        before, after = copy.copy(child), copy.copy(child)
        before.content = child.content[: matched.end()]
        after.content = child.content[matched.end():].lstrip()
        rest = ([after] if after.content else []) + children[index + 1:]
        return (children[:index] + [before], rest) if rest else None
    return None


def _split_lead(tokens: list[Token], position: int) -> None:
    """开头这段是结论：第一句放大，后面的话作为补充。整段都加粗的先去掉粗体，放大已经够醒目。"""
    opening, inline, closing = tokens[position], tokens[position + 1], tokens[position + 2]
    opening.attrJoin("class", "lead")
    children = _without_empty_text(inline.children or [])
    if _is_whole_bold(children):
        children = children[1:-1]
        inline.children = children
    line_break = next(
        (index for index, child in enumerate(children) if child.type in ("softbreak", "hardbreak")),
        None,
    )
    if line_break is not None:
        head, rest = children[:line_break], children[line_break + 1:]
    else:
        head, rest = _split_at_first_sentence(children) or (children, [])
    head_text = "".join(child.content for child in head if child.type in ("text", "code_inline"))
    balanced = sum(c.nesting for c in head) == 0
    if len(head_text) <= _LEAD_BIG_MAX_CHARS and balanced:
        opening.attrJoin("class", "lead-big")
        if rest:
            rest_open = copy.copy(opening)
            rest_open.attrs = {"class": "lead-end"}
            rest_inline = copy.copy(inline)
            rest_inline.children = rest
            inline.children = head
            tokens[position + 3:position + 3] = [rest_open, rest_inline, copy.copy(closing)]
            return
    opening.attrJoin("class", "lead-end")


def _mark_fine_print(tokens: list[Token]) -> None:
    """行情时间、价格单位、免责这类说明不是观点，改成小字，粗体也去掉。"""
    for position, token in enumerate(tokens[:-1]):
        if token.type != "paragraph_open" or token.level != 0 or token.attrGet("class"):
            continue
        inline = tokens[position + 1]
        if not _is_fine_print(inline):
            continue
        token.attrSet("class", "fine")
        children = _without_empty_text(inline.children or [])
        if _is_whole_bold(children):
            inline.children = children[1:-1]


def _is_fine_print(inline: Token) -> bool:
    text = _inline_text(inline)
    children = _without_empty_text(inline.children or [])
    limit = _FINE_PRINT_MAX_CHARS if _is_whole_bold(children) else _FINE_PRINT_PLAIN_MAX_CHARS
    return len(text) <= limit and bool(_DATA_NOTE.search(text) or _DISCLAIMER.search(text))


def _split_stock_item(children: list[Token]) -> tuple[_StockHead, list[Token]] | None:
    """拆开一条股票列表项，返回 (股票行, 后面的理由)。两种写法：

    - **★上海谊众（688091）｜35.64｜+2.44%** 换行写理由
    - ★同力天启｜21.78｜+0.97%｜已到21.68反弹卖价；止损20.59。
    """
    star_outside = bool(children) and children[0].type == "text" and children[0].content.strip() == "★"
    if star_outside:
        children = children[1:]
    if not children:
        return None
    if children[0].type == "strong_open":
        end = next((i for i, child in enumerate(children) if child.type == "strong_close"), None)
        if end is None:
            return None
        head = _parse_stock("".join(child.content for child in children[1:end] if child.type == "text"))
        rest = children[end + 1:]
    elif children[0].type == "text" and re.search(r"[｜|]", children[0].content):
        parts = re.split(r"\s*[｜|]\s*", children[0].content.strip())
        count = 1
        while count < len(parts) and (_QUOTED_PRICE.fullmatch(parts[count]) or _CHANGE_VALUE.fullmatch(parts[count])):
            count += 1
        if count == 1:
            return None  # 第一段后面不是价格或涨跌幅，只是一句带竖线的话
        head = _parse_stock("｜".join(parts[:count]))
        remainder = copy.copy(children[0])
        remainder.content = "｜".join(parts[count:])
        rest = ([remainder] if remainder.content else []) + children[1:]
    else:
        return None
    if head is None:
        return None
    while rest and (rest[0].type in ("softbreak", "hardbreak") or (rest[0].type == "text" and not rest[0].content.strip("｜| ："))):
        rest = rest[1:]
    if rest and rest[0].type == "text":
        first = copy.copy(rest[0])
        first.content = first.content.lstrip("｜| ：:")
        rest = [first] + rest[1:]
    return head._replace(star=head.star or star_outside), rest


def _decorate_stock_list(tokens: list[Token], start: int) -> None:
    """一行一只的股票列表也是账本行；有一条不是这种写法就整个不动。"""
    level = tokens[start].level
    close = next(
        index
        for index in range(start + 1, len(tokens))
        if tokens[index].type == "bullet_list_close" and tokens[index].level == level
    )
    items: list[tuple[Token, _StockHead, list[Token]]] = []
    for index in range(start + 1, close):
        if tokens[index].type != "list_item_open" or tokens[index].level != level + 1:
            continue
        inline = tokens[index + 2]
        if inline.type != "inline":
            return
        parsed = _split_stock_item(_without_empty_text(inline.children or []))
        if parsed is None:
            return
        items.append((inline, parsed[0], parsed[1]))
    if not items:
        return
    tokens[start].attrSet("class", "stocks")
    for inline, head, rest in items:
        text = "".join(child.content for child in rest if child.type == "text")
        kind = _action_class(text)
        tag = _ACTION_TAGS.get(kind or "") or _neutral_tag(text)
        price_html = _prose_price_html(text)
        tag_html = ""
        if tag and (kind or not text.startswith(tag)):
            tag_html = f'<span class="tag {kind or "act-none"}">{escapeHtml(tag)}</span>'
        inline.children = [
            _html_token(
                f'<span class="sl-head">{_stock_head_html(head)}</span>'
                + _note_opening(price_html, tag_html, "sl-note")
            ),
            *_highlight_numbers(rest),
            _html_token("</span>"),
        ]


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


# ── 飞书卡片上的那一句 ────────────────────────────────────────────────
_BRIEF_MAX_CHARS = 80  # 超过就按句子截
_BRIEF_MIN_CHARS = 30  # 第一行不成句（如“13:43｜暂无新变化”）又不够这么长，就接上下一行
_BRIEF_SENTENCE_MIN_CHARS = 12  # 已经是完整的一句，到这么长就够了，不再往下接
_BRIEF_CLAUSE = re.compile(r"[，,；;、]")
_BRIEF_JOINED = tuple("。！？!?；;：:，,、")
_MEANINGFUL = re.compile(r"\w")  # 只有“━━━”这类装饰线的行不算

_Segments = list[tuple[str, bool]]  # [(文字, 是否粗体)]


def _brief_paragraph(tokens: list[Token]) -> Token | None:
    """和 _mark_lead 认同一段；开头不是段落时，取第一段不是小字说明的。"""
    for position, token in enumerate(tokens):
        if token.level != 0:
            continue
        if token.type == "paragraph_open":
            return tokens[position + 1]
        if token.type not in ("heading_open", "heading_close", "hr"):
            break
    for position, token in enumerate(tokens):
        if token.level == 0 and token.type == "paragraph_open" and not _is_fine_print(tokens[position + 1]):
            return tokens[position + 1]
    return None


def _segments_text(segments: _Segments) -> str:
    return "".join(text for text, _ in segments)


def _slice_segments(segments: _Segments, start: int, end: int) -> _Segments:
    """按字符位置截取，粗体标记跟着字走。"""
    sliced: _Segments = []
    offset = 0
    for text, bold in segments:
        piece = text[max(start - offset, 0):max(end - offset, 0)]
        if piece:
            sliced.append((piece, bold))
        offset += len(text)
    return sliced


def _brief_lines(children: list[Token]) -> list[_Segments]:
    lines: list[_Segments] = [[]]
    bold = 0
    for child in children:
        if child.type == "strong_open":
            bold += 1
        elif child.type == "strong_close":
            bold -= 1
        elif child.type in ("softbreak", "hardbreak"):
            lines.append([])
        elif child.type in ("text", "code_inline"):
            lines[-1].append((child.content, bold > 0))
    kept: list[_Segments] = []
    for line in lines:
        text = _segments_text(line)
        start, end = len(text) - len(text.lstrip()), len(text.rstrip())
        if _MEANINGFUL.search(text):
            kept.append(_slice_segments(line, start, end))
    return kept


def _cut_brief(segments: _Segments, limit: int) -> _Segments:
    text = _segments_text(segments)
    if len(text) <= limit:
        return segments
    ends = [m.end() for m in _SENTENCE_END.finditer(text) if m.end() <= limit]
    if ends and ends[-1] >= _BRIEF_MIN_CHARS:
        return _slice_segments(segments, 0, ends[-1])
    clauses = [m.start() for m in _BRIEF_CLAUSE.finditer(text) if _BRIEF_MIN_CHARS <= m.start() < limit]
    return _slice_segments(segments, 0, clauses[-1] if clauses else limit - 1) + [("…", False)]


def _segments_markdown(segments: _Segments) -> str:
    parts: list[str] = []
    for bold, group in itertools.groupby(segments, key=lambda segment: segment[1]):
        text = "".join(piece for piece, _ in group)
        core = text.strip()
        if bold and core:
            head, tail = text[: len(text) - len(text.lstrip())], text[len(text.rstrip()):]
            parts.append(f"{head}**{core}**{tail}")
        else:
            parts.append(text)
    return "".join(parts).strip()


def lead_brief(source: str, limit: int = _BRIEF_MAX_CHARS) -> str:
    """飞书卡片只放的那一句：正文开头那段的第一行，也就是手机版顶部放大的那句。

    第一行不成句就接上下一行，太长按句子截（实在没有句号才按逗号截并加省略号）。
    返回飞书 markdown，保留行内粗体；找不到像样的一段时返回空串。
    """
    source = _PROCESS_LINE.sub("", _PROCESS_BLOCK.sub("", source or ""))
    inline = _brief_paragraph(_MARKDOWN.parse(source.strip(), {}))
    if inline is None:
        return ""
    children = _without_empty_text(inline.children or [])
    if _is_whole_bold(children):
        children = children[1:-1]
    picked: _Segments = []
    for line in _brief_lines(children):
        so_far = _segments_text(picked)
        if picked:
            complete = len(so_far) >= _BRIEF_SENTENCE_MIN_CHARS and _SENTENCE_END.fullmatch(so_far[-1])
            if complete or len(so_far) >= _BRIEF_MIN_CHARS:
                break
            if not so_far.endswith(_BRIEF_JOINED):
                picked.append(("。", False))
        picked.extend(line)
    if all(bold for _, bold in picked):
        # 整句都粗等于没有重点，和手机版一样去掉，各条卡片的观感才统一
        picked = [(text, False) for text, _ in picked]
    return _segments_markdown(_cut_brief(picked, limit))


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
    source = _PROCESS_LINE.sub("", _PROCESS_BLOCK.sub("", source or ""))
    tokens = _MARKDOWN.parse(source.strip(), env)
    sections: list[tuple[str, str]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.type == "table_open":
            index = _decorate_table(tokens, index)
        elif token.type == "bullet_list_open":
            _decorate_stock_list(tokens, index)
        elif token.type == "heading_open" and token.tag == "h2":
            anchor = f"s{len(sections) + 1}"
            token.attrSet("id", anchor)
            sections.append((anchor, _inline_text(tokens[index + 1])))
        index += 1
    _mark_lead(tokens)
    _mark_fine_print(tokens)
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
.page-title{margin:24px 0 0;color:var(--text);font-size:30px;font-weight:400;line-height:1.25;letter-spacing:.01em}
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
.report strong{color:#f4e4b8;font-weight:600}
.report .fine{color:var(--muted);font-size:12px;line-height:1.6}
.report .fine strong{color:inherit;font-weight:400}
.report .lead{margin:20px 0 0;padding-top:16px;border-top:1px solid var(--line-strong)}
.report .lead::before{content:"结论";display:block;margin-bottom:8px;color:var(--gold);font-size:12px;font-weight:400;letter-spacing:.14em}
.report .lead-big{color:var(--text);font-size:18px;font-weight:400;line-height:1.6}
.report .lead-big strong{color:var(--gold-2);font-weight:500}
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
.chg.up{color:var(--up)}
.chg.down{color:var(--down)}
.chg.flat{color:var(--muted)}
.star{margin-right:-2px;color:var(--gold);font-size:13px}
.key{color:var(--gold-2);font-size:1.05em}
.tag{display:inline-block;margin-right:8px;padding:0 6px;border:1px solid var(--tag-edge);border-radius:2px;color:var(--tag);font-size:12px;line-height:20px}
.report .stocks{margin:0 0 8px;padding:0;list-style:none}
.report .stocks li{display:grid;grid-template-columns:minmax(0,1fr) auto;column-gap:8px;margin:0;padding:14px 0;border-bottom:1px solid var(--line)}
.report .stocks li p{display:contents}
.sl-head{display:flex;flex-wrap:wrap;grid-column:1/-1;grid-row:1;align-items:baseline;gap:0 6px;color:var(--text);font-size:15px;font-weight:500}
.row-tag{grid-column:1;grid-row:2;align-self:center;margin-top:8px}
.row-tag .tag{margin-right:0}
.sl-note,.act-note{grid-column:1/-1;grid-row:3;margin-top:8px;color:var(--soft);font-size:13px;line-height:1.55}
.sl-note.wide,.act-note.wide{grid-row:2}
.stocks .px,.c-solo .px{align-self:center;margin-top:8px}
.plan td.c-solo{display:contents}
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
