"""
Config loader: environment variables, API key handling, and tunable constants.

All secrets come from .env (via python-dotenv) or a secure runtime prompt.
Nothing here is hard-coded business data -- companies/geographies live in Excel.
"""
from __future__ import annotations

import getpass
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# News categories (must match the brief exactly; used for classification)
# ---------------------------------------------------------------------------
CATEGORIES = [
    "Executive & Leadership Changes",
    "Strategic Moves (M&A, Partnerships, Investments, Funding)",
    "Office Openings/Expansions",
    "New Products/Launches",
    "Marketing Campaigns",
    "Market Entry/Exit",
    "Government/Regulatory Developments",
    "Awards",
    "Layoffs & Restructuring",
    "Financial Results",
    "Negative News (Lawsuits, Controversies, Data Breaches, Regulatory Action, Complaints)",
]

# Content to actively exclude even if it mentions a tracked company.
EXCLUDE_PATTERNS = [
    r"\bshare(s)?\s+(price|rose|fell|gained|dropped|closed)\b",
    r"\bstock\s+(price|movement|split|buyback)\b",
    r"\banalyst\s+(rating|upgrade|downgrade|price target)\b",
    r"\bmarket\s+commentary\b",
    r"\btrading\s+(volume|session|update)\b",
]

PRESS_WIRE_DOMAINS = [
    "prnewswire.com",
    "businesswire.com",
    "globenewswire.com",
    "cision.com",
    "accesswire.com",
    "einpresswire.com",
]

# ---------------------------------------------------------------------------
# Runtime-tunable settings (env-overridable, sensible defaults)
# ---------------------------------------------------------------------------


