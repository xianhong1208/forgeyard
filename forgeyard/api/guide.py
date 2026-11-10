"""The user guide for the in-app help (/guide/<lang>/<page> in the web UI). Public, like the
"Trust this server" page: it explains how to install the server and its agents, before anyone can
sign in, and holds nothing secret."""

from __future__ import annotations

from fastapi import APIRouter

from forgeyard import guide
from forgeyard.api.deps import fail

router = APIRouter(prefix="/api/guide", tags=["guide"])


@router.get("")
def guide_index():
    return {"languages": [{"code": c, "name": n} for c, n in guide.LANGUAGES.items()],
            "pages": {lang: guide.pages(lang) for lang in guide.LANGUAGES}}


@router.get("/{lang}/{slug}")
def guide_page(lang: str, slug: str):
    try:
        return guide.page(lang, slug)
    except guide.NotFound:
        raise fail(404, "not_found", "No such page in the guide.") from None
