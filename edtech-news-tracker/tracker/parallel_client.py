"""
Parallel.ai API client wrapper.

Uses the official `parallel-web` SDK (Parallel Search API). Wraps every call
with:
  - a bounded semaphore for concurrency control,
  - exponential backoff with jitter on rate limits/transient errors,
  - failed-query logging so a query that still fails after all retries is
    recorded (never silently dropped) and can be retried again at the end
    of the run.

NOTE: exact endpoint paths/response fields are provided by the SDK, which
insulates this integration from documentation changes. If you swap in a
different Parallel API surface, only this file needs to change.
"""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional

try:
    from parallel import Parallel
except ImportError:  # pragma: no cover - surfaced clearly at runtime instead
    Parallel = None


# Reference/profile/aggregator sites that are never a news article about a company.
NOISE_DOMAINS = [
    "linkedin.com", "crunchbase.com", "wikipedia.org", "glassdoor.com", "indeed.com",
    "owler.com", "zoominfo.com", "pitchbook.com", "facebook.com", "reddit.com",
    "youtube.com", "tiktok.com", "instagram.com", "x.com", "twitter.com",
]


@dataclass
class RawResult:
    url: str
    title: str
    excerpt: str
    source_domain: str
    published_date: Optional[str] = None


@dataclass
class FailedQuery:
    company_name: str
    lane: str
    geography: Optional[str]
    objective: str
    error: str
    urls: Optional[List[str]] = None  # populated for lane == "extract"


