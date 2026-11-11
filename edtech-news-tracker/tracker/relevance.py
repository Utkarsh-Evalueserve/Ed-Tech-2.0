"""
Relevance filtering, disambiguation, category classification and scoring.

Nothing is ever silently dropped for being low-confidence: items below the
review threshold are kept and flagged for manual review instead. Only items
that are pure stock/analyst noise (the brief's explicit exclude list) with no
other topical signal are filtered out.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from .config import CATEGORIES, EXCLUDE_PATTERNS, PRESS_WIRE_DOMAINS
from .excel_loader import Company, Geography

_EXCLUDE_RE = re.compile("|".join(EXCLUDE_PATTERNS), re.IGNORECASE)

_CATEGORY_KEYWORDS = {
    "Executive & Leadership Changes": ["ceo", "cfo", "coo", "appoint", "steps down", "resign", "named president", "new chief", "hires", "promoted"],
    "Strategic Moves (M&A, Partnerships, Investments, Funding)": ["acquisition", "acquire", "merger", "partner", "invest", "funding", "raises", "series a", "series b", "series c", "stake", "collaborat", "mou", "memorandum of understanding", "signs agreement", "pilot program"],
    "Office Openings/Expansions": ["opens office", "expansion", "new office", "expands to", "opens new"],
    "New Products/Launches": ["launch", "unveils", "introduces", "new product", "rolls out", "release of"],
    "Marketing Campaigns": ["campaign", "marketing", "brand partnership", "sponsorship", "conference", "summit", "seminar", "conclave", "workshop", "keynote"],
    "Market Entry/Exit": ["enters market", "market entry", "exits", "withdraws from", "shuts down operations"],
    "Government/Regulatory Developments": ["regulation", "regulatory", "government", "ministry", "policy", "compliance", "approval from", "accreditation", "accredited", "certification", "certified by"],
    "Awards": ["award", "wins", "recognized", "honored", "named best", "named a luminary", "ranked among"],
    "Layoffs & Restructuring": ["layoff", "layoffs", "restructuring", "job cuts", "workforce reduction"],
    "Financial Results": ["quarterly results", "annual report", "revenue", "earnings", "financial results"],
    "Negative News (Lawsuits, Controversies, Data Breaches, Regulatory Action, Complaints)": ["lawsuit", "sues", "data breach", "controversy", "complaint", "fined", "investigation", "scandal", "outage", "disruption", "service issue"],
}

# Phrases that make a keyword match misleading for its category -- e.g. a
# government "launches a tender" is a procurement notice, not a product
# launch, even though it hits the New Products/Launches keyword "launch".
_CATEGORY_FALSE_FRIEND_PATTERNS = {
    "New Products/Launches": [r"launches? (a |an )?(tender|rfp|request for proposal)", r"invites bids"],
}
_CATEGORY_FALSE_FRIEND_RE = {
    cat: re.compile("|".join(pats), re.IGNORECASE) for cat, pats in _CATEGORY_FALSE_FRIEND_PATTERNS.items()
}

_INDUSTRY_CONTEXT = [
    "education", "edtech", "ed-tech", "learning", "school", "university",
    "student", "curriculum", "e-learning", "online course", "test prep",
    "language learning", "assessment", "training",
]

# Domains/URL patterns that are reference/profile pages, not news -- these
# were the "general company pages" showing up as noise in early runs.
_GENERIC_PAGE_DOMAINS = [
    "linkedin.com", "crunchbase.com", "wikipedia.org", "glassdoor.com",
    "indeed.com", "owler.com", "zoominfo.com", "pitchbook.com",
    "builtwith.com", "similarweb.com",
]
_GENERIC_PAGE_URL_PATTERNS = [
    r"/about(-us)?/?$", r"/company/?$", r"/overview/?$", r"/profile/?$",
    r"/wiki/", r"/directory/", r"/author/",
]
_GENERIC_TITLE_PATTERNS = [
    r"^about\s", r"\boverview\b", r"\bcompany profile\b", r"\| linkedin\b",
    r"^home\s*[-|]", r"\bwikipedia\b",
    # Wire-service topic/index pages (e.g. "X News and Press Releases | PR
    # Newswire", "Press Releases | Y") -- a feed of many stories, not one
    # article. These showed up as noise/stale-content rows in real runs.
    r"news (and|&) press releases", r"^press releases\b",
    r"news,\s*analysis,\s*announcements",
]
_GENERIC_PAGE_URL_RE = re.compile("|".join(_GENERIC_PAGE_URL_PATTERNS), re.IGNORECASE)
_GENERIC_TITLE_RE = re.compile("|".join(_GENERIC_TITLE_PATTERNS), re.IGNORECASE)
_ROOT_PATH_RE = re.compile(r"^https?://[^/]+/?$")  # just a domain, no path at all: a homepage


@dataclass
class Article:
    company: str
    geography: str
    category: str
    headline_en: str
    summary_en: str
    source_name: str
    publication_date: Optional[str]
    url: str
    relevance_score: float
    review_flag: str
    other_source_urls: List[str] = field(default_factory=list)
    _dedup_key: str = ""


def is_excluded_noise(text: str, matched_category: Optional[str]) -> bool:
    """True only when the ONLY signal is stock/analyst noise and no other
    category keyword matched -- never used to drop a genuinely relevant story."""
    if matched_category is not None:
        return False
    return bool(_EXCLUDE_RE.search(text))


def is_generic_page(url: str, title: str, source_domain: str, company: Optional[Company] = None) -> bool:
    """True for reference/profile/directory pages (LinkedIn, Wikipedia,
    Crunchbase, an 'About Us' page, etc.) that are not news articles -- the
    main source of 'general company pages' noise in broad searches."""
    url = url or ""
    title = title or ""

    if any(d in source_domain for d in _GENERIC_PAGE_DOMAINS):
        return True
    if _GENERIC_PAGE_URL_RE.search(url):
        return True
    if _GENERIC_TITLE_RE.search(title):
        return True
    if _ROOT_PATH_RE.match(url):
        return True  # bare homepage URL, not a specific article

    # A URL with only 1-2 short path segments and no long descriptive slug
    # (real article URLs almost always carry one, e.g. "-cambium-learning-
    # group-ceo-named-...") OR a numeric article ID (e.g. government/wire
    # sites that use "/en/61550/" instead of a text slug) is usually a
    # section/tag/topic listing page, not a single story (e.g.
    # cam.ac.uk/news/technology).
    path = re.sub(r"^https?://[^/]+", "", url).rstrip("/")
    segments = [s for s in path.split("/") if s]
    looks_like_specific_item = any(
        (len(s) >= 15 and "-" in s) or (s.isdigit() and len(s) >= 3) for s in segments
    )
    if segments and len(segments) <= 2 and not looks_like_specific_item:
        return True

    if company is not None:
        names = [n.lower() for n in company.all_names()]
        title_lower = title.lower().strip()
        # A title that's just the bare company name (a blog/author index
        # card) or that opens with "<Company> |" (a directory/member-profile
        # entry, e.g. "University of Babylon | Higher Education and
        # research for...") reads as a listing, not a news headline.
        if title_lower in names:
            return True
        if any(title_lower.startswith(n + " |") or title_lower.startswith(n + "|") for n in names):
            return True

    return False


def company_mentioned_prominently(company: Company, headline: str, excerpt: str) -> bool:
    """True only if the company/alias appears in the headline, or near the
    start of the excerpt -- filters out results where the name is buried in
    a long article about something else (a passing mention, not the subject)."""
    names = [n.lower() for n in company.all_names()]
    if any(n in (headline or "").lower() for n in names):
        return True
    lead = (excerpt or "")[:250].lower()
    return any(n in lead for n in names)


def mention_count(names: List[str], text: str) -> int:
    lower = (text or "").lower()
    return sum(lower.count(n.lower()) for n in names if n)


def is_prominent_in_full_text(company_name: str, headline: str, full_text: str) -> bool:
    """Stricter re-check run on the FULL article (after extraction), not
    just the short search excerpt -- catches cases where a short excerpt
    happened to show the name near the top, but the actual article is about
    something else and only mentions the company once in passing (the
    excerpt-only check can't see that; the full text can)."""
    name = (company_name or "").lower()
    if not name:
        return True
    if name in (headline or "").lower():
        return True
    return mention_count([company_name], full_text) >= 2


def classify_category(text: str) -> Optional[str]:
    lower = text.lower()
    best_cat, best_hits = None, 0
    for cat in CATEGORIES:
        if cat in _CATEGORY_FALSE_FRIEND_RE and _CATEGORY_FALSE_FRIEND_RE[cat].search(lower):
            continue  # e.g. a procurement "launches a tender" isn't a product launch
        hits = sum(1 for kw in _CATEGORY_KEYWORDS[cat] if kw in lower)
        if hits > best_hits:
            best_cat, best_hits = cat, hits
    return best_cat


def score_relevance(
    company: Company,
    headline: str,
    excerpt: str,
    source_domain: str,
    within_date_window: bool,
    date_known: bool = True,
) -> float:
    text = f"{headline} {excerpt}".lower()
    score = 0.0

    # Exact company/alias name match
    if any(name.lower() in text for name in company.all_names()):
        score += 0.35

    # Official domain match is the strongest disambiguation signal
    if company.website and _domain(company.website) and _domain(company.website) in source_domain:
        score += 0.3

    # Known press-wire source is a reasonably strong signal
    if any(d in source_domain for d in PRESS_WIRE_DOMAINS):
        score += 0.15

    # Industry context words help disambiguate generic company names
    if any(term in text for term in _INDUSTRY_CONTEXT):
        score += 0.2

    # A genuinely-confirmed date is a signal either way; an UNKNOWN date is
    # neutral, not a free bonus -- otherwise undated items (which are also
    # exempt from the date-window drop) get an inflated score that can mask
    # a wrong-entity match (e.g. a same-named but unrelated company/product)
    # that happens to lack a parseable publish date.
    if date_known:
        score += 0.1 if within_date_window else -0.2

    return max(0.0, min(1.0, round(score, 2)))


def detect_geography(text: str, geographies: List[Geography], default: str = "Global") -> str:
    """Attributes a result to one of the tracked geographies by checking for
    an explicit country-name mention, instead of forcing a separate API call
    per country. Falls back to 'Global' when no specific country is named."""
    lower = text.lower()
    for geo in geographies:
        if geo.country.lower() in lower:
            return geo.country
    return default


def _domain(url: str) -> str:
    if not url:
        return ""
    url = url.replace("https://", "").replace("http://", "").replace("www.", "")
    return url.split("/")[0]
