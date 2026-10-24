"""Plain-text layout for the CopperPilot chat chrome.

Widgets render these strings. Nothing here invents a command, mode, or key
the CLI does not already implement. Follow-up queueing is not one of those.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable

from textual.content import Content

from copper_pilot_cli.copper_logo import LOGO_WIDTH, logo_lines
from copper_pilot_cli.copper_theme import (
    ColorMode,
    ascii_mode,
    detect_color_mode,
    glyph,
    soften,
    style_for,
)

Segment = tuple[str, str | None]

# Real `/` commands. `/model` is added only while the model selector is enabled.
SLASH_COMMANDS: tuple[tuple[str, str], ...] = (
    ("/approval", "Switch approval mode"),
    ("/auth", "Log in or out"),
    ("/build", "Build a saved plan"),
    ("/clear", "Clear the transcript"),
    ("/context", "Show workspace and thread"),
    ("/copy", "Copy the last response"),
    ("/cwd", "Switch project folder"),
    ("/effort", "Switch balanced or intelligent"),
    ("/exit", "Quit CopperPilot"),
    ("/feedback", "Send product feedback"),
    ("/help", "List commands"),
    ("/hooks", "Show hook status"),
    ("/mode", "Switch agent, plan, or ask"),
    ("/quit", "Quit CopperPilot"),
    ("/resume", "Resume a thread"),
    ("/theme", "Switch the colour theme"),
    ("/threads", "List threads"),
    ("/tokens", "Show token usage"),
    ("/tools", "List local tools"),
    ("/update", "Check for an update"),
    ("/version", "Show the version"),
)

SLASH_LIMIT = 6

# Keys that exist. Shift+Enter and Ctrl+L do not. There is no follow-up queue.
KEY_HELP: tuple[tuple[str, str], ...] = (
    ("enter", "send"),
    ("/", "commands"),
    ("@", "attach file"),
    ("shift+tab", "switch mode: plan, ask, agent"),
    ("ctrl+e", "switch effort: balanced, intelligent"),
    ("up/down", "history"),
    ("esc", "stop the turn"),
    ("ctrl+c", "cancel or quit"),
    ("ctrl+q", "quit"),
    ("ctrl+r", "resume thread"),
    ("ctrl+v", "paste"),
)

QUICK_START: tuple[tuple[str, str], ...] = (
    ("/help", "see every command"),
    ("@file", "mention a schematic, layout or datasheet"),
    ("shift+tab", "switch mode: plan, ask, agent"),
    ("ctrl+e", "switch effort: balanced, intelligent"),
)

# One sentence each, under 90 characters. A second message is not queued, esc
# only stops the turn, and ctrl+e toggles effort rather than opening a review mode.
TIPS: tuple[str, ...] = (
    "Mention a datasheet with @ and ask about the footprint.",
    "Press shift+tab to switch between plan, ask and agent.",
    "Start a big change in plan mode: you get the steps before anything is touched.",
    "Press ctrl+e to switch between balanced and intelligent.",
    "Press esc while it is working to stop the turn.",
    "Type / to see every command, then keep typing to filter.",
    "Mention several files with @ in the same message.",
)

MODE_ORDER: tuple[str, ...] = ("plan", "ask", "agent")
MODE_COPY: dict[str, str] = {
    "plan": "drafts a plan, changes nothing",
    "ask": "checks with you before each change",
    "agent": "keeps working through the task on its own",
}
EFFORT_ORDER: tuple[str, ...] = ("balanced", "intelligence")
EFFORT_LABEL: dict[str, str] = {"balanced": "balanced", "intelligence": "intelligent"}
EFFORT_COPY: dict[str, str] = {
    "balanced": "weighs speed and depth — the everyday setting",
    "intelligence": "reasons step by step — for hard problems",
}

# Decision values stay approve / auto / reject. There is no per-file allowlist.
APPROVAL_OPTIONS: tuple[str, ...] = ("yes", "auto-accept edits", "no")


def approval_heading(tool_name: str, path: str | None) -> str:
    """Question shown above an approval. Uses the real tool, not a sample edit."""
    if tool_name == "execute":
        return "Run command?"
    if tool_name in {"edit_file", "write_file", "write_binary_file", "delete"} and path:
        return f"Apply edit to {path}?"
    return f"Allow {tool_name}?"


def slash_footer(*, ascii: bool | None = None) -> str:
    use_ascii = ascii_mode() if ascii is None else ascii
    choose = "up/down" if use_ascii else "↑↓"
    return f"{choose} choose · enter run · esc close"


def approval_hint(*, ascii: bool | None = None) -> str:
    use_ascii = ascii_mode() if ascii is None else ascii
    choose = "left/right" if use_ascii else "←→"
    return f"{choose} choose · enter confirm · esc no"


SLASH_FOOTER = slash_footer(ascii=False)
APPROVAL_HINT = approval_hint(ascii=False)


def middle_truncate(text: str, limit: int, *, ascii: bool = False) -> str:
    """Truncate the middle. Never cuts a line into two."""
    mark = "..." if ascii else "…"
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= len(mark):
        return mark[:limit]
    left = (limit - len(mark)) // 2
    right = limit - len(mark) - left
    return text[:left] + mark + text[-right:]


def end_truncate(text: str, limit: int, *, ascii: bool = False) -> str:
    """Drop the end of one token with an ellipsis."""
    mark = "..." if ascii else "…"
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= len(mark):
        return mark[:limit]
    return text[: limit - len(mark)] + mark


def _clip_words(text: str, limit: int, *, ascii: bool) -> str:
    """Shorten on a word boundary. Never wraps onto a second line."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    mark = "..." if ascii else "…"
    if limit <= len(mark):
        return mark[:limit]
    room = limit - len(mark)
    cut = text[:room]
    if room < len(text) and text[room] != " ":
        space = cut.rfind(" ")
        if space > 0:
            cut = cut[:space]
    return cut.rstrip() + mark


