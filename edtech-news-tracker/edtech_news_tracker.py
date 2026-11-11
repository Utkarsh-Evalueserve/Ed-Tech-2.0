#!/usr/bin/env python3
"""
EdTech News Tracker
====================
Daily multilingual news monitoring for Ed-Tech competitors, powered by the
Parallel.ai Search API.

Run from a terminal:
    python edtech_news_tracker.py

See README.md for setup instructions (this is written for a non-developer).
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

from dateutil import parser as dateparser
from tqdm import tqdm

from tracker import checkpoint
from tracker.config import load_settings, Settings
from tracker.dedup import deduplicate
from tracker.excel_loader import (
    Company, Geography, TrackingListError, load_tracking_list,
)
from tracker.exporter import RunLog, export
from tracker.parallel_client import ParallelClient, RawResult
from tracker.budget import BudgetTracker
from tracker.planner import choose_batch_size, group_geographies, plan_geo_units, unit_key
from tracker.query_builder import (
    SearchTask, build_geography_group_task, build_global_broad_task, build_official_press_task,
)
from tracker.relevance import (
    Article, classify_category, company_mentioned_prominently, detect_geography,
    is_excluded_noise, is_generic_page, is_prominent_in_full_text, score_relevance,
)
from tracker.summarizer import clean_short_summary, summarize
from tracker.translator import translate_to_english

DATE_FMT = "%Y-%m-%d"


# ---------------------------------------------------------------------------
# Date input handling
# ---------------------------------------------------------------------------
def prompt_date_range() -> tuple[datetime, datetime]:
    print("\nEnter the reporting window (format YYYY-MM-DD).")
    print("Press Enter on both to default to the last 24 hours.\n")

    start_raw = input("Start date [Enter = last 24h]: ").strip()
    end_raw = input("End date   [Enter = now]:       ").strip()

    if not start_raw and not end_raw:
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(hours=24)
        print(f"Using default window: {start_dt.date()} to {end_dt.date()} (last 24 hours)")
        return start_dt, end_dt

    if not start_raw or not end_raw:
        print("Please provide BOTH a start and end date, or leave BOTH blank for the default.")
        return prompt_date_range()

    try:
        start_dt = datetime.strptime(start_raw, DATE_FMT)
        end_dt = datetime.strptime(end_raw, DATE_FMT)
    except ValueError:
        print(f"Could not parse dates. Use the exact format {DATE_FMT}, e.g. 2026-09-01.")
        return prompt_date_range()

    if start_dt > end_dt:
        print("Start date must be before or equal to the end date. Please try again.")
        return prompt_date_range()

    if end_dt > datetime.now() + timedelta(days=1):
        print("End date is in the future. Please try again.")
        return prompt_date_range()

    return start_dt, end_dt + timedelta(hours=23, minutes=59, seconds=59)


_DATE_SNIFF_RE = re.compile(
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    r"Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+"
    r"\d{1,2},?\s+\d{4}\b"
    r"|\b\d{4}-\d{2}-\d{2}\b"
    r"|\b\d{1,2}\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\.?,?\s+\d{4}\b",
    re.IGNORECASE,
)


def _sniff_date(text: str | None) -> datetime | None:
    """Finds an explicit, unambiguous date (e.g. 'May 2, 2023' or
    '2026-10-23') inside free text. Used as a fallback when a result has no
    structured publish-date metadata, and to catch stale content (an old
    article with a dateline baked into the text) that metadata alone missed."""
    if not text:
        return None
    m = _DATE_SNIFF_RE.search(text)
    if not m:
        return None
    try:
        return dateparser.parse(m.group(0), fuzzy=False)
    except (ValueError, OverflowError):
        return None


# BusinessWire's URL scheme embeds the exact release date right in the path
# (".../news/home/YYYYMMDDnnnnnn/en/..."), confirmed against real stale
# rows from actual runs (dates found this way ranged from 2021 to 2026, all
# correctly years/months old) -- this is a 100% reliable source of truth
# when present, used ahead of fuzzier text-sniffing.
_BUSINESSWIRE_URL_DATE_RE = re.compile(r"/news/home/(\d{4})(\d{2})(\d{2})\d*/")


def _date_from_url(url: str | None) -> datetime | None:
    if not url:
        return None
    m = _BUSINESSWIRE_URL_DATE_RE.search(url)
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _within_window(
    published_raw: str | None,
    start_dt: datetime,
    end_dt: datetime,
    fallback_text: str | None = None,
    url: str | None = None,
) -> tuple[bool, bool]:
    """Returns (within_window, date_was_determined). Tries the structured
    publish-date field first, then a date decoded from the URL itself (e.g.
    BusinessWire), then falls back to sniffing an explicit date out of
    `fallback_text` (title/excerpt) -- this catches cases where the search
    result has no date metadata but the text or URL carries one anyway
    (which, left unhandled, let stale/old articles through as "undated and
    therefore exempt" in earlier runs)."""
    pub_dt = None
    if published_raw:
        try:
            pub_dt = dateparser.parse(published_raw, fuzzy=True)
        except (ValueError, OverflowError):
            pub_dt = None
    if pub_dt is None:
        pub_dt = _date_from_url(url)
    if pub_dt is None:
        pub_dt = _sniff_date(fallback_text)
    if pub_dt is None:
        return True, False  # genuinely unknown: keep, but flagged for review by caller
    if pub_dt.tzinfo is not None:
        pub_dt = pub_dt.replace(tzinfo=None)
    return start_dt <= pub_dt <= end_dt, True


# ---------------------------------------------------------------------------
# Per-result processing
# ---------------------------------------------------------------------------
def _raw_result_to_article(
    result: RawResult,
    task: SearchTask,
    geographies: list[Geography],
    settings: Settings,
    start_dt: datetime,
    end_dt: datetime,
) -> Article | None:
    is_own_domain = bool(
        result.source_domain
        and task.company.website
        and _domain_match(task.company.website, result.source_domain)
    )

    within_window, date_known = _within_window(
        result.published_date,
        start_dt,
        end_dt,
        fallback_text=f"{result.title} {result.excerpt}",
        url=result.url,
    )
    if date_known and not within_window:
        return None  # outside the requested reporting window
    if not date_known and not is_own_domain:
        # Undated items are only trusted when they're on the company's OWN
        # verified domain -- real feedback showed press-wire archives
        # (BusinessWire, PR Newswire) are the dominant source of "old news"
        # complaints: a wire service re-indexing a 2021-2023 release with no
        # date metadata is indistinguishable from today's without a
        # confirmed date, so a wire alone is no longer enough to exempt it.
        return None

    if is_generic_page(result.url, result.title, result.source_domain, task.company):
        return None  # LinkedIn/Wikipedia/Crunchbase/"About Us"-style pages, not news

    headline_en, headline_ok = translate_to_english(result.title)
    summary_raw = result.excerpt[:600]
    summary_en, summary_ok = translate_to_english(summary_raw)
    # trim to a 2-3 line summary
    summary_en = summary_en.strip()
    if len(summary_en) > 480:
        summary_en = summary_en[:477] + "..."

    if not company_mentioned_prominently(task.company, headline_en, summary_en):
        return None  # name only appears in passing, not actually about this company

    combined_text = f"{headline_en} {summary_en}"
    category = classify_category(combined_text)
    if category is None:
        return None  # no recognizable news-category signal at all
    if is_excluded_noise(combined_text, category):
        return None

    score = score_relevance(
        task.company, headline_en, summary_en, result.source_domain, within_window, date_known
    )

    review_reasons = []
    if score < settings.relevance_review_threshold:
        review_reasons.append("Low confidence")
    if not date_known:
        review_reasons.append("Date unverified")
    if not headline_ok or not summary_ok:
        review_reasons.append("Translation uncertain")

    review_flag = "; ".join(review_reasons) if review_reasons else "OK"

    # A task scoped to a specific geography (the per-country lane) already
    # knows its country with certainty -- prefer that over inferring one by
    # scanning the text, which is only needed for the global-scope lanes.
    if task.geography:
        geography_name = task.geography.country
    elif task.geographies:  # multi-country group call: attribute by explicit mention, else the group
        geography_name = detect_geography(combined_text, task.geographies, default=", ".join(g.country for g in task.geographies))
    else:
        geography_name = detect_geography(combined_text, geographies)

    return Article(
        company=task.company.name,
        geography=geography_name,
        category=category,
        headline_en=headline_en or result.title,
        summary_en=summary_en,
        source_name=result.source_domain or "Unknown",
        publication_date=result.published_date,
        url=result.url,
        relevance_score=score,
        review_flag=review_flag,
    )


def _domain_match(website: str, source_domain: str) -> bool:
    w = website.replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0]
    return bool(w) and w in source_domain


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------
def run_tasks(tasks: list[SearchTask], client: ParallelClient, settings: Settings,
              geographies: list[Geography], start_dt: datetime, end_dt: datetime) -> list[Article]:
    articles: list[Article] = []
    for task in tasks:
        max_results = (
            settings.geography_max_results_per_query
            if task.lane == "geography"
            else settings.max_results_per_query
        )
        raw_results = client.search(
            task.objective, task.search_queries, task.company.name, task.lane, max_results=max_results
        )
        for r in raw_results:
            art = _raw_result_to_article(r, task, geographies, settings, start_dt, end_dt)
            if art:
                articles.append(art)
    return articles


