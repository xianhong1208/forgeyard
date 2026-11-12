"""The user guide (docs/guide) and the in-app help that serves it (FY-GUIDE-*)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from forgeyard import guide
from forgeyard.agent import config as agent_config
from forgeyard.config import Settings

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web" / "src"
FENCE = re.compile(r"^( *)```.*?^\1```", re.M | re.S)


def test_fy_guide_001_every_page_in_both_languages_without_signing_in(client):
    """FY-GUIDE-001: every page renders in English and Traditional Chinese, with a title, a table
    of contents and the page list in the guide's order, for someone not signed in."""
    r = client.get("/api/guide")
    assert r.status_code == 200
    body = r.json()
    assert [lang["code"] for lang in body["languages"]] == ["en", "zh-TW"]
    for lang in ("en", "zh-TW"):
        assert [p["slug"] for p in body["pages"][lang]] == list(guide.PAGES)
        for slug in guide.PAGES:
            r = client.get(f"/api/guide/{lang}/{slug}")
            assert r.status_code == 200, (lang, slug)
            page = r.json()
            assert page["title"] and page["html"].startswith("<h1") and page["lang"] == lang
            assert [p["slug"] for p in page["pages"]] == list(guide.PAGES)
            assert all(lang_["available"] for lang_ in page["languages"])
            if slug != "index":
                assert page["toc"], (lang, slug)
    assert client.get("/api/guide/en/quick-start").json()["title"] == "Quick start"
    assert client.get("/api/guide/zh-TW/quick-start").json()["title"] == "快速入門"


def test_fy_guide_002_the_two_languages_have_the_same_structure_and_commands():
    """FY-GUIDE-002: each Chinese page has the English page's headings (same levels, same order),
    so sections share their anchors, and the same code blocks byte for byte: every command is the
    same in both languages."""
    for slug, name in guide.PAGES.items():
        en = (guide.ROOT / name).read_text()
        zh = (guide.ROOT / "zh-TW" / name).read_text()
        assert [lvl for lvl, _ in guide.structure("en", slug)] == [lvl for lvl, _ in guide.structure("zh-TW", slug)], slug
        assert FENCE.findall(en) is not None
        assert [m.group(0) for m in FENCE.finditer(en)] == [m.group(0) for m in FENCE.finditer(zh)], slug
        en_ids = [t["id"] for t in guide.page("en", slug)["toc"]]
        assert en_ids == [t["id"] for t in guide.page("zh-TW", slug)["toc"]], slug


def _anchors(lang: str, slug: str) -> set[str]:
    return set(re.findall(r'<h[1-6] id="([^"]+)"', guide.page(lang, slug)["html"]))


def test_fy_guide_003_links_stay_in_the_guide_and_land_on_a_section():
    """FY-GUIDE-003: links between pages become /guide/<lang>/<page>#anchor and every such anchor
    exists in both languages; nothing links outside the guide except web addresses, which open in a
    new tab without an opener."""
    for lang in guide.LANGUAGES:
        for slug in guide.PAGES:
            html = guide.page(lang, slug)["html"]
            for href in re.findall(r'<a href="([^"]+)"', html):
                if href.startswith(("https://", "http://")):
                    assert f'<a href="{href}" target="_blank" rel="noopener noreferrer">' in html
                    continue
                if href.startswith("#"):
                    assert href[1:] in _anchors(lang, slug), (lang, slug, href)
                    continue
                m = re.fullmatch(r"/guide/(en|zh-TW)/([a-z-]+)(?:#([^\"]+))?", href)
                assert m, (lang, slug, href)
                assert m.group(2) in guide.PAGES, href
                if m.group(3):
                    assert m.group(3) in _anchors(m.group(1), m.group(2)), (lang, slug, href)
    assert '<a href="/guide/en/index">English</a>' in guide.page("zh-TW", "index")["html"]
    assert '<a href="/guide/zh-TW/index">' in guide.page("en", "index")["html"]


def test_fy_guide_004_every_help_link_in_the_ui_lands_on_a_section():
    """FY-GUIDE-004: each "How do I…?" link on the pages names a guide page and a section that exist
    (in both languages, which share anchors)."""
    links = []
    for f in WEB.rglob("*.tsx"):
        links += re.findall(r'<HelpLink page="([a-z-]+)"(?: anchor="([^"]+)")?', f.read_text())
    assert len(links) >= 15
    for page, anchor in links:
        assert page in guide.PAGES, page
        if anchor:
            for lang in guide.LANGUAGES:
                assert anchor in _anchors(lang, page), (lang, page, anchor)