def pick_tip_index(
    last: int | None,
    *,
    count: int | None = None,
    choose: Callable[[list[int]], int] | None = None,
) -> int:
    """Pick a tip that is not ``last`` when another tip exists."""
    total = len(TIPS) if count is None else count
    if total <= 0:
        raise ValueError("no tips")
    if isinstance(last, int) and not isinstance(last, bool) and 0 <= last < total and total > 1:
        options = [index for index in range(total) if index != last]
    else:
        options = list(range(total))
    if choose is None:
        return options[secrets.randbelow(len(options))]
    return choose(options)


def tip_content(
    width: int,
    text: str,
    *,
    mode: ColorMode | None = None,
    ascii: bool | None = None,
) -> Content:
    """``tip`` plus the sentence. Empty under 60 columns."""
    color_mode = detect_color_mode() if mode is None else mode
    use_ascii = ascii_mode() if ascii is None else ascii
    if width < 60 or not text:
        return Content("")
    prefix = "tip  "
    body = _clip_words(text, width - len(prefix), ascii=use_ascii)
    if not body:
        return Content("")
    return Content.assemble(
        _piece(prefix[:3], "stone-500", color_mode),
        prefix[3:],
        _piece(body, "muted", color_mode),
    )


def rule_content(
    width: int,
    *,
    busy: bool = False,
    mode: ColorMode | None = None,
    ascii: bool | None = None,
) -> Content:
    """Full-width rule above and below the input. ``-`` in ASCII mode."""
    color_mode = detect_color_mode() if mode is None else mode
    use_ascii = ascii_mode() if ascii is None else ascii
    text = glyph("h", ascii=use_ascii) * max(0, width)
    token = "divider" if busy else "rule"
    return Content.assemble(_piece(text, token, color_mode))


def slash_catalog(*, include_model: bool = False) -> list[tuple[str, str]]:
    rows = list(SLASH_COMMANDS)
    if include_model:
        rows.append(("/model", "Choose an Intelligence model"))
    return sorted(rows)


def _fuzzy(query: str, name: str) -> bool:
    """Subsequence match. Case does not matter, so mixed-case skill ids still hit."""
    query = query.lower()
    name = name.lower()
    index = 0
    for character in name:
        if index < len(query) and character == query[index]:
            index += 1
    return index == len(query) and bool(query)


