"""
Precision gate: drops results a human reviewer would call irrelevant.

Rules were derived from a reviewed run (Nov 11): the "Irrelevant" rows were
(1) listing / tag / events / tender / archive pages rather than one article,
(2) law-firm investor-alert spam about a listed company,
(3) a different entity sharing the company's name (Apollo PE, Criteria
    Venture Tech, ...), and
(4) off-topic news about broad organisations the list tracks only for their
    education angle (Mastercard payments, Gates medical grants, World Bank
    fashion fund, UNICEF speeches...).
Rule 3/4 are driven by the tracking list itself (Category Code, the Notes
column) rather than hard-coded company names.
"""
from __future__ import annotations

import re
from typing import Optional

from .excel_loader import Company

EDU_TERMS = [
    "education", "educational", "edtech", "ed-tech", "learning", "learner", "school", "schools",
    "university", "universities", "college", "student", "students", "teacher", "teachers",
    "curriculum", "classroom", "scholarship", "literacy", "tutor", "tutoring", "academic",
    "exam", "examination", "ielts", "toefl", "assessment", "english language", "language learning",
    "e-learning", "online course", "course", "training", "upskill", "reskill", "campus", "k-12",
    "ai tutor", "apprenticeship", "vocational", "graduate", "enrol",
]
_EDU_RE = re.compile("|".join(re.escape(t) for t in EDU_TERMS), re.IGNORECASE)

_LISTING_TITLE_RE = re.compile(
    r"upcoming events|^events?\b|\bevents? ›|\bnews archive\b|^\s*\d{4}\s*[-–]\s|\barchive\b|"
    r"\btenders?\b|request for proposals?|\bRFP\b|\bjob (listing|openings?)\b|\bcareers?\b\s*[|-]|"
    r"\bscorecard\b|\bnewsletter\b|^browsing news|\bsearch results\b|\bpage \d+\b|"
    r"\bdeep tech week\b|/ organizations? /",
    re.IGNORECASE,
)
_LISTING_URL_RE = re.compile(
    r"/(category|categories|tag|tags|topic|topics|archive|archives|events?/category|listings?|"
    r"tenders?|contractresults|organizations?|author|page/\d+)(/|\.|$)|[?&](page|paged|s)=|"
    r"/news/[a-z0-9-]+/\d{4}/?$|\.pdf(\?|$)",
    re.IGNORECASE,
)
# Law-firm "investors have until X to file" solicitations about a listed company.
_SOLICITATION_RE = re.compile(
    r"investors? (have|has) until|class action|lead plaintiff|securities (fraud|litigation)|"
    r"shareholder (alert|rights|investigation)|deadline alert|investigation on behalf of|"
    r"rosen law|pomerantz|bragar|levi\s*&\s*korsinsky|faruqi|glancy|hagens berman|kessler topaz",
    re.IGNORECASE,
)
_NOT_RE = re.compile(r"\bNOT\b(.*)", re.IGNORECASE | re.DOTALL)


def is_listing_or_spam(url: str, title: str, text: str) -> Optional[str]:
    """Returns a reason string if the item is a listing page / solicitation, else None."""
    if _LISTING_TITLE_RE.search(title or ""):
        return "listing/events/tender/archive page"
    if _LISTING_URL_RE.search(url or ""):
        return "listing/category/pdf URL"
    if _SOLICITATION_RE.search(f"{title} {text[:600]}"):
        return "law-firm investor solicitation"
    return None


def has_edu_context(text: str, min_distinct: int = 1) -> bool:
    return len({m.group(0).lower() for m in _EDU_RE.finditer(text or "")}) >= min_distinct


def edu_hits_needed(company: Company) -> int:
    """Broad organisations (NGOs/foundations/adjacent corporates) must show
    stronger education context (2+ distinct education terms) than a company
    that is itself an education business."""
    return 2 if (company.category_code or "").upper() in {"F", "H"} else 1


def requires_edu_context(company: Company, extra_names: set[str]) -> bool:
    """Companies tracked only for an education angle, or whose names collide
    with unrelated businesses, must show education context in the article."""
    code = (company.category_code or "").upper()
    notes = company.notes or ""
    if code in {"F", "H"}:  # NGOs/foundations/government + adjacent corporates
        return True
    if re.search(r"\bONLY\b|\bNOT\b", notes):
        return True
    if company.name.lower() in extra_names:
        return True
    return False


def names_a_excluded_entity(company: Company, text: str) -> bool:
    """Notes like "Apollo Education Group ONLY. NOT Apollo private equity,
    Apollo Global Management, or Apollo Hospitals" -> reject text naming those."""
    m = _NOT_RE.search(company.notes or "")
    if not m:
        return False
    lower = (text or "").lower()
    for part in re.split(r",|;|\bor\b|\band\b", m.group(1)):
        part = part.strip(" .").lower()
        if len(part) >= 6 and part in lower:
            return True
    return False