@dataclass
class Settings:
    api_key: str
    # Which engine finds the news:
    #   "google"   (default) Google News (per-country editions) + Bing News, plus
    #              Serper.dev if SERPER_API_KEY is set. Free, key-less, far more complete.
    #   "parallel" the original Parallel.ai Search API (needs PARALLEL_API_KEY, budget-capped).
    news_provider: str = os.getenv("NEWS_PROVIDER", "google").strip().lower()
    serper_api_key: str = os.getenv("SERPER_API_KEY", "").strip()
    use_bing: bool = os.getenv("USE_BING_NEWS", "true").strip().lower() not in {"0", "false", "no"}
    bing_per_country: bool = os.getenv("BING_PER_COUNTRY", "false").strip().lower() in {"1", "true", "yes"}
    # Global request pace against Google/Bing (they throttle by IP; 429s also
    # slow the whole client down automatically). Raise it only with Serper/proxies.
    requests_per_second: float = float(os.getenv("REQUESTS_PER_SECOND", "4"))
    # "all" = every company x every country (default for Google, it is free);
    # "none" = global searches only.
    country_sweep: str = os.getenv("COUNTRY_SWEEP", "all").strip().lower()
    excel_path: Path = PROJECT_ROOT / "edtech_tracking_list.xlsx"
    output_dir: Path = PROJECT_ROOT / "output"
    checkpoint_dir: Path = PROJECT_ROOT / ".state"

    max_concurrent_requests: int = int(os.getenv("MAX_CONCURRENT_REQUESTS", "15"))
    max_retries: int = int(os.getenv("MAX_RETRIES", "4"))
    backoff_base_seconds: float = float(os.getenv("BACKOFF_BASE_SECONDS", "2"))

    relevance_review_threshold: float = float(
        os.getenv("RELEVANCE_REVIEW_THRESHOLD", "0.55")
    )

    # A full 82-geography sweep for every company proved expensive (~$500
    # for one run) because the large majority of (company, geography) pairs
    # find nothing -- most companies only ever generate news in a handful
    # of countries, if any, on a given day. Rather than searching all 82
    # countries for every company regardless of relevance, the per-geography
    # sweep now only runs for companies in these Category Code tiers (from
    # the tracking list's Category Code column); every other company still
    # gets the 2 cheap official/global searches, just not the 82-country
    # sweep. Default: A (English/Language Learning), B (Assessment/Exam),
    # C (EdTech/Online Learning/Publishing) -- the categories where local,
    # country-specific news (local partnerships, exam centers, regional
    # launches) is most central to the business and most likely to be
    # missed by a global-only search. A company with no Category Code set
    # (not in the master list) defaults to getting the full sweep anyway,
    # so an untagged company's coverage is never silently reduced.
    full_sweep_category_codes: set[str] = field(
        default_factory=lambda: {
            c.strip().upper() for c in os.getenv("FULL_SWEEP_CATEGORIES", "A,B,C").split(",") if c.strip()
        }
    )

    # One of: turbo, fast, basic, advanced (increasing thoroughness/latency,
    # and -- as far as can be observed without published per-mode pricing --
    # cost). "basic" is a reasonable default for the official/global lanes,
    # which are comparatively few calls (560 total) with a good hit rate.
    # Results scoring below this are dropped (0 disables). Reviewed runs showed
    # nearly every "Low confidence" (<0.55) row was irrelevant.
    min_relevance: float = float(os.getenv("MIN_RELEVANCE", "0.5"))
    # Extra companies (comma-separated names) that must show education context.
    edu_context_companies: set[str] = field(
        default_factory=lambda: {c.strip().lower() for c in os.getenv("EDU_CONTEXT_COMPANIES", "Criteria,Orijin").split(",") if c.strip()}
    )
    parallel_mode: str = os.getenv("PARALLEL_MODE", "basic")
    max_results_per_query: int = int(os.getenv("MAX_RESULTS_PER_QUERY", "10"))
    # The geography lane is the expensive one (tens of thousands of calls,
    # most finding nothing) -- a low max_results here costs nothing in
    # practice (there's rarely more than 1-2 genuine local hits per country
    # per day even for an active company) while keeping each call cheaper.
    geography_max_results_per_query: int = int(os.getenv("GEOGRAPHY_MAX_RESULTS_PER_QUERY", "10"))

    # ------------------------------------------------------------------
    # Spend control. Hard cap per run (resumes of the same date window share
    # the same ledger). Prices are ESTIMATES per request -- verify against
    # your Parallel.ai pricing page/dashboard and override via env if needed.
    # ------------------------------------------------------------------
    budget_usd: float = float(os.getenv("BUDGET_USD", "25"))
    # Fraction of the budget held back for full-text extraction of the final
    # (deduplicated) articles, so searching can never starve summaries.
    extract_reserve_fraction: float = float(os.getenv("EXTRACT_RESERVE_FRACTION", "0.05"))
    # Safety margin kept unspent to absorb price estimate error.
    safety_margin_fraction: float = float(os.getenv("SAFETY_MARGIN_FRACTION", "0.04"))
    search_cost_usd: float = float(
        os.getenv("SEARCH_COST_USD", "") or {"advanced": 0.01}.get(os.getenv("PARALLEL_MODE", "basic"), 0.005)
    )
    extract_cost_usd: float = float(os.getenv("EXTRACT_COST_USD", "0.001"))
    # Countries are grouped (by shared language) into one search call per
    # company per group, since a call costs the same however many countries it
    # names. "auto" picks the smallest group size (1..GEO_BATCH_MAX) at which
    # EVERY company x EVERY country is covered within the budget.
    geo_batch_size: str = os.getenv("GEO_BATCH_SIZE", "auto")
    geo_batch_max: int = int(os.getenv("GEO_BATCH_MAX", "10"))
    # Searched first when budget forces some groups to be dropped.
    geo_priority_countries: list[str] = field(
        default_factory=lambda: [
            c.strip() for c in os.getenv(
                "GEO_PRIORITY_COUNTRIES",
                "India,United States,United Kingdom,China,Brazil,Mexico,Indonesia,Japan,South Korea,"
                "Germany,France,Spain,Turkey,Saudi Arabia,United Arab Emirates,Egypt,Nigeria,Vietnam,"
                "Thailand,Philippines,Pakistan,Bangladesh,Australia,Canada,Italy,Poland,Russia,South Africa,Kenya",
            ).split(",") if c.strip()
        ]
    )

    def __post_init__(self):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)


def load_settings(require_key: bool = True) -> Settings:
    load_dotenv(PROJECT_ROOT / ".env")

    api_key = os.getenv("PARALLEL_API_KEY", "").strip()
    if os.getenv("NEWS_PROVIDER", "google").strip().lower() != "parallel":
        return Settings(api_key=api_key)  # Google/Bing/Serper need no Parallel key
    if not api_key and not require_key:
        api_key = "dry-run"
    if not api_key:
        print("\nPARALLEL_API_KEY not found in environment or .env file.")
        api_key = getpass.getpass("Enter your Parallel.ai API key (input hidden): ").strip()
        if not api_key:
            raise SystemExit("No API key provided. Exiting.")

    return Settings(api_key=api_key)
