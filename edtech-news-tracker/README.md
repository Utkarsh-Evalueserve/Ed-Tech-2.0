# EdTech News Tracker

A daily news-monitoring tool that tracks ~280 Ed-Tech competitor companies
across 82 geographies, searching company websites, press-release wires, and
local-language news sources, then produces one Excel report per run.

This guide assumes no prior coding experience. Follow the steps in order.

## 1. One-time setup

### 1.1 Install Python
You need Python 3.10 or newer. If you're not sure whether you have it,
open a terminal in VS Code (`Terminal` menu → `New Terminal`) and run:
```
python3 --version
```
If that fails, install Python from https://www.python.org/downloads/.

### 1.2 Open this folder in VS Code
`File` → `Open Folder...` → select the `edtech-news-tracker` folder.

### 1.3 Install the required packages
In the VS Code terminal, from inside this folder, run:
```
pip install -r requirements.txt
```

### 1.4 Add your Parallel.ai API key
1. Find the file `.env.example` in this folder.
2. Make a copy of it named exactly `.env` (still in this same folder).
3. Open `.env` and replace `your_parallel_api_key_here` with your real
   Parallel.ai API key.
4. Save the file. `.env` is already excluded from version control
   (see `.gitignore`), so your key will never be committed or shared.

   If you skip this step, the script will simply ask you to paste the key
   the first time you run it (the input is hidden as you type).

## 2. Keep your tracking list up to date

Open `edtech_tracking_list.xlsx`. It has two tabs:

- **Companies**: `Company Name`, `Aliases`, `Official Website`, `HQ Country`, `Notes`, `Category Code`
  - Add a row to start tracking a new company. Delete a row to stop.
  - Fill in `Aliases` with any past/other brand names, comma-separated
    (e.g. `IDP IELTS, IDP Education`) -- this makes searches far more
    accurate.
  - Filling in `Official Website` and `HQ Country` is optional but strongly
    recommended: it helps the tool tell apart companies that share a common
    name from unrelated businesses, and also controls one specific
    behavior -- an article with no confirmed publish date is only kept
    when it's on the company's *own* verified website (a press-wire hit
    with no date is dropped rather than assumed current).
  - `Notes` is a free-text field for disambiguation guidance, sent directly
    into the search instructions for that company -- e.g. `Apollo`'s notes
    say "Apollo Education Group ONLY (operates University of Phoenix). NOT
    Apollo private equity, Apollo Global Management, or Apollo Hospitals."
    This is the single highest-impact field for a company whose name
    collides with something else: a plain-language "NOT X, NOT Y"
    instruction here is followed far more reliably than any keyword filter
    could manage after the fact. 50 companies already have notes merged in
    from a master company list; add more for any company you see pulling
    in wrong-entity results.
  - `Category Code` (A-H, from the same master company list) controls
    whether a company gets the full, expensive 82-country search or just
    the 2 cheap global/official searches -- see the cost section below.
    Leave it blank for a new company and it defaults to getting the full
    sweep (coverage is never silently reduced just because a company isn't
    categorized yet).
- **Geographies**: `Country`, `Primary Language(s)`, `Local News Sources`
  - `Primary Language(s)` is pre-filled with a sensible default per country
    -- edit it if a market needs an additional language.
  - `Local News Sources` is optional: add domain names (e.g.
    `timesofindia.com`) to bias the search toward specific local outlets.

Nothing about companies or geographies is hard-coded in the script -- every
run reads this file fresh, so editing it is the only thing you need to do to
change what gets tracked.

> **Note on the starter file included in this project:** it was generated
> from your uploaded `Edtech_Complete_list_of_Competitor__Geography.xlsx`
> (280 companies, 82 countries). That source file listed companies and
> countries as two separate columns, not matched row-by-row, so they have
> been split into their own sheets here. Aliases, websites and HQ country
> were not present in the source and are left blank for your team to fill
> in over time -- the tool works without them, just with lower precision on
> companies that share a name with something else.

## 3. Run the tracker

In the VS Code terminal:
```
python edtech_news_tracker.py
```

You'll be asked for a date range:
```
Start date [Enter = last 24h]: 2026-09-01
End date   [Enter = now]:       2026-09-30
```
Press Enter on both (leave them blank) to default to the last 24 hours --
this is what you'd normally use for the daily run.

