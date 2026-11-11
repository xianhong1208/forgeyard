"""The user guide (docs/guide), rendered for the in-app help at /guide.

English pages are in docs/guide, Traditional Chinese ones in docs/guide/zh-TW, with the same file
names and the same headings in the same order: a heading's anchor comes from the English page, so
a link such as /guide/zh-TW/troubleshooting#agent-offline lands on the same section in either
language. Only the pages listed in PAGES are served (no path from the URL reaches the disk), raw
HTML in the Markdown is not rendered, and nothing is fetched from elsewhere: it works offline.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Optional

from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parent.parent / "docs" / "guide"
LANGUAGES = {"en": "English", "zh-TW": "繁體中文"}
# slug -> file; the order is the guide's table of contents.
PAGES = {"index": "README.md", "quick-start": "quick-start.md", "concepts": "concepts.md", "how-to": "how-to.md",
         "troubleshooting": "troubleshooting.md", "reference": "reference.md"}
_FILES = {v: k for k, v in PAGES.items()}


class NotFound(Exception):
    pass


def directory(lang: str) -> Path:
    return ROOT if lang == "en" else ROOT / lang


def _md() -> MarkdownIt:
    # No raw HTML and no images: a page shows only its own text and loads nothing from elsewhere.
    return MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False}).enable(
        ["table", "strikethrough"]).disable("image")


def slugify(text: str) -> str:
    """GitHub's anchor rule: lower case, punctuation dropped, spaces to hyphens."""
    text = unicodedata.normalize("NFKC", text).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return re.sub(r" ", "-", text)


def _headings(tokens) -> list[tuple[int, str]]:
    out = []
    for i, t in enumerate(tokens):
        if t.type == "heading_open":
            out.append((int(t.tag[1]), tokens[i + 1].content))
    return out


def _ids(headings: list[tuple[int, str]]) -> list[str]:
    seen: dict[str, int] = {}
    ids = []
    for _, text in headings:
        base = slugify(text) or "section"
        n = seen.get(base, 0)
        seen[base] = n + 1
        ids.append(base if n == 0 else f"{base}-{n}")
    return ids


def _href(href: str, lang: str) -> str:
    """Links between guide pages work on GitHub (file.md#anchor) and in the app (/guide/<lang>/…)."""
    if re.match(r"^[a-z][a-z0-9+.-]*:", href, re.I) or href.startswith(("#", "/")):
        return href
    path, _, anchor = href.partition("#")
    target_lang = lang
    if path.startswith("../") and lang != "en":
        path, target_lang = path[3:], "en"
    if path.startswith("zh-TW/"):
        path, target_lang = path[len("zh-TW/"):], "zh-TW"
    slug = _FILES.get(path)
    if slug is None:
        return href
    return f"/guide/{target_lang}/{slug}" + (f"#{anchor}" if anchor else "")


def read(lang: str, slug: str) -> str:
    if lang not in LANGUAGES or slug not in PAGES:
        raise NotFound(f"{lang}/{slug}")
    path = directory(lang) / PAGES[slug]
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise NotFound(f"{lang}/{slug}") from None


def structure(lang: str, slug: str) -> list[tuple[int, str]]:
    return _headings(_md().parse(read(lang, slug)))


@lru_cache(maxsize=64)
def _render(lang: str, slug: str, stamp: tuple) -> dict:
    md = _md()
    tokens = md.parse(read(lang, slug))
    heads = _headings(tokens)
    ids = _ids(heads)
    if lang != "en":  # the English page's anchors, when the structure matches
        try:
            en = structure("en", slug)
        except NotFound:
            en = []
        if [lvl for lvl, _ in en] == [lvl for lvl, _ in heads]:
            ids = _ids(en)
    toc, title, n = [], None, 0
    for i, t in enumerate(tokens):
        if t.type == "heading_open":
            level, text = heads[n]
            t.attrSet("id", ids[n])
            if level == 1 and title is None:
                title = text
            elif level in (2, 3):
                toc.append({"id": ids[n], "text": text, "level": level})
            n += 1
        if t.type == "inline" and t.children:
            for c in t.children:
                if c.type == "link_open":
                    href = c.attrGet("href") or ""
                    new = _href(href, lang)
                    c.attrSet("href", new)
                    if new.startswith(("http://", "https://")):
                        c.attrSet("target", "_blank")
                        c.attrSet("rel", "noopener noreferrer")
    html = md.renderer.render(tokens, md.options, {})
    return {"lang": lang, "slug": slug, "title": title or slug, "html": html, "toc": toc}


def page(lang: str, slug: str) -> dict:
    """One rendered page: its title, HTML, table of contents and the guide's page list."""
    path = directory(lang) / PAGES.get(slug, "")
    if lang not in LANGUAGES or slug not in PAGES or not path.is_file():
        raise NotFound(f"{lang}/{slug}")
    en = ROOT / PAGES[slug]
    stamp = (path.stat().st_mtime_ns, en.stat().st_mtime_ns if en.is_file() else 0)
    out = dict(_render(lang, slug, stamp))
    out["pages"] = pages(lang)
    out["languages"] = [{"code": c, "name": n, "available": (directory(c) / PAGES[slug]).is_file()}
                        for c, n in LANGUAGES.items()]
    return out


def pages(lang: str) -> list[dict]:
    out = []
    for slug, name in PAGES.items():
        path = directory(lang) / name
        if not path.is_file():
            continue
        title: Optional[str] = None
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break
        out.append({"slug": slug, "title": title or slug})
    return out