class ParallelClient:
    def __init__(
        self,
        api_key: str,
        max_concurrent: int = 5,
        max_retries: int = 4,
        backoff_base: float = 2.0,
        mode: str = "advanced",
        max_results: int = 10,
        budget=None,
        search_cost: float = 0.005,
        extract_cost: float = 0.001,
    ):
        if Parallel is None:
            raise RuntimeError(
                "The 'parallel-web' package is not installed. Run: pip install -r requirements.txt"
            )
        # max_retries=0: we own the retry/backoff policy ourselves (below),
        # including logging failures for an end-of-run retry pass -- letting
        # the SDK retry internally too would just stack redundant delays.
        self._client = Parallel(api_key=api_key, max_retries=0)
        self._sem = threading.Semaphore(max_concurrent)
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.mode = mode
        self.max_results = max_results
        self.failed_queries: List[FailedQuery] = []
        self._lock = threading.Lock()
        self.budget = budget  # BudgetTracker or None (unlimited)
        self.search_cost = search_cost
        self.extract_cost = extract_cost
        # Searches may only spend down to this level; the rest is held back
        # for full-text extraction (set by the main script).
        self.search_floor_remaining = 0.0

    def can_search(self) -> bool:
        return self.budget is None or (self.budget.remaining - self.search_cost >= self.search_floor_remaining)

    def search(
        self,
        objective: str,
        queries: List[str],
        company_name: str,
        lane: str,
        geography: Optional[str] = None,
        max_results: Optional[int] = None,
        after_date: Optional[str] = None,
    ) -> List[RawResult]:
        """Run one search task with retry/backoff. Never raises; on final
        failure it records the failure and returns an empty list so the run
        continues and nothing silently blocks the rest of the batch.

        `max_results` overrides the client default for this one call -- used
        to request far fewer results for the high-volume, mostly-empty
        geography sweep than for the official/global lanes."""
        last_error = "unknown error"
        if self.budget is not None:
            if self.budget.remaining - self.search_cost < self.search_floor_remaining or not self.budget.reserve(self.search_cost):
                self.budget.skipped += 1
                return []
        with self._sem:
            for attempt in range(self.max_retries + 1):
                try:
                    policy = {"exclude_domains": NOISE_DOMAINS}
                    if after_date:
                        # Native server-side date filter: stale pages never
                        # consume one of the limited result slots.
                        policy["after_date"] = after_date
                    resp = self._client.search(
                        objective=objective,
                        search_queries=queries,
                        mode=self.mode,
                        advanced_settings={
                            "source_policy": policy,
                            "max_results": max_results if max_results is not None else self.max_results,
                            # A larger excerpt means the short fallback summary
                            # (used whenever full-text extraction fails) is more
                            # likely to actually include the article's dateline
                            # and real content, instead of just a bare,
                            # near-empty slug-derived title -- real runs showed
                            # many "Full article unavailable" rows with almost
                            # no usable text at the default excerpt size.
                            "excerpt_settings": {"max_chars_per_result": 2000},
                        },
                    )
                    return self._parse_response(resp)
                except Exception as exc:  # noqa: BLE001 - broad on purpose, retried below
                    last_error = str(exc)
                    is_rate_limit = "429" in last_error or "rate limit" in last_error.lower()
                    if attempt < self.max_retries:
                        delay = self.backoff_base * (2**attempt) + random.uniform(0, 1)
                        if is_rate_limit:
                            delay *= 1.5
                        time.sleep(delay)
                        continue

        if self.budget is not None:
            self.budget.refund(self.search_cost)  # failed calls are not billed
        with self._lock:
            self.failed_queries.append(
                FailedQuery(company_name, lane, geography, objective, last_error)
            )
        return []

    def extract(self, urls: List[str], max_chars_per_result: int = 4000) -> dict[str, tuple[Optional[str], Optional[str]]]:
        """Fetches full article text for a batch of URLs (used to build a
        real 400-500 word summary instead of a short search excerpt). Never
        raises; URLs that fail are logged for the end-of-run retry pass and
        come back as (None, None) so the caller can fall back to the short
        excerpt. Each value is (full_content, title) -- the extracted
        page's own title is often more accurate than the search excerpt's
        and is used to catch a homepage/listing page the initial check
        missed (see is_generic_page re-check in enrich_with_full_summaries)."""
        result_map: dict[str, tuple[Optional[str], Optional[str]]] = {u: (None, None) for u in urls}
        last_error = "unknown error"
        reserved = 0
        if self.budget is not None:
            # Reserve as many URLs as the budget still allows (never exceed the cap).
            for _ in range(len(urls)):
                if not self.budget.reserve(self.extract_cost, kind="extract"):
                    break
                reserved += 1
            if reserved == 0:
                return result_map
            urls = urls[:reserved]
        with self._sem:
            for attempt in range(self.max_retries + 1):
                try:
                    resp = self._client.extract(
                        urls=urls,
                        advanced_settings={"full_content": {"max_chars_per_result": max_chars_per_result}},
                    )
                    for r in self._extract_results(resp):
                        get = (lambda k, default=None: getattr(r, k, default)) if not isinstance(r, dict) else r.get
                        url = get("url")
                        if url:
                            result_map[url] = (get("full_content"), get("title"))
                    return result_map
                except Exception as exc:  # noqa: BLE001
                    last_error = str(exc)
                    is_rate_limit = "429" in last_error or "rate limit" in last_error.lower()
                    if attempt < self.max_retries:
                        delay = self.backoff_base * (2**attempt) + random.uniform(0, 1)
                        if is_rate_limit:
                            delay *= 1.5
                        time.sleep(delay)
                        continue

        if self.budget is not None:
            self.budget.refund(self.extract_cost * reserved, kind="extract", count=reserved)
        with self._lock:
            self.failed_queries.append(
                FailedQuery("(batch)", "extract", None, f"{len(urls)} urls", last_error, urls=urls)
            )
        return result_map

    @staticmethod
    def _extract_results(resp) -> List:
        results = getattr(resp, "results", None)
        if results is None and isinstance(resp, dict):
            results = resp.get("results", [])
        return results or []

    def retry_failed(self) -> List[FailedQuery]:
        """Retry every currently-failed query once more (end-of-run pass).
        Returns the list of queries still failing after this pass."""
        pending = self.failed_queries
        self.failed_queries = []
        still_failed: List[FailedQuery] = []
        for fq in pending:
            if fq.lane == "extract" and fq.urls:
                self.extract(fq.urls)
            else:
                self.search(fq.objective, [fq.objective], fq.company_name, fq.lane, fq.geography)
            if self.failed_queries:
                still_failed.extend(self.failed_queries)
                self.failed_queries = []
        return still_failed

    @staticmethod
    def _parse_response(resp) -> List[RawResult]:
        """Normalize SDK response objects/dicts into RawResult, tolerating
        minor shape differences across SDK versions."""
        results = getattr(resp, "results", None)
        if results is None and isinstance(resp, dict):
            results = resp.get("results", [])
        results = results or []

        parsed: List[RawResult] = []
        for r in results:
            get = (lambda k, default=None: getattr(r, k, default)) if not isinstance(r, dict) else r.get
            url = get("url") or ""
            title = get("title") or ""
            excerpts = get("excerpts") or get("excerpt") or get("content") or []
            if isinstance(excerpts, list):
                excerpt = " ".join(str(e) for e in excerpts)
            else:
                excerpt = str(excerpts)
            domain = url.replace("https://", "").replace("http://", "").split("/")[0] if url else ""
            published = get("publish_date") or get("published_date") or get("date")
            parsed.append(RawResult(url=url, title=title, excerpt=excerpt, source_domain=domain, published_date=published))
        return parsed
