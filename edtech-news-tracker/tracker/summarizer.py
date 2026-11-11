"""
Extractive summarizer: selects the most representative sentences from an
article's full text, in their original order, up to a target word count.

Deliberately extractive (verbatim sentences from the source) rather than
LLM-generated -- for a competitive-intelligence feed, grounding the summary
in actual article sentences avoids the risk of a generated summary
misstating or inventing a fact. No extra API/model is needed for this.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import List, Optional

_STOPWORDS = {
    "a", "about", "above", "after", "again", "against", "all", "am", "an", "and", "any",
    "are", "as", "at", "be", "because", "been", "before", "being", "below", "between",
    "both", "but", "by", "could", "did", "do", "does", "doing", "down", "during", "each",
    "few", "for", "from", "further", "had", "has", "have", "having", "he", "her", "here",
    "hers", "herself", "him", "himself", "his", "how", "i", "if", "in", "into", "is", "it",
    "its", "itself", "just", "me", "more", "most", "my", "myself", "no", "nor", "not",
    "now", "of", "off", "on", "once", "only", "or", "other", "our", "ours", "ourselves",
    "out", "over", "own", "same", "she", "should", "so", "some", "such", "than", "that",
    "the", "their", "theirs", "them", "themselves", "then", "there", "these", "they",
    "this", "those", "through", "to", "too", "under", "until", "up", "very", "was", "we",
    "were", "what", "when", "where", "which", "while", "who", "whom", "why", "will",
    "with", "would", "you", "your", "yours", "yourself", "yourselves", "also", "said",
}

_SENT_SPLIT_RE = re.compile(r'(?<=[.!?])\s+(?=[A-Z0-9"‘“])')
_WORD_RE = re.compile(r"[A-Za-z']+")

_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_BARE_URL_RE = re.compile(r"https?://\S+")
_BOILERPLATE_MARKERS = (
    "skip to content", "back to top", "contact us", "share this article",
    "share to x", "subscribe", "follow us", "sign in", "log in", "menu",
    "cookie", "privacy policy", "terms of service", "all rights reserved",
    "items per page", "newsroom", "press release distribution",
    "services & solutions", "site map", "main navigation",
)


def _clean_full_text(text: str) -> str:
    """Strips site navigation/boilerplate out of raw extracted page text
    before summarizing. Full-page extraction often prepends/interleaves menu
    links, breadcrumbs and "Contact Us"-style chrome ahead of the actual
    article body; left in, these polluted summaries (e.g. a summary opening
    with 'Contact Us Contact Us Newsroom Services...' instead of the story).
    Line-based filtering (before collapsing whitespace) lets each nav item
    be dropped individually instead of merging into one unsplittable blob
    with the real sentence that follows it (which length-only filtering on
    the final joined text can't undo)."""
    if not text:
        return text
    kept_lines = []
    for line in text.splitlines():
        stripped = re.sub(r"^#{1,6}\s*", "", line.strip())  # markdown heading syntax
        if not stripped:
            continue

        link_matches = _MD_LINK_RE.findall(stripped)
        anchor_only = _BARE_URL_RE.sub("", _MD_LINK_RE.sub(r"\1", stripped)).strip()
        low = anchor_only.lower()

        if len(anchor_only) < 60 and any(m in low for m in _BOILERPLATE_MARKERS):
            continue
        # Nav bars/breadcrumbs pack many links into few words (e.g. "Contact
        # Us Contact Us" from 3 links) -- a real sentence with one incidental
        # link has far more words per link. Using a word-count ratio (not a
        # character ratio) avoids being thrown off by long URLs diluting a
        # character-based density check.
        word_count = max(len(anchor_only.split()), 1)
        if link_matches and len(link_matches) / word_count > 0.3:
            continue
        # Breadcrumb trails ("* X / * Y / * Z") use repeated separators that
        # real prose essentially never does.
        if stripped.count(" * ") >= 2 or stripped.count(" / ") >= 2:
            continue

        kept_lines.append(anchor_only if link_matches else stripped)

    cleaned = " ".join(kept_lines)
    return re.sub(r"\s+", " ", cleaned).strip()


def _split_sentences(text: str) -> List[str]:
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return []
    sentences = _SENT_SPLIT_RE.split(text)
    return [s.strip() for s in sentences if len(s.strip()) >= 40 and len(s.split()) >= 7]


def _words(sentence: str) -> List[str]:
    return _WORD_RE.findall(sentence.lower())


def clean_short_summary(text: str) -> str:
    """Public wrapper for stripping nav/boilerplate junk out of a short
    fallback summary (built from a search excerpt, not a full extraction) --
    the same junk patterns can leak into excerpts too, not just full pages."""
    return _clean_full_text(text)


def summarize(
    text: str,
    focus_terms: Optional[List[str]] = None,
    target_words: int = 450,
    max_words: int = 500,
) -> str:
    """Returns a summary of `text`: the full text if it's already short, or
    the highest-scoring sentences (by word-frequency + lead-sentence bonus +
    mention of `focus_terms`, e.g. the company name) otherwise, capped at
    `max_words` and kept in original article order for readability."""
    text = _clean_full_text(text)
    sentences = _split_sentences(text)
    if not sentences:
        words = (text or "").split()
        return " ".join(words[:max_words]) + ("..." if len(words) > max_words else "")

    if sum(len(_words(s)) for s in sentences) <= max_words:
        return " ".join(sentences)

    freq = Counter()
    for s in sentences:
        for w in _words(s):
            if w not in _STOPWORDS and len(w) > 2:
                freq[w] += 1
    max_freq = max(freq.values()) if freq else 1
    for w in freq:
        freq[w] /= max_freq

    focus_lower = [t.lower() for t in (focus_terms or []) if t]

    scored = []  # (index, sentence, score, word_count)
    for idx, s in enumerate(sentences):
        ws = _words(s)
        if not ws:
            continue
        score = sum(freq.get(w, 0) for w in ws) / len(ws)
        if idx < 3:
            score += 0.15  # news leads usually carry the crux
        if any(t in s.lower() for t in focus_lower):
            score += 0.2
        scored.append((idx, s, score, len(ws)))

    selected = set()
    word_count = 0
    for idx, s, score, wc in sorted(scored, key=lambda x: x[2], reverse=True):
        if word_count >= target_words:
            break
        selected.add(idx)
        word_count += wc

    ordered = [s for idx, s, _, _ in scored if idx in selected]

    # Trim from the end by whole sentences, never mid-sentence -- cutting a
    # sentence off partway (the old "..." word-count truncation) read as an
    # incomplete, lower-quality summary even when the content itself was fine.
    while ordered and sum(len(s.split()) for s in ordered) > max_words:
        ordered.pop()

    return " ".join(ordered)