def _run_units(units, fn, client, completed, articles, settings, start_tag, end_tag, desc, zero_news=None):
    """Runs units (key, callable-args) concurrently; checkpoints each finished
    unit. Units skipped because the budget ran out are NOT marked completed,
    so a rerun with a higher BUDGET_USD resumes exactly where this stopped."""
    with ThreadPoolExecutor(max_workers=settings.max_concurrent_requests) as pool:
        futures = {pool.submit(fn, *args): (key, name) for key, name, args in units}
        for fut in tqdm(as_completed(futures), total=len(futures), desc=desc, unit="call"):
            key, name = futures[fut]
            try:
                res = fut.result()
            except Exception as exc:  # noqa: BLE001
                print(f"\n[WARN] Unexpected error on '{key}': {exc}")
                continue
            if res is None:  # skipped: budget exhausted
                continue
            if zero_news is not None and not res:
                zero_news.append(name)
            articles.extend(res)
            completed.append(key)
            checkpoint.save(settings.checkpoint_dir, start_tag, end_tag, completed, articles)


def _append_flag(existing: str, note: str) -> str:
    if not existing or existing == "OK":
        return note
    return existing if note in existing else f"{existing}; {note}"


def enrich_with_full_summaries(
    articles: list[Article],
    client: ParallelClient,
    settings: Settings,
    start_dt: datetime,
    end_dt: datetime,
) -> list[Article]:
    """For each (deduplicated) article, fetches the full article text via
    Parallel's extract API, translates it to English if needed, and replaces
    the short search-excerpt summary with a real extractive summary covering
    the article's crux (~400-500 words). Runs only after dedup so each URL
    is fetched once, not once per raw search hit. Falls back to the original
    short summary -- never drops the article -- if extraction fails for a
    URL (paywalled, blocked, etc.).

    Also catches stale content whose dateline only shows up in the full
    body text (not in the search result's metadata or excerpt, so the
    earlier date check couldn't see it) and drops it -- this is what let an
    old "raises Series A" press release through as if it were current in an
    earlier run. Returns the surviving article list."""
    url_to_articles: dict[str, list[Article]] = {}
    # Highest-relevance first, so if the budget only covers some extractions
    # the best articles get full summaries and the rest keep short ones.
    for art in sorted(articles, key=lambda a: -(a.relevance_score or 0)):
        if art.url:
            url_to_articles.setdefault(art.url, []).append(art)
    urls = list(url_to_articles.keys())
    if not urls:
        return articles

    to_drop: set[int] = set()
    batch_size = 20
    batches = [urls[i : i + batch_size] for i in range(0, len(urls), batch_size)]
    content_map: dict[str, tuple[str | None, str | None]] = {}

    with ThreadPoolExecutor(max_workers=settings.max_concurrent_requests) as pool:
        futures = [pool.submit(client.extract, batch) for batch in batches]
        for fut in tqdm(as_completed(futures), total=len(futures), desc="Full-text extraction", unit="batch"):
            content_map.update(fut.result())

    # A URL can fail inside a mixed batch of 20 for a transient reason (a
    # single slow/blocked page) even when most of the batch succeeds -- one
    # retry per URL, alone, recovers a meaningful share of these rather than
    # accepting the batch's partial failure as final. This directly targets
    # the "Incomplete Title and Summary" feedback, which traced back to
    # extraction failing and leaving only a bare, near-empty fallback.
    failed_urls = [u for u, (c, _) in content_map.items() if not c or not c.strip()]
    if failed_urls:
        with ThreadPoolExecutor(max_workers=settings.max_concurrent_requests) as pool:
            futures = {pool.submit(client.extract, [u]): u for u in failed_urls}
            for fut in tqdm(as_completed(futures), total=len(futures), desc="Retrying failed extractions", unit="url"):
                content_map.update(fut.result())

    for url, (full_content, extracted_title) in content_map.items():
        for art in url_to_articles.get(url, []):
            # A date decoded straight from the URL (e.g. BusinessWire's
            # YYYYMMDD path) is checked even when extraction failed --
            # real feedback showed wire-service staleness is common enough
            # that this is worth doing independently of full-text content.
            url_date = _date_from_url(url)
            if url_date is not None and not (start_dt <= url_date <= end_dt):
                to_drop.add(id(art))
                continue

            # The page's OWN title (from full extraction) is often more
            # accurate than the short search excerpt's -- re-checking catches
            # a homepage/section-front/listing page the initial check missed
            # (reported in practice as "source link goes to a homepage").
            if extracted_title and is_generic_page(url, extracted_title, art.source_name):
                to_drop.add(id(art))
                continue

            if not full_content or not full_content.strip():
                # Full-text extraction failed -- at least clean the
                # short fallback excerpt of any nav/boilerplate junk
                # it may carry, rather than leaving it raw.
                art.summary_en = clean_short_summary(art.summary_en)
                art.review_flag = _append_flag(art.review_flag, "Full article unavailable - short summary only")
                continue

            # Datelines are conventionally near the top of a press
            # release/article; only sniff the first ~800 chars so an
            # unrelated date mentioned deep in the body (e.g. "since
            # its 2016 founding") can't cause a false stale-drop.
            sniffed = _sniff_date(full_content[:800])
            if sniffed is not None:
                if sniffed.tzinfo is not None:
                    sniffed = sniffed.replace(tzinfo=None)
                if not (start_dt <= sniffed <= end_dt):
                    to_drop.add(id(art))
                    continue

            translated, translate_ok = translate_to_english(full_content)
            if not translate_ok:
                art.review_flag = _append_flag(art.review_flag, "Translation uncertain")

            # A short excerpt can misleadingly show the company name
            # near the top while the full article is actually about
            # something else (seen in practice: a passing mention in
            # an unrelated local-news digest). Re-check against the
            # full text before trusting it enough to summarize.
            if not is_prominent_in_full_text(art.company, art.headline_en, translated):
                to_drop.add(id(art))
                continue

            art.summary_en = summarize(
                translated, focus_terms=[art.company], target_words=450, max_words=500
            )

    if to_drop:
        print(f"  Dropped {len(to_drop)} article(s) after full-text review (stale date, a homepage/listing page, or not actually about the company).")
    return [a for a in articles if id(a) not in to_drop]