@pytest.mark.parametrize("path", ["/api/guide/en/..%2F..%2Fpyproject", "/api/guide/en/../../pyproject.toml",
                                  "/api/guide/..%2Fdocs/index", "/api/guide/fr/index", "/api/guide/en/README",
                                  "/api/guide/en/README.md", "/api/guide/zh-TW/..%2F..", "/api/guide/en/https-and-trust",
                                  "/api/guide/en/%2Fetc%2Fpasswd"])
def test_fy_guide_005_only_the_guides_own_pages(client, path):
    """FY-GUIDE-005: a page is looked up by name in a fixed list; nothing in the URL reaches the
    disk: other languages, file names, ../ in any spelling are all 404 (or not the guide at all)."""
    r = client.get(path)
    assert r.status_code == 404 or not r.headers.get("content-type", "").startswith("application/json")
    assert "[project]" not in r.text and "root:" not in r.text
    for slug in ("../pyproject", "../https-and-trust", "../design", "zh-TW/README"):  # files that exist
        with pytest.raises(guide.NotFound):
            guide.read("en", slug)


def test_fy_guide_006_markdown_cannot_bring_html_or_scripts(tmp_path, monkeypatch):
    """FY-GUIDE-006: raw HTML in a page is shown as text, never rendered; javascript: links are not
    links; the page loads nothing from elsewhere (no img, script, iframe or stylesheet): it works
    offline."""
    (tmp_path / "zh-TW").mkdir()
    for name in guide.PAGES.values():
        (tmp_path / name).write_text("# T\n\nok\n")
    (tmp_path / "concepts.md").write_text('# T\n\n<script>alert(1)</script>\n\n<img src="https://x/y.png">\n\n'
                                          "[bad](javascript:alert(1)) ![pic](https://x/p.png)\n")
    monkeypatch.setattr(guide, "ROOT", tmp_path)
    html = guide.page("en", "concepts")["html"]
    assert "<script" not in html and "&lt;script&gt;" in html and "<img" not in html.replace("&lt;img", "")
    assert 'href="javascript:' not in html
    with pytest.raises(guide.NotFound):  # this language's file is missing
        guide.page("zh-TW", "concepts")
    for lang in ("en", "zh-TW"):
        for slug in guide.PAGES:
            try:
                real = guide.page(lang, slug)["html"]
            except guide.NotFound:
                continue
            assert not re.search(r"<(img|script|iframe|link|style)\b", real)


def test_fy_guide_007_the_reference_lists_every_setting():
    """FY-GUIDE-007: the reference names every agent setting (file key, variable and flag) and every
    server setting, in both languages, so neither can drift from the code."""
    for lang in ("en", "zh-TW"):
        text = (guide.directory(lang) / "reference.md").read_text()
        for s in agent_config.SETTINGS:
            assert f"`{s.name}`" in text and f"`{s.env}`" in text and f"`{s.flag}`" in text, (lang, s.key)
        for field in Settings.model_fields:
            assert field.upper() in text, (lang, field)


def test_fy_guide_008_examples_use_documentation_addresses_only():
    """FY-GUIDE-008: the guide's examples use 192.168.1.20 and /srv/forgeyard-models, nothing from a
    real deployment."""
    bad = re.compile(r"192\.168\.(?!1\.(20|21)\b)\d+\.\d+|10\.\d+\.\d+\.\d+|/media/|/tmp/|fyk_[A-Za-z0-9]{8}|"
                     r"fyj_[A-Za-z0-9]{8}|sha256:[0-9a-f]{64}")
    for f in sorted(guide.ROOT.rglob("*.md")):
        found = bad.findall(f.read_text())
        assert not found, (f, found)


def _gateway_codes() -> set[str]:
    """Every `code` the gateway can answer with: GatewayError(status, "code", …) and the few errors
    written out by hand (the host check, a stream that breaks off)."""
    codes = set()
    for f in (ROOT / "forgeyard").rglob("*.py"):
        text = f.read_text()
        codes |= set(re.findall(r'GatewayError\(\s*\w+,\s*"([a-z_]+)"', text))
        codes |= set(re.findall(r'"code":\s*"([a-z_]+)"', text)) if f.parent.name in ("gateway", "security") else set()
    return codes


