"""Design-system v1 codemod for the quant module pages (one-shot, reviewed after)."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path("frontend/src/views")

HEX_MAP = {
    "#d7b76f": "var(--gold)",
    "#ead08f": "var(--gold-2)",
    "#eabf63": "var(--gold)",
    "#0d1117": "var(--bg-2)",
    "#161b22": "var(--surface)",
    "#21262d": "var(--surface-2)",
    "#30363d": "var(--line)",
    "#484f58": "var(--muted-2)",
    "#8b949e": "var(--muted)",
    "#e6edf3": "var(--text)",
    "#785f2d": "var(--gold-dim)",
    "#75602f": "var(--gold-dim)",
    "#75602e": "var(--gold-dim)",
    "#8a713b": "var(--gold-dim)",
    "#745e2d": "var(--gold-dim)",
    "#b38a34": "var(--warning)",
    # GitHub 次级灰/文本（BacktestHistory/Report、FactorAnalysis）
    "#6e7681": "var(--muted)",
    "#7d8590": "var(--muted)",
    "#858c90": "var(--muted)",
    "#8f979d": "var(--muted)",
    "#c9d1d9": "var(--muted)",
    "#aeb4b4": "var(--muted)",
    "#f2f5f8": "var(--text)",
    "#e5e2d9": "var(--text)",
    "#3a424d": "var(--line)",
    "#2a3034": "var(--line)",
    "#101315": "var(--bg-2)",
    "#171710": "var(--bg-2)",
    "#dfb65c": "var(--gold-dim)",
    # 语义色归位
    "#f25f64": "var(--danger)",
    "#ff6b70": "var(--danger)",
    "#20c997": "var(--ok)",
    "#128269": "var(--ok)",
    "#1ba87d": "var(--ok)",
    "#128100": "var(--ok)",
    "#f0883e": "var(--warning)",
    "#3bb2d6": "var(--cyan)",
}

MONO_DECL_RE = re.compile(r"[ \t]*font-family:\s*(?:ui-monospace|\"Cascadia Mono\")[^;}\n]*;?[ \t]*")

def fix_mono(css: str) -> str:
    """Drop font-family mono declarations except genuine code blocks (pre/code)."""
    out: list[str] = []
    last = 0
    for match in MONO_DECL_RE.finditer(css):
        open_brace = css.rfind("{", 0, match.start())
        prev_close = css.rfind("}", 0, match.start())
        selector = css[prev_close + 1: open_brace] if open_brace > prev_close else ""
        if re.search(r"\b(?:pre|code)\b", selector):
            continue
        out.append(css[last:match.start()])
        rule_end = css.find("}", match.end())
        rule_tail = css[match.end(): rule_end if rule_end != -1 else len(css)]
        rule_head = css[open_brace:match.start()] if open_brace > prev_close else ""
        line_head = css[css.rfind("\n", 0, match.start()):match.start()]
        if (
            "font-variant-numeric" not in rule_tail
            and "font-variant-numeric" not in rule_head
            and "font-variant-numeric" not in line_head
        ):
            out.append("font-variant-numeric: tabular-nums; ")
        last = match.end()
    out.append(css[last:])
    return "".join(out)

def font_target(px: float) -> int:
    if px <= 12: return 12
    if px <= 13.5: return 13
    if px <= 14.5: return 14
    if px <= 16: return 16
    if px <= 20: return 20
    if px <= 26: return 24
    return 32

FONT_RE = re.compile(r"(font(?:-size)?:\s*)(\d+(?:\.\d+)?)px")

def fix_fonts(css: str) -> str:
    def sub(m: re.Match) -> str:
        target = font_target(float(m.group(2)))
        return f"{m.group(1)}{target}px"
    return FONT_RE.sub(sub, css)

RADIUS_RE = re.compile(r"(border-radius:\s*)(\d+(?:\.\d+)?)px")

def fix_radii(css: str) -> str:
    def sub(m: re.Match) -> str:
        value = float(m.group(2))
        if value in (4.0, 8.0) or value == 0:
            return m.group(0)
        target = 4 if value <= 7 else 8
        return f"{m.group(1)}{target}px"
    return RADIUS_RE.sub(sub, css)

def replace_hex(text: str) -> str:
    for old, new in HEX_MAP.items():
        text = re.sub(re.escape(old) + r"\b", new, text, flags=re.IGNORECASE)
    return text

def split_vue(text: str) -> tuple[str, str, str]:
    tpl_start = text.index("<template>")
    style_start = text.index("<style")
    style_open_end = text.index(">", style_start) + 1
    style_end = text.index("</style>") + len("</style>")
    head = text[:tpl_start]
    template = text[tpl_start:style_start]
    middle = text[style_start:style_open_end]
    style = text[style_open_end:style_end]
    tail = text[style_end:]
    return head, template, middle, style, tail

def remove_css_block(css: str, selector_prefix: str) -> str:
    """Remove a rule block whose selector starts with selector_prefix (brace-aware)."""
    out = []
    i = 0
    while True:
        idx = css.find(selector_prefix, i)
        if idx == -1:
            out.append(css[i:])
            break
        # must be at line start (selector position)
        line_start = css.rfind("\n", 0, idx) + 1
        if css[line_start:idx].strip():
            out.append(css[i:idx + len(selector_prefix)])
            i = idx + len(selector_prefix)
            continue
        brace = css.find("{", idx)
        if brace == -1:
            out.append(css[i:])
            break
        depth = 0
        j = brace
        while j < len(css):
            if css[j] == "{":
                depth += 1
            elif css[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        end = j + 1
        # swallow following blank line
        if css[end:end + 2] == "\n\n":
            end += 1
        out.append(css[i:line_start])
        i = end
    return "".join(out)


def migrate_screener(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    head, template, middle, style, tail = split_vue(text)

    tpl_replacements = [
        ('class="sc-btn sc-btn-primary sc-generate"', 'class="ui-btn ui-btn--primary sc-generate"'),
        ('class="sc-btn sc-btn-ghost sc-profile-refresh"', 'class="ui-btn ui-btn--ghost sc-profile-refresh"'),
        ('class="sc-btn sc-btn-primary"', 'class="ui-btn ui-btn--primary"'),
        ('class="sc-btn sc-btn-ghost"', 'class="ui-btn ui-btn--ghost"'),
        ('class="sc-btn sc-btn-soft"', 'class="ui-btn ui-btn--ghost"'),
        ('class="sc-text-btn sc-danger-text"', 'class="ui-btn--text sc-danger-text"'),
        ('class="sc-text-btn"', 'class="ui-btn--text"'),
    ]
    for old, new in tpl_replacements:
        template = template.replace(old, new)

    for selector in (
        ".sc-btn {",
        ".sc-btn:disabled {",
        ".sc-btn-primary {",
        ".sc-btn-primary:hover:not(:disabled) {",
        ".sc-btn-ghost {",
        ".sc-btn-ghost:hover:not(:disabled) {",
        ".sc-btn-soft {",
        ".sc-text-btn {",
        ".sc-text-btn:disabled {",
    ):
        style = remove_css_block(style, selector)
    style = style.replace(".sc-text-btn", ".ui-btn--text")
    style = style.replace(".sc-btn", ".ui-btn")

    text = head + template + middle + style + tail
    text = replace_hex(text)
    head, template, middle, style, tail = split_vue(text)
    style = fix_radii(fix_fonts(style))
    path.write_text(head + template + middle + style + tail, encoding="utf-8")


def migrate_automatic_selection(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    head, template, middle, style, tail = split_vue(text)

    template = template.replace('class="auto-button auto-button--primary"', 'class="ui-btn ui-btn--primary"')
    template = template.replace('class="auto-button auto-button--ghost"', 'class="ui-btn ui-btn--ghost"')
    template = template.replace("'auto-button--danger' : 'auto-button--primary'", "'ui-btn--danger' : 'ui-btn--primary'")
    template = template.replace('class="auto-button"', 'class="ui-btn"')

    for selector in (
        ".auto-button {",
        ".auto-button:hover:not(:disabled) {",
        ".auto-button:disabled {",
        ".auto-button--primary {",
        ".auto-button--primary:hover:not(:disabled) {",
        ".auto-button--ghost {",
        ".auto-button--danger {",
    ):
        style = remove_css_block(style, selector)
    style = style.replace(".auto-button", ".ui-btn")

    text = head + template + middle + style + tail
    text = replace_hex(text)
    head, template, middle, style, tail = split_vue(text)
    style = fix_radii(fix_fonts(style))
    path.write_text(head + template + middle + style + tail, encoding="utf-8")


def migrate_industry(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    head, template, middle, style, tail = split_vue(text)

    template = template.replace('class="ia-primary"', 'class="ui-btn ui-btn--primary"')
    style = remove_css_block(style, ".ia-page .ia-primary {")

    text = head + template + middle + style + tail
    text = replace_hex(text)
    head, template, middle, style, tail = split_vue(text)
    style = fix_radii(fix_fonts(style))
    path.write_text(head + template + middle + style + tail, encoding="utf-8")


def migrate_simple(path: Path, *, mono: bool = True) -> None:
    """Color/font/radius-only migration (backtest history & report, factor analysis, task board)."""
    text = path.read_text(encoding="utf-8")
    text = replace_hex(text)
    head, template, middle, style, tail = split_vue(text)
    if mono:
        style = fix_mono(style)
    style = fix_radii(fix_fonts(style))
    path.write_text(head + template + middle + style + tail, encoding="utf-8")


if __name__ == "__main__":
    migrate_screener(ROOT / "ScreenerView.vue")
    migrate_automatic_selection(ROOT / "AutomaticSelectionView.vue")
    migrate_industry(ROOT / "IndustryAnalysisView.vue")
    migrate_simple(ROOT / "BacktestHistoryView.vue")
    migrate_simple(ROOT / "BacktestReportView.vue")
    migrate_simple(ROOT / "FactorAnalysisView.vue")
    migrate_simple(ROOT / "AgentTaskBoardView.vue")
    print("codemod done")