def parse_args():
    ap = argparse.ArgumentParser(description="EdTech News Tracker (budget-capped)")
    ap.add_argument("--date", help="single reporting date YYYY-MM-DD (skips the prompt)")
    ap.add_argument("--start", help="window start YYYY-MM-DD")
    ap.add_argument("--end", help="window end YYYY-MM-DD")
    ap.add_argument("--budget", type=float, help="max USD of Parallel.ai spend for this run (default 25)")
    ap.add_argument("--geo-batch", help="countries per search call: integer or 'auto' (default)")
    ap.add_argument("--dry-run", action="store_true", help="print the cost/coverage plan without calling the API")
    return ap.parse_args()


def main() -> int:
    print("=" * 70)
    print("EdTech News Tracker")
    print("=" * 70)

    args = parse_args()
    settings = load_settings(require_key=not args.dry_run)
    if args.budget is not None:
        settings.budget_usd = args.budget
    if args.geo_batch:
        settings.geo_batch_size = args.geo_batch

    try:
        companies, geographies, validation_messages = load_tracking_list(settings.excel_path)
    except TrackingListError as e:
        print(f"\n[ERROR] {e}")
        return 1

    print(f"\nLoaded {len(companies)} companies and {len(geographies)} geographies "
          f"from '{settings.excel_path.name}'.")
    if validation_messages:
        print("\nValidation notes:")
        for m in validation_messages:
            print(f"  - {m}")
    else:
        print("No validation issues found.")

    if args.date or args.start:
        try:
            start_dt = datetime.strptime(args.start or args.date, DATE_FMT)
            end_dt = datetime.strptime(args.end or args.date or args.start, DATE_FMT) + timedelta(hours=23, minutes=59, seconds=59)
        except ValueError:
            print(f"Dates must be {DATE_FMT}.")
            return 1
    else:
        start_dt, end_dt = prompt_date_range()
    start_tag, end_tag = start_dt.strftime(DATE_FMT), end_dt.strftime(DATE_FMT)

    completed_companies, articles = checkpoint.load(settings.checkpoint_dir, start_tag, end_tag)
    if completed_companies:
        print(f"\nResuming previous run: {len(completed_companies)} company/companies already completed.")

    start_s, end_s = start_tag, end_tag
    budget = BudgetTracker(settings.budget_usd, settings.checkpoint_dir / f"spend_{start_tag}_to_{end_tag}.json")
    hold_back = settings.budget_usd * (settings.extract_reserve_fraction + settings.safety_margin_fraction)

    core_todo = [c for c in companies if unit_key("core", c) not in completed_companies]
    search_budget = settings.budget_usd - hold_back - budget.spent
    core_cost = len(core_todo) * 2 * settings.search_cost_usd
    geo_calls_available = max(0, int((search_budget - core_cost) / settings.search_cost_usd))
    done_geo = sum(1 for k in completed_companies if k.startswith("geo::"))
    batch_size = choose_batch_size(settings.geo_batch_size, geographies, len(companies),
                                   geo_calls_available + done_geo, settings.geo_batch_max,
                                   settings.geo_priority_countries)
    groups = group_geographies(geographies, batch_size, settings.geo_priority_countries)
    geo_units_all = plan_geo_units(companies, groups)
    geo_todo = [(c, gi) for c, gi in geo_units_all if unit_key("geo", c, gi) not in completed_companies]
    planned_geo = min(len(geo_todo), geo_calls_available)
    total_cost = core_cost + planned_geo * settings.search_cost_usd

    print(f"\nBudget plan (cap ${settings.budget_usd:.2f}, ~${settings.search_cost_usd}/search, "
          f"~${settings.extract_cost_usd}/URL extract):")
    print(f"  Core searches (official + global) : {len(core_todo) * 2}  (~${core_cost:.2f})")
    print(f"  Country groups                    : {len(groups)} groups of up to {batch_size} countries "
          f"-> {len(geo_todo)} calls needed for full coverage")
    print(f"  Country calls affordable          : {planned_geo}  (~${planned_geo * settings.search_cost_usd:.2f})")
    print(f"  Held back for extraction + margin : ~${hold_back:.2f}")
    print(f"  Estimated search spend            : ~${total_cost:.2f}")
    if planned_geo < len(geo_todo):
        print(f"  NOTE: budget covers {planned_geo}/{len(geo_todo)} company-country-group calls; priority "
              f"countries first, spread evenly across companies. Raise BUDGET_USD or GEO_BATCH_MAX for more.")
    if args.dry_run:
        print("\nDry run only -- no API calls made.")
        return 0

    client = ParallelClient(
        api_key=settings.api_key,
        max_concurrent=settings.max_concurrent_requests,
        max_retries=settings.max_retries,
        backoff_base=settings.backoff_base_seconds,
        mode=settings.parallel_mode,
        max_results=settings.max_results_per_query,
        budget=budget,
        search_cost=settings.search_cost_usd,
        extract_cost=settings.extract_cost_usd,
    )
    client.search_floor_remaining = hold_back

    run_start_time = time.time()
    zero_news_companies: list[str] = []
    s_str, e_str = start_dt.strftime(DATE_FMT), end_dt.strftime(DATE_FMT)

    def core_unit(c):
        if not client.can_search():
            return None
        return run_tasks([build_official_press_task(c, s_str, e_str), build_global_broad_task(c, s_str, e_str)],
                         client, settings, geographies, start_dt, end_dt)

    def geo_unit(c, gi):
        if not client.can_search():
            return None
        return run_tasks([build_geography_group_task(c, groups[gi], s_str, e_str)],
                         client, settings, geographies, start_dt, end_dt)

    try:
        _run_units([(unit_key("core", c), c.name, (c,)) for c in core_todo], core_unit, client,
                   completed_companies, articles, settings, start_tag, end_tag, "Core searches", zero_news_companies)
        # Priority 2: hit companies/countries that already showed activity first? Round-robin keeps it even.
        geo_units = [(unit_key("geo", c, gi), c.name, (c, gi)) for c, gi in geo_todo[:planned_geo]]
        _run_units(geo_units, geo_unit, client, completed_companies, articles, settings, start_tag, end_tag,
                   "Country searches")
    except KeyboardInterrupt:
        print("\n\nInterrupted. Progress has been saved -- rerun with the same date range to resume.")
        return 130

    # Companies whose core lanes AND all country calls found nothing
    found_companies = {a.company for a in articles}
    zero_news_companies = [c.name for c in companies if c.name not in found_companies]

    print("\nRetrying any failed queries once more before finalizing...")
    still_failed = client.retry_failed()

    print("Deduplicating articles across sources and languages...")
    deduped = deduplicate(articles)

    print(f"Fetching full article text for {len(deduped)} articles and writing summaries...")
    deduped = enrich_with_full_summaries(deduped, client, settings, start_dt, end_dt)

    run_log = RunLog(
        start_date=start_tag,
        end_date=end_tag,
        companies_searched=[c.name for c in companies],
        geographies_searched=[g.country for g in geographies],
        articles_found=len(deduped),
        companies_with_zero_news=zero_news_companies,
        failed_queries=[f"{fq.company_name} | {fq.lane} | {fq.geography or 'Global'} | {fq.error}" for fq in still_failed],
        duration_seconds=time.time() - run_start_time,
    )

    out_path = export(deduped, run_log, settings.output_dir)
    checkpoint.clear(settings.checkpoint_dir, start_tag, end_tag)

    print("\n" + "=" * 70)
    print(f"Done. {len(deduped)} articles written to: {out_path}")
    print(f"Companies with zero news found: {len(zero_news_companies)}")
    print(f"Failed queries (after retries): {len(still_failed)}")
    print(f"Run duration: {run_log.duration_seconds:.1f}s")
    print(f"Spend: {budget.summary()}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
