# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2025-2026 TG11
"""Tiny, dependency-free Markdown subset -> safe HTML (escaped first).
Supports headings (#), numbered / bulleted lists, **bold**, `code`, paragraphs.
Used for AI-written instructions and assistant messages."""
from __future__ import annotations

import html
import re

_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
_OL = re.compile(r"^\s*(\d+)[.)]\s+(.*)$")
_UL = re.compile(r"^\s*[-*•]\s+(.*)$")
_H = re.compile(r"^\s*(#{1,4})\s+(.*)$")


def _inline(text: str) -> str:
    text = html.escape(text, quote=False)
    text = _INLINE_CODE.sub(r"<code>\1</code>", text)
    text = _BOLD.sub(r"<b>\1</b>", text)
    text = _ITALIC.sub(r"<i>\1</i>", text)
    return text


def md_lite(text: str) -> str:
    out, mode, para = [], None, []

    def flush_para():
        if para:
            out.append("<p>" + "<br>".join(para) + "</p>")
            para.clear()

    def close_list():
        nonlocal mode
        if mode:
            out.append(f"</{mode}>")
            mode = None

    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            flush_para()
            close_list()
            continue
        m = _H.match(line)
        if m:
            flush_para(); close_list()
            lvl = min(len(m.group(1)) + 2, 5)
            out.append(f"<h{lvl}>{_inline(m.group(2))}</h{lvl}>")
            continue
        m = _OL.match(line)
        if m:
            flush_para()
            if mode != "ol":
                close_list(); out.append("<ol>"); mode = "ol"
            out.append(f"<li>{_inline(m.group(2))}</li>")
            continue
        m = _UL.match(line)
        if m:
            flush_para()
            if mode != "ul":
                close_list(); out.append("<ul>"); mode = "ul"
            out.append(f"<li>{_inline(m.group(1))}</li>")
            continue
        if mode and line.startswith((" ", "\t")):
            out[-1] = out[-1][:-5] + "<br>" + _inline(line.strip()) + "</li>"
            continue
        close_list()
        para.append(_inline(line))
    flush_para(); close_list()
    return "\n".join(out)
