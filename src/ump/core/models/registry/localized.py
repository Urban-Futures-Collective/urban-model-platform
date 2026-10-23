"""Language maps ("L" in REF-F12, D13): ``{"de": "...", "en": "..."}``.

Read endpoints resolve one language: the caller's preferences in order, then
the model's ``default_lang`` — which the database guarantees for ``full_name``
and the core for every other filled map.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


def pick_language(
    preferences: Sequence[str], available: Sequence[str], default_lang: str
) -> str:
    """First preferred language present in *available*, else *default_lang*.

    A preference ``de-CH`` also matches ``de``; ``*`` is not a language here.
    """
    for pref in preferences:
        if pref in available:
            return pref
        base = pref.split("-", 1)[0]
        if base in available:
            return base
    return default_lang


def resolve(value: Mapping[str, Any] | None, lang: str, default_lang: str) -> Any:
    """The entry for *lang*, falling back to *default_lang*; None if neither."""
    if not value:
        return None
    if lang in value:
        return value[lang]
    return value.get(default_lang)


def parse_accept_language(header: str | None) -> list[str]:
    """Language tags from an Accept-Language header, by descending q-value."""
    if not header:
        return []
    weighted: list[tuple[float, int, str]] = []
    for index, part in enumerate(header.split(",")):
        tag, _, params = part.strip().partition(";")
        tag = tag.strip()
        if not tag or tag == "*":
            continue
        q = 1.0
        if params.strip().startswith("q="):
            try:
                q = float(params.strip()[2:])
            except ValueError:
                continue
        if q > 0:
            weighted.append((-q, index, tag))
    return [tag for _, _, tag in sorted(weighted)]
