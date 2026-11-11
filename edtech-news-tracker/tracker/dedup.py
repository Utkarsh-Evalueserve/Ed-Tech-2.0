"""
Deduplicates articles across sources and languages:
  - exact match by normalized URL
  - near-duplicate match by title similarity (same company, close titles)

The first-seen article is kept as the main row; other sources reporting the
same story are appended (by name) into the Source Name cell rather than
being dropped, per the brief ("keep one main article and list the other
sources").
"""
from __future__ import annotations

from difflib import SequenceMatcher
from typing import List
from urllib.parse import urlsplit, urlunsplit

from .relevance import Article

_TITLE_SIMILARITY_THRESHOLD = 0.85


def _normalize_url(url: str) -> str:
    if not url:
        return url
    parts = urlsplit(url)
    # drop query/fragment (tracking params) and trailing slash
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc.lower(), path, "", ""))


def deduplicate(articles: List[Article]) -> List[Article]:
    by_url: dict[str, Article] = {}
    ordered_keys: List[str] = []

    for art in articles:
        norm_url = _normalize_url(art.url)
        if norm_url in by_url:
            _merge_source(by_url[norm_url], art)
            continue
        by_url[norm_url] = art
        ordered_keys.append(norm_url)

    deduped: List[Article] = []
    for key in ordered_keys:
        candidate = by_url[key]
        merged_into_existing = False
        for kept in deduped:
            if (
                kept.company == candidate.company
                and _titles_similar(kept.headline_en, candidate.headline_en)
            ):
                _merge_source(kept, candidate)
                merged_into_existing = True
                break
        if not merged_into_existing:
            deduped.append(candidate)

    return deduped


def _titles_similar(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return SequenceMatcher(None, a.lower(), b.lower()).ratio() >= _TITLE_SIMILARITY_THRESHOLD


def _merge_source(main: Article, other: Article) -> None:
    if other.url and other.url != main.url and other.url not in main.other_source_urls:
        main.other_source_urls.append(other.url)
    if other.source_name and other.source_name != main.source_name:
        extra = f"{other.source_name}"
        if extra not in main.source_name:
            main.source_name = f"{main.source_name} (also: {extra})"
    main.relevance_score = max(main.relevance_score, other.relevance_score)