def rank_slash(query: str, commands: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Prefix matches first, then subsequence matches. At most six rows."""
    needle = query.lower()
    if needle and not needle.startswith("/"):
        needle = "/" + needle
    prefix = [item for item in commands if item[0].lower().startswith(needle)]
    prefix_names = {item[0] for item in prefix}
    fuzzy = [
        item
        for item in commands
        if item[0] not in prefix_names
        and _fuzzy(needle.removeprefix("/"), item[0].removeprefix("/"))
    ]
    return (prefix + fuzzy)[:SLASH_LIMIT]


def next_mode(current: str) -> str:
    """plan → ask → agent → plan."""
    try:
        index = MODE_ORDER.index(current)
    except ValueError:
        index = -1
    return MODE_ORDER[(index + 1) % len(MODE_ORDER)]


def next_effort(current: str) -> str:
    """balanced ↔ intelligence. The stored value stays ``intelligence``."""
    try:
        index = EFFORT_ORDER.index(current)
    except ValueError:
        index = -1
    return EFFORT_ORDER[(index + 1) % len(EFFORT_ORDER)]


def _piece(
    text: str,
    token: str | None,
    mode: ColorMode,
    *,
    bold: bool = False,
) -> str | tuple[str, str]:
    if mode is ColorMode.NONE or not text:
        return text
    style = style_for(token, mode) if token else ""
    if bold:
        style = f"bold {style}".strip()
    if style:
        return (text, style)
    return text


def _show_logo(width: int, mode: ColorMode, *, ascii: bool) -> bool:
    return width >= 60 and mode is not ColorMode.NONE and not ascii


def welcome_lines(
    width: int,
    *,
    directory: str,
    branch: str | None,
    show_quick_start: bool = True,
    mode: ColorMode = ColorMode.TRUECOLOR,
    ascii: bool = False,
) -> list[str]:
    """Flat welcome. The logo is omitted when narrow, uncoloured, or ASCII."""
    repo = directory
    if branch:
        repo = f"{directory} · {branch}"
    text = ["CopperPilot", "hardware design agent", "", repo]
    if _show_logo(width, mode, ascii=ascii):
        logo = logo_lines()
        pad = (len(logo) - len(text)) // 2
        rows: list[str] = []
        for index, logo_row in enumerate(logo):
            text_index = index - pad
            line = text[text_index] if 0 <= text_index < len(text) else ""
            rows.append(f"{logo_row}   {line}")
    else:
        rows = list(text)
    if not show_quick_start:
        return [end_truncate(row, width, ascii=ascii) for row in rows]
    rows.extend(["", ""])
    rows.append("quick start")
    descriptions = width >= 60
    for key, description in QUICK_START:
        if descriptions:
            rows.append(f"{key:<12}{description}")
        else:
            rows.append(key)
    return [end_truncate(row, width, ascii=ascii) for row in rows]


def welcome_content(
    width: int,
    *,
    directory: str,
    branch: str | None,
    show_quick_start: bool = True,
    mode: ColorMode | None = None,
    ascii: bool | None = None,
) -> Content:
    color_mode = detect_color_mode() if mode is None else mode
    use_ascii = ascii_mode() if ascii is None else ascii
    lines = welcome_lines(
        width,
        directory=directory,
        branch=branch,
        show_quick_start=show_quick_start,
        mode=color_mode,
        ascii=use_ascii,
    )
    if color_mode is ColorMode.NONE:
        return Content("\n".join(lines))
    logo = logo_lines() if _show_logo(width, color_mode, ascii=use_ascii) else []
    parts: list[str | tuple[str, str]] = []
    logo_row = 0
    quick_keys = {key for key, _description in QUICK_START}
    for index, line in enumerate(lines):
        if index:
            parts.append("\n")
        if not line:
            continue
        if logo and logo_row < len(logo) and line.startswith(logo[logo_row]):
            parts.append(_piece(logo[logo_row], "orange-600", color_mode))
            rest = line[LOGO_WIDTH:]
            gap = rest[:3]
            body = rest[len(gap) :]
            if gap:
                parts.append(gap)
            parts.extend(_header_text(body, color_mode))
            logo_row += 1
            continue
        if line == "quick start":
            parts.append(_piece(line, "muted", color_mode))
            continue
        key = next((item for item in quick_keys if line.startswith(item)), "")
        if key:
            shown = f"{key:<12}" if len(line) > len(key) else key
            parts.append(_piece(shown, "orange-500", color_mode))
            if len(line) > 12:
                parts.append(_piece(line[12:], "text", color_mode))
            continue
        parts.extend(_header_text(line, color_mode))
    return Content.assemble(*parts) if parts else Content("")


def _header_text(text: str, mode: ColorMode) -> list[str | tuple[str, str]]:
    if text.startswith("CopperPilot"):
        title = "CopperPilot"
        return [
            _piece(title, "heading", mode, bold=True),
            _piece(text[len(title) :], "muted", mode),
        ]
    if text.startswith("hardware design agent") or text == "quick start":
        return [_piece(text, "muted", mode)]
    if text:
        return [_piece(text, "muted", mode)]
    return []


def status_content(
    width: int,
    *,
    chat_mode: str,
    effort: str,
    flash: str | None = None,
    mode: ColorMode | None = None,
    ascii: bool | None = None,
) -> Content:
    """One status line. Under 40 columns, only the two icons remain."""
    color_mode = detect_color_mode() if mode is None else mode
    use_ascii = ascii_mode() if ascii is None else ascii
    mode_name = chat_mode if chat_mode in MODE_COPY else "agent"
    effort_name = EFFORT_LABEL.get(effort, "balanced")
    mode_icon = glyph(mode_name, ascii=use_ascii)
    effort_icon = glyph(effort_name, ascii=use_ascii)
    if width < 40:
        parts: list[str | tuple[str, str]] = [
            _piece(mode_icon, "orange-500", color_mode),
            " ",
            _piece(effort_icon, "orange-500", color_mode),
        ]
        return Content.assemble(*parts)
    mode_hint = MODE_COPY[mode_name] if flash == "mode" else "shift+tab"
    effort_hint = (
        EFFORT_COPY.get(effort, EFFORT_COPY["balanced"]) if flash == "effort" else "ctrl+e"
    )
    mode_hint = soften(mode_hint, ascii=use_ascii)
    effort_hint = soften(effort_hint, ascii=use_ascii)
    segments: list[Segment] = [
        (mode_icon, "orange-500"),
        (" ", None),
        (mode_name, "text"),
        ("  ", None),
        (mode_hint, "muted"),
        ("    ", None),
        (effort_icon, "orange-500"),
        (" ", None),
        (effort_name, "text"),
        ("  ", None),
        (effort_hint, "muted"),
    ]
    left = "".join(text for text, _token in segments)
    right = "/help"
    if len(left) + 2 + len(right) <= width:
        gap = width - len(left) - len(right)
        segments.append((" " * gap, None))
        segments.append((right, "orange-500"))
    else:
        plain = end_truncate(left, width, ascii=use_ascii)
        return Content.assemble(_piece(plain, "text", color_mode))
    parts = [_piece(text, token, color_mode) for text, token in segments]
    return Content.assemble(*parts) if parts else Content("")


def slash_row_content(
    name: str,
    description: str,
    width: int,
    *,
    selected: bool,
    mode: ColorMode | None = None,
    ascii: bool = False,
) -> Content:
    color_mode = detect_color_mode() if mode is None else mode
    command = name.ljust(16) if len(name) <= 16 else end_truncate(name, 16, ascii=ascii)
    remaining = max(0, width - 16)
    detail = end_truncate(description, remaining, ascii=ascii) if remaining else ""
    command_token = "orange-500" if selected else "text"
    detail_token = "text" if selected else "muted"
    return Content.assemble(
        _piece(command, command_token, color_mode),
        _piece(detail, detail_token, color_mode),
    )


def working_content(
    frame: int,
    status: str,
    elapsed: int,
    *,
    width: int | None = None,
    mode: ColorMode | None = None,
    ascii: bool | None = None,
) -> Content:
    """Three dots. The lit dot walks left to right. There is no progress count.

    ``width`` keeps ``esc to stop`` on the line by shortening the status.
    """
    color_mode = detect_color_mode() if mode is None else mode
    use_ascii = ascii_mode() if ascii is None else ascii
    dot = glyph("dot", ascii=use_ascii)
    suffix = f"{elapsed}s · esc to stop"
    lead = f"{dot} {dot} {dot}  "
    status_text = status
    if width is not None and width > 0:
        if len(lead) + len(suffix) > width:
            body = suffix if len(suffix) <= width else end_truncate(suffix, width, ascii=use_ascii)
            return Content.assemble(_piece(body, "muted", color_mode))
        room = width - len(lead) - len(suffix) - 2
        status_text = end_truncate(status, room, ascii=use_ascii) if room > 0 else ""
    parts: list[str | tuple[str, str]] = []
    for index in range(3):
        if index:
            parts.append(" ")
        token = "orange-500" if index == frame % 3 else "orange-900"
        parts.append(_piece(dot, token, color_mode))
    spacer = f"  {status_text}  " if status_text else "  "
    parts.append(_piece(spacer, "text", color_mode))
    parts.append(_piece(suffix, "muted", color_mode))
    return Content.assemble(*parts)


def help_entries(*, include_model: bool = False) -> list[tuple[str, str]]:
    return [*KEY_HELP, *slash_catalog(include_model=include_model)]


def help_content(
    width: int,
    *,
    include_model: bool = False,
    mode: ColorMode | None = None,
    ascii: bool | None = None,
) -> Content:
    """Commands and keys, two columns when the line is wide enough."""
    color_mode = detect_color_mode() if mode is None else mode
    use_ascii = ascii_mode() if ascii is None else ascii
    entries = help_entries(include_model=include_model)
    columns = 2 if width >= 72 else 1
    gap = 2 if columns > 1 else 0
    column_width = max(16, (width - gap * (columns - 1)) // columns)
    parts: list[str | tuple[str, str]] = []
    for index, (key, description) in enumerate(entries):
        key_cell = end_truncate(key, 16, ascii=use_ascii).ljust(16)
        room = max(0, column_width - 16)
        detail = end_truncate(description, room, ascii=use_ascii).ljust(room) if room else ""
        parts.append(_piece(key_cell, "orange-500", color_mode))
        if detail:
            parts.append(_piece(detail, "text", color_mode))
        last_in_row = (index + 1) % columns == 0 or index == len(entries) - 1
        if last_in_row:
            parts.append("\n")
        elif gap:
            parts.append(" " * gap)
    if parts and parts[-1] == "\n":
        parts.pop()
    return Content.assemble(*parts) if parts else Content("")


def content_plain(content: Content) -> str:
    return content.plain