A progress bar will show companies being processed. Each company gets its
own newsroom/press-wire search and a broad international search; on top of
that, companies in the full-sweep Category Code tiers (`A`/`B`/`C` by
default -- English/Language Learning, Assessment/Exam, EdTech/Publishing --
see `FULL_SWEEP_CATEGORIES` in `.env.example`) also get a dedicated search
for *each* tracked geography, so a company's local activity in any one of
the 82 countries gets a real chance to surface. This tiering exists because
a full 82-country sweep for every company turned out to be expensive in
practice (a real run cost roughly $500 in Parallel.ai credits) for a
large number of searches that found nothing -- most companies only ever
generate news in a handful of countries, if any, on a given day. Limiting
the expensive sweep to the company categories where local/regional news is
most central to the business cuts total search calls by roughly a third
without reducing coverage for the companies most likely to need it; a
company with no Category Code at all still gets the full sweep, so an
untagged company is never silently under-covered. Companies outside the
full-sweep tiers still get the 2 cheap searches, which catch most
significant news (funding, leadership, M&A, major product launches)
regardless of geography.

A full run with the default tiering is on the order of **1-2 hours** and
noticeably cheaper than an untiered run; `MAX_CONCURRENT_REQUESTS` in
`.env` controls speed (not cost) if your Parallel.ai plan's rate limit
allows raising it. To cut cost further: narrow `FULL_SWEEP_CATEGORIES` to
fewer letters, or lower `GEOGRAPHY_MAX_RESULTS_PER_QUERY`. To widen
coverage back toward "every company, every country" at higher cost, add
more letters (or all of A-H) to `FULL_SWEEP_CATEGORIES`.

### If it gets interrupted
Just run `python edtech_news_tracker.py` again with the **same date range**.
It automatically resumes from the last company it fully finished, instead of
starting over.

## 4. Find your results

Each run produces a file named:
```
output/EdTech_News_<start-date>_to_<end-date>.xlsx
```
It has two tabs:
- **News**: one row per article, with company, geography, category, the
  article title, a ~400-500 word English summary covering the article's
  crux, source name, publication date, and the source link, plus a
  relevance score (0-1) and a review flag. Titles and summaries are
  translated to English automatically when the source article is in another
  language. The tool only keeps results that are genuinely *about* the
  company (not generic profile/directory pages like LinkedIn or Wikipedia,
  and not articles where the name is just mentioned in passing alongside
  others) and that match a real news category. Among those, anything still
  uncertain is kept and flagged (`Low confidence`, `Date unverified`,
  `Full article unavailable - short summary only`, etc.) rather than
  silently dropped, so a human can take a quick look rather than the tool
  guessing wrong.
- **Run Log**: how many companies/geographies were searched, articles
  found, which companies had zero news that day, any queries that failed
  even after retries, and total run time.

## 5. Running it daily
Just repeat step 3 each day, leaving both date fields blank so it defaults
to the last 24 hours. Consider setting a recurring reminder or a scheduled
task (Windows Task Scheduler / macOS `cron`/`launchd`) to run the command
automatically if you want it fully hands-off.

## How it works (for reference)

The code is split into small, single-purpose modules under `tracker/`:
- `config.py` -- settings, API key loading, category list.
- `excel_loader.py` -- reads and validates `edtech_tracking_list.xlsx`.
- `query_builder.py` -- builds, per company: its own newsroom/press-wire
  search, a broad international search (catches a story picked up or
  translated by a foreign outlet), and -- for companies in a full-sweep
  Category Code tier -- one dedicated search *per tracked geography*, the
  fix for local/regional recall after a shared, all-countries-in-one-call
  design kept missing local partnerships, conferences and awards covered
  only by a country's own press. A company's `Notes` (disambiguation
  guidance) are appended to every search's instructions.
- `edtech_news_tracker.run_company` -- decides whether a company qualifies
  for the full geography sweep (its `Category Code` is in
  `FULL_SWEEP_CATEGORIES`, or it has none) before calling `query_builder`,
  which is the main cost control (see the cost section above).
- `parallel_client.py` -- talks to the Parallel.ai Search and Extract APIs,
  with retry/backoff and a concurrency limit; failed queries are logged and
  retried once more at the end of the run. A failed full-text extraction is
  also retried once on its own (outside a batch), which recovers a
  meaningful share of transient per-URL failures.
