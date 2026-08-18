"""Interface texts.

The app speaks Khurerdeutsch. Texts live in i18n/gsw-chur.json rather than inline, so
wording can be corrected without touching code -- useful for a dialect that has no
settled spelling. en.json is kept as the reference and as a fallback, so a key that is
missing from the dialect file shows English instead of breaking the page.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

I18N_DIR = Path(__file__).resolve().parent / "i18n"
LANGUAGE = "gsw-chur"
FALLBACK = "en"


@lru_cache(maxsize=None)
def _catalogue(language: str) -> dict:
    path = I18N_DIR / f"{language}.json"
    data = json.loads(path.read_text("utf-8"))
    flat: dict[str, str] = {}

    def walk(node, prefix=""):
        for key, value in node.items():
            if key.startswith("_"):
                continue
            if isinstance(value, dict):
                walk(value, f"{prefix}{key}.")
            elif isinstance(value, str):
                flat[f"{prefix}{key}"] = value

    walk(data)
    return flat


def t(key: str, **values) -> str:
    """The text for `key`, with any {placeholders} filled in."""
    text = _catalogue(LANGUAGE).get(key)
    if text is None:
        text = _catalogue(FALLBACK).get(key)
    if text is None:
        # Better a visible key than a crash mid-render.
        return key
    if not values:
        return text
    try:
        return text.format(**values)
    except (KeyError, IndexError):
        # A translation with a stray brace must not take the page down.
        return text


def missing_keys() -> list[str]:
    """Keys present in the reference catalogue but absent from the active one."""
    return sorted(set(_catalogue(FALLBACK)) - set(_catalogue(LANGUAGE)))
