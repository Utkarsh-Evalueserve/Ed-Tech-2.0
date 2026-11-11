"""
Translates non-English headlines/summaries to English.

Uses deep-translator (Google Translate backend, no separate API key needed).
Translation failures never crash the run -- they fall back to the original
text and the caller is expected to flag the item for manual review.
"""
from __future__ import annotations

from typing import Tuple

try:
    from deep_translator import GoogleTranslator
except ImportError:  # pragma: no cover
    GoogleTranslator = None


def _looks_like_english(text: str) -> bool:
    """Cheap heuristic to skip unnecessary translation calls: if the text is
    almost entirely ASCII, it's overwhelmingly likely already English (most
    other languages in our geography list use accented or non-Latin
    characters). Saves API calls and avoids false 'translation uncertain'
    flags on text that never needed translating."""
    if not text:
        return True
    non_ascii = sum(1 for ch in text if ord(ch) > 127)
    return (non_ascii / len(text)) < 0.02


def translate_to_english(text: str) -> Tuple[str, bool]:
    """Returns (text_in_english, translation_succeeded)."""
    if not text or not text.strip():
        return text, True
    if _looks_like_english(text):
        return text, True
    if GoogleTranslator is None:
        return text, False
    try:
        translated = GoogleTranslator(source="auto", target="en").translate(text[:4900])
        return translated or text, translated is not None
    except Exception:  # noqa: BLE001 - translation is best-effort
        return text, False