def test_fy_guide_009_the_reference_lists_every_gateway_endpoint_header_and_error_code():
    """FY-GUIDE-009: a client author finds every /v1 endpoint, every X-Forgeyard-* header and every
    error `code` the gateway can send in the reference, in both languages."""
    from forgeyard.gateway import budgets, proxy

    codes = _gateway_codes() | {"budget_exhausted", "budget_unpriced"}
    assert {"model_not_found", "no_ready_replica", "local_busy", "cloud_only_model", "host_not_allowed",
            "upstream_stream_broken"} <= codes
    headers = {budgets.HEADER, "X-Forgeyard-Target", "X-Forgeyard-Spilled", "X-Forgeyard-Replica", "X-Forgeyard-Local-Only"}
    for f in (ROOT / "forgeyard").rglob("*.py"):
        headers |= {h for h in re.findall(r"X-Forgeyard-[A-Za-z-]+", f.read_text(), re.I) if not h.endswith("-")}
    headers = {h.lower() for h in headers}
    for lang in ("en", "zh-TW"):
        text = (guide.directory(lang) / "reference.md").read_text()
        for code in codes:
            assert f"`{code}`" in text, (lang, code)
        for endpoint in proxy.ENDPOINTS:
            assert f"`POST /v1/{endpoint}`" in text, (lang, endpoint)
        assert "`GET /v1/models`" in text and "`GET /v1/models/{model}`" in text
        lower = text.lower()
        for h in headers:
            assert f"`{h}`" in lower, (lang, h)


def test_fy_guide_010_the_reference_lists_every_command_and_install_option():
    """FY-GUIDE-010: every subcommand of `forgeyard`, every option of the agent's commands and every
    option of install.sh is in the reference, in both languages."""
    import argparse

    from forgeyard import cli

    words, flags = set(), set()

    def walk(p: argparse.ArgumentParser) -> None:
        for a in p._actions:
            if isinstance(a, argparse._SubParsersAction):
                for name, sub in a.choices.items():
                    words.add(name)
                    walk(sub)
            elif a.help != argparse.SUPPRESS:
                flags.update(o for o in a.option_strings if o.startswith("--") and o != "--help")

    walk(cli.build_parser())
    script = (ROOT / "forgeyard" / "install" / "install.sh").read_text()
    install = set(re.findall(r"^\s+(--[a-z-]+)(?:\|(--[a-z-]+))?\)", script, re.M))
    install_flags = {o for pair in install for o in pair if o and o != "--help"}
    assert {"serve", "agent", "secrets", "reset", "inventory", "runtimes", "init", "join", "config", "trust"} <= words
    for lang in ("en", "zh-TW"):
        text = (guide.directory(lang) / "reference.md").read_text()
        for w in words - {"agent", "secrets", "reset", "serve"}:
            assert f"forgeyard agent {w}" in text, (lang, w)
        assert "forgeyard serve" in text and "forgeyard secrets reset" in text
        for f in flags | install_flags:
            assert re.search(rf"(?<![\w-]){re.escape(f)}(?![\w-])", text), (lang, f)


def test_fy_guide_011_the_roadmap_in_both_languages_stays_in_step():
    """FY-GUIDE-011: ROADMAP.md and ROADMAP.zh-TW.md both exist and link to each other, have the
    same number of `##` and `###` headings, and list the same versions in the "Next" table; the
    Chinese README links the Chinese roadmap."""
    en_path, zh_path = ROOT / "ROADMAP.md", ROOT / "ROADMAP.zh-TW.md"
    assert en_path.is_file() and zh_path.is_file()
    en, zh = en_path.read_text(), zh_path.read_text()
    assert "[繁體中文](ROADMAP.zh-TW.md)" in en
    assert "[English](ROADMAP.md)" in zh
    for level in ("##", "###"):
        heading = re.compile(rf"^{level} \S", re.M)
        assert len(heading.findall(en)) == len(heading.findall(zh)), level
    row = re.compile(r"^\|\s*\*\*(\d+(?:\.\d+)+)", re.M)
    en_versions = set(row.findall(en))
    assert {"1.3.1", "1.4", "1.9"} <= en_versions
    assert set(row.findall(zh)) == en_versions
    readme = (ROOT / "README.zh-TW.md").read_text()
    assert "(ROADMAP.zh-TW.md)" in readme and "(ROADMAP.md)" not in readme