- **Dates**: every search objective now states the exact required date
  range explicitly (not just "recent"). An undated result is only kept
  when it's on the company's own verified website (`Official Website` in
  the Excel) -- a press-wire hit with no date is dropped rather than
  assumed current, since real runs showed wire archives (BusinessWire,
  PR Newswire) resurfacing years-old releases with no date metadata often
  enough to be the top complaint. BusinessWire's URLs encode the exact
  publish date in the path itself, which is decoded and checked directly
  wherever present -- the single most reliable date signal available.
- `translator.py` -- translates non-English titles/article text to English.
- `relevance.py` -- drops generic profile/directory pages (LinkedIn,
  Wikipedia, Crunchbase, "About Us" pages) and passing mentions where the
  company isn't actually the subject, scores the rest for relevance,
  disambiguates similarly-named companies, and classifies each surviving
  article into a news category.
- `dedup.py` -- merges duplicate/near-duplicate articles across sources
  (after which each surviving URL's full text is fetched once -- see below
  -- rather than once per raw search hit).
- `summarizer.py` -- after dedup, the full article text is fetched (via
  Parallel's Extract API) and translated if needed, then this picks the
  most representative actual sentences from it (not AI-generated text,
  to avoid misstating facts) to build a ~400-500 word summary capturing
  the article's crux.
- `exporter.py` -- writes the final Excel report.
- `checkpoint.py` -- saves progress so an interrupted run can resume.

`edtech_news_tracker.py` is the single entry point that ties these together.

## Troubleshooting
- **"Tracking list not found"**: make sure `edtech_tracking_list.xlsx` is in
  this same folder and hasn't been renamed.
- **"missing a 'Company Name' column"**: a sheet's header row doesn't match
  the expected column names -- check `edtech_tracking_list.xlsx` against
  section 2 above.
- **Rate limit / repeated failures**: lower `MAX_CONCURRENT_REQUESTS` in
  `.env` (see `.env.example`), or check your Parallel.ai plan's limits.
- **Translation looks off**: `deep-translator` uses a free Google Translate
  backend and occasionally struggles with idioms; these rows are flagged
  `Translation uncertain` in the News sheet for a quick manual check.

## Budget cap ($25 default)

Every Parallel.ai call is checked against a hard spend cap before it is made
(`tracker/budget.py`); the running total persists in `.state/`, so a resumed
run never exceeds it.

```
python edtech_news_tracker.py --date 2026-11-10 --dry-run   # plan + cost estimate, no API calls
python edtech_news_tracker.py --date 2026-11-10             # real run, capped at $25
python edtech_news_tracker.py --date 2026-11-10 --budget 40 # different cap
```

How coverage is kept high within the cap:
1. Official/press + global searches run for **every** company.
2. Countries are grouped by shared language and searched in one call per
   (company, group) -- a call costs the same however many countries it names.
   `GEO_BATCH_SIZE=auto` picks the smallest group size that covers **all
   companies x all countries** within budget (about 7 countries/call at $25).
3. If the budget still can't cover everything, priority countries run first,
   spread round-robin across companies; unfinished units resume on rerun.
4. ~12% of the budget is reserved for full-text extraction (best articles first).

Costs are **estimates** ($0.005/search, $0.001/URL by default). Check your
Parallel.ai pricing and set `SEARCH_COST_USD` / `EXTRACT_COST_USD` if different.

## Relevance & recall tuning (Nov 2026)
- Server-side date filter (`source_policy.after_date`) and noise-domain exclusion on every search.
- Precision gate (`tracker/gate.py`): drops listing/events/tender/archive pages, law-firm investor alerts,
  entities named after "NOT" in the Notes column, and off-topic news about NGO/adjacent-corporate
  companies (Category F/H, or Notes containing ONLY/NOT) that lacks education context. Results below
  `MIN_RELEVANCE` (default 0.5) are dropped. Set `EDU_CONTEXT_COMPANIES=Criteria,Orijin,...` for more.
- Recall: stories with no stock keyword are kept as "Other Relevant News" instead of dropped; broader
  category keywords (forums, events, recognitions, studies, curricula, grants...); a deep-dive lane
  (3 topic searches) for high-volume companies whose core searches came back full (e.g. OpenAI).
- Companies not on the tracking list (e.g. UNESCO) are never searched - add them to the Companies sheet.
