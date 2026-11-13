"""
Free, key-less multi-source news search (Google News + Bing News), with an
optional paid Google-results API (Serper.dev) -- a drop-in replacement for
ParallelClient that gives far more complete coverage.

Why this exists: Parallel's Search API is an LLM-driven *web* search that
returns ~10 curated pages per call, so it routinely misses stories. Google
News is a real news index: one query returns up to 100 dated headlines, and
each country has its own local edition (language + local outlets), which is
exactly what "every company in every country" needs.

Sources
-------
* Google News RSS   (always on)  news.google.com/rss/search -- per-country
  edition (hl/gl/ceid) so local-language outlets surface. Free, no API key.
* Bing News RSS     (default on) bing.com/news/search?format=rss -- a second,
  independent index; catches stories Google News ranks out. Free, no key.
* Serper.dev        (only if SERPER_API_KEY is set) -- Google News results via
  a proper API: real article URLs + snippets, no redirect decoding, higher
  rate limits. Recommended for production scale.

Completeness mechanics
----------------------
* A query that returns the source's cap (100 for Google RSS) is assumed
  truncated and is automatically split -- by halving the date window, or for
  a single day by topic keyword sets -- until nothing is cut off.
* Google News links are redirect URLs; they are decoded to the real
  publisher URL (after dedup, so each story is resolved once).
* Full article text is fetched directly from the publisher (trafilatura if
  installed, BeautifulSoup otherwise) for the 400-500 word summaries.

Nothing here raises: a failed request is recorded and retried at the end of
the run, exactly like the Parallel client.
"""
from __future__ import annotations

import base64
import html
import random
import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote_plus, urlsplit

import requests

from .excel_loader import Company, Geography
from .parallel_client import FailedQuery, RawResult

GOOGLE_CAP = 100  # Google News RSS returns at most 100 items per query
BING_CAP = 100

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Country -> Google News edition. Keys are normalised (lowercase, no spaces) so
# the spreadsheet's "CzechRepublic"/"Czech Republic"/"HongKong" all match.
# value = (display name, ISO country, Google hl language, edition supported?)
# Where a country has no real Google News edition, `supported` is False and
# the query falls back to the US-English edition with the country name added
# to the query -- so every country is still searched, just less locally.
# ---------------------------------------------------------------------------
_E = {
    "albania": ("Albania", "AL", "sq", False),
    "algeria": ("Algeria", "DZ", "fr", True),
    "argentina": ("Argentina", "AR", "es-419", True),
    "australia": ("Australia", "AU", "en", True),
    "austria": ("Austria", "AT", "de", True),
    "azerbaijan": ("Azerbaijan", "AZ", "az", False),
    "bahrain": ("Bahrain", "BH", "ar", False),
    "brazil": ("Brazil", "BR", "pt-419", True),
    "brunei": ("Brunei", "BN", "ms", False),
    "bulgaria": ("Bulgaria", "BG", "bg", True),
    "cambodia": ("Cambodia", "KH", "km", False),
    "canada": ("Canada", "CA", "en", True),
    "catalonia": ("Catalonia", "ES", "ca", True),
    "chile": ("Chile", "CL", "es-419", True),
    "china": ("China", "CN", "zh-CN", False),
    "colombia": ("Colombia", "CO", "es-419", True),
    "croatia": ("Croatia", "HR", "hr", True),
    "cyprus": ("Cyprus", "CY", "el", False),
    "czechrepublic": ("Czech Republic", "CZ", "cs", True),
    "czechia": ("Czech Republic", "CZ", "cs", True),
    "ecuador": ("Ecuador", "EC", "es-419", True),
    "egypt": ("Egypt", "EG", "ar", True),
    "estonia": ("Estonia", "EE", "et", True),
    "france": ("France", "FR", "fr", True),
    "georgia": ("Georgia", "GE", "ka", False),
    "germany": ("Germany", "DE", "de", True),
    "greece": ("Greece", "GR", "el", True),
    "hongkong": ("Hong Kong", "HK", "zh-HK", True),
    "hungary": ("Hungary", "HU", "hu", True),
    "india": ("India", "IN", "en", True),
    "indonesia": ("Indonesia", "ID", "id", True),
    "iran": ("Iran", "IR", "fa", False),
    "iraq": ("Iraq", "IQ", "ar", False),
    "ireland": ("Ireland", "IE", "en", True),
    "israel": ("Israel", "IL", "he", True),
    "italy": ("Italy", "IT", "it", True),
    "japan": ("Japan", "JP", "ja", True),
    "jordan": ("Jordan", "JO", "ar", False),
    "kazakhstan": ("Kazakhstan", "KZ", "ru", False),
    "korea": ("South Korea", "KR", "ko", True),
    "southkorea": ("South Korea", "KR", "ko", True),
    "kuwait": ("Kuwait", "KW", "ar", False),
    "lebanon": ("Lebanon", "LB", "ar", True),
    "libya": ("Libya", "LY", "ar", False),
    "malaysia": ("Malaysia", "MY", "en", True),
    "mauritius": ("Mauritius", "MU", "en", False),
    "mexico": ("Mexico", "MX", "es-419", True),
    "montenegro": ("Montenegro", "ME", "sr", False),
    "morocco": ("Morocco", "MA", "fr", True),
    "myanmar": ("Myanmar", "MM", "my", False),
    "nepal": ("Nepal", "NP", "ne", False),
    "netherlands": ("Netherlands", "NL", "nl", True),
    "newzealand": ("New Zealand", "NZ", "en", True),
    "nigeria": ("Nigeria", "NG", "en", True),
    "northmacedonia": ("North Macedonia", "MK", "mk", False),
    "oman": ("Oman", "OM", "ar", False),
    "pakistan": ("Pakistan", "PK", "en", True),
    "palestine": ("Palestine", "PS", "ar", False),
    "panama": ("Panama", "PA", "es-419", False),
    "peru": ("Peru", "PE", "es-419", True),
    "philippines": ("Philippines", "PH", "en", True),
    "poland": ("Poland", "PL", "pl", True),
    "portugal": ("Portugal", "PT", "pt-150", True),
    "qatar": ("Qatar", "QA", "ar", False),
    "romania": ("Romania", "RO", "ro", True),
    "russia": ("Russia", "RU", "ru", True),
    "saudiarabia": ("Saudi Arabia", "SA", "ar", True),
    "singapore": ("Singapore", "SG", "en", True),
    "slovakia": ("Slovakia", "SK", "sk", True),
    "southafrica": ("South Africa", "ZA", "en", True),
    "spain": ("Spain", "ES", "es", True),
    "srilanka": ("Sri Lanka", "LK", "si", False),
    "sudan": ("Sudan", "SD", "ar", False),
    "taiwan": ("Taiwan", "TW", "zh-TW", True),
    "thailand": ("Thailand", "TH", "th", True),
    "tunisia": ("Tunisia", "TN", "fr", False),
    "turkey": ("Turkey", "TR", "tr", True),
    "uae": ("United Arab Emirates", "AE", "ar", True),
    "unitedarabemirates": ("United Arab Emirates", "AE", "ar", True),
    "uk": ("United Kingdom", "GB", "en", True),
    "unitedkingdom": ("United Kingdom", "GB", "en", True),
    "us": ("United States", "US", "en", True),
    "usa": ("United States", "US", "en", True),
    "unitedstates": ("United States", "US", "en", True),
    "ukraine": ("Ukraine", "UA", "uk", True),
    "uruguay": ("Uruguay", "UY", "es-419", False),
    "uzbekistan": ("Uzbekistan", "UZ", "uz", False),
    "vietnam": ("Vietnam", "VN", "vi", True),
}


def _norm(country: str) -> str:
    return re.sub(r"[^a-z]", "", (country or "").lower())


def edition_for(country: str) -> Tuple[str, str, str, bool]:
    """(display_name, ISO, hl_language, supported) for a spreadsheet country."""
    return _E.get(_norm(country), (country, "US", "en", False))


_EDITION_SPECIAL = {  # ISO -> (hl, ceid language) where it isn't simply "<lang>-<ISO>" / "<ISO>:<lang>"
    "BR": ("pt-BR", "pt-419"), "PT": ("pt-PT", "pt-150"), "TW": ("zh-TW", "zh-Hant"),
    "HK": ("zh-HK", "zh-Hant"), "CN": ("zh-CN", "zh-Hans"),
}


def google_edition_params(country: str) -> Tuple[str, str, str, str]:
    """(display_name, hl, gl, ceid) for a country's Google News edition. A
    country with no real edition falls back to the US-English one."""
    name, iso, lang, supported = edition_for(country)
    if not supported:
        return name, "en-US", "US", "US:en"
    if iso in _EDITION_SPECIAL:
        hl, ceid_lang = _EDITION_SPECIAL[iso]
    elif lang == "es-419":
        hl, ceid_lang = "es-419", "es-419"
    else:
        hl, ceid_lang = (lang if "-" in lang else f"{lang}-{iso}"), lang
    return name, hl, iso, f"{iso}:{ceid_lang}"


# ---------------------------------------------------------------------------
# Query construction
# ---------------------------------------------------------------------------
_TOPIC_SPLITS = [
    "partnership OR agreement OR MOU OR collaboration OR funding OR investment OR acquisition OR grant",
    "launch OR launches OR unveils OR new OR introduces OR programme OR course OR AI",
    "appoints OR appointed OR CEO OR award OR wins OR ranked OR conference OR event OR forum",
    "lawsuit OR breach OR regulation OR government OR ministry OR layoffs OR results OR complaint",
]


def _name_clause(company: Company) -> str:
    names = []
    for n in company.all_names():
        n = (n or "").strip()
        # one/two-letter aliases ("ABA", "PR") are pure noise as a bare query term
        if len(n) >= 3 and n.lower() not in {x.lower() for x in names}:
            names.append(n)
    if not names:
        names = [company.name]
    quoted = " OR ".join(f'"{n}"' for n in names[:6])
    return f"({quoted})" if len(names) > 1 else quoted


def _site(url: str) -> str:
    u = (url or "").replace("https://", "").replace("http://", "").replace("www.", "")
    return u.split("/")[0]


def _day(d: datetime) -> str:
    return d.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
_TAG_RE = re.compile(r"<[^>]+>")


def _clean_html(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", s or ""))).strip()


def _rfc_date(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is not None:  # normalise to naive UTC
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return s


def parse_google_rss(xml_text: str) -> List[RawResult]:
    try:
        root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except ET.ParseError:
        return []
    out: List[RawResult] = []
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        raw_title = _clean_html(item.findtext("title") or "")
        src = item.find("source")
        src_name = (src.text or "").strip() if src is not None else ""
        src_url = (src.get("url") if src is not None else "") or ""
        title = raw_title
        if src_name and title.endswith(f" - {src_name}"):
            title = title[: -len(src_name) - 3].strip()
        domain = _site(src_url) or _site(link)
        desc = _clean_html(item.findtext("description") or "")
        # Google's description is just "<title> <source>"; not a real snippet.
        excerpt = title if (not desc or desc.startswith(title[:30])) else desc
        if link and title:
            out.append(RawResult(link, title, excerpt, domain, _rfc_date(item.findtext("pubDate"))))
    return out


def parse_bing_rss(xml_text: str) -> List[RawResult]:
    try:
        root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except ET.ParseError:
        return []
    out: List[RawResult] = []
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        qs = parse_qs(urlsplit(link).query)
        real = qs.get("url", [None])[0]
        if real:
            link = real
        title = _clean_html(item.findtext("title") or "")
        desc = _clean_html(item.findtext("description") or "")
        src_name = ""
        for child in item:  # <News:Source>Name</News:Source>
            if child.tag.endswith("Source"):
                src_name = (child.text or "").strip()
        excerpt = f"{title}. {desc}".strip() if desc else title
        if link and title:
            out.append(RawResult(link, title, excerpt, _site(link), _rfc_date(item.findtext("pubDate"))))
    return out


_REL_RE = re.compile(r"(\d+)\s*(minute|hour|day|week|month|year)s?\s+ago", re.I)


def _serper_date(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    m = _REL_RE.search(s)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        days = {"minute": 0, "hour": 0, "day": n, "week": 7 * n, "month": 30 * n, "year": 365 * n}[unit]
        return _day(datetime.now() - timedelta(days=days))
    return s


# ---------------------------------------------------------------------------
# Google News redirect decoding
# ---------------------------------------------------------------------------
_URL_IN_BYTES = re.compile(rb"https?://[^\s\x00-\x1f\"<>\\^`{|}\x80-\xff]+")


def _decode_offline(link: str) -> Optional[str]:
    """Older-format Google News ids embed the URL in base64: no request needed."""
    try:
        gid = urlsplit(link).path.rstrip("/").split("/")[-1]
        raw = base64.urlsafe_b64decode(gid + "=" * (-len(gid) % 4))
    except Exception:  # noqa: BLE001
        return None
    m = _URL_IN_BYTES.search(raw)
    return m.group(0).decode("utf-8", "ignore") if m else None


def _is_google_link(url: str) -> bool:
    return "news.google.com/" in (url or "") and "/articles/" in (url or "")


class RateLimiter:
    """Global requests-per-second throttle shared by all worker threads."""

    def __init__(self, rps: float):
        self.interval = 1.0 / max(rps, 0.05)
        self._lock = threading.Lock()
        self._next = 0.0
        self.penalty_until = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next, self.penalty_until)
            self._next = start + self.interval
        delay = start - now
        if delay > 0:
            time.sleep(delay)

    def back_off(self, seconds: float) -> None:
        with self._lock:
            self.penalty_until = max(self.penalty_until, time.monotonic() + seconds)


@dataclass
class _Failed:
    fq: FailedQuery
    redo: Callable[[], Optional[List[RawResult]]]


class NewsSearchClient:
    """Same surface the main script uses on ParallelClient, but backed by
    Google News / Bing News / Serper."""

    is_free_provider = True

    def __init__(
        self,
        serper_key: str = "",
        use_bing: bool = True,
        bing_per_country: bool = False,
        requests_per_second: float = 4.0,
        max_retries: int = 4,
        backoff_base: float = 2.0,
        fetch_timeout: int = 20,
        session: Optional[requests.Session] = None,
    ):
        self.serper_key = serper_key
        self.use_bing = use_bing
        self.bing_per_country = bing_per_country
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.fetch_timeout = fetch_timeout
        self._http = session or requests.Session()
        self._http.headers.update({"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.8"})
        self._limiter = RateLimiter(requests_per_second)
        # page fetches (publisher sites) are spread across many hosts: lighter throttle
        self._page_limiter = RateLimiter(max(requests_per_second * 3, 8))
        self._lock = threading.Lock()
        self.failed_queries: List[FailedQuery] = []
        self._failed: List[_Failed] = []
        self.budget = None
        self.search_floor_remaining = 0.0
        self.request_count = 0
        self.truncated_splits = 0

    # -- main-script compatibility -------------------------------------
    def can_search(self) -> bool:
        return True

    # -- low-level GET with retry/backoff ------------------------------
    def _get(self, url: str, limiter: Optional[RateLimiter] = None, **kw) -> Optional[requests.Response]:
        limiter = limiter or self._limiter
        last = "unknown error"
        for attempt in range(self.max_retries + 1):
            limiter.wait()
            try:
                with self._lock:
                    self.request_count += 1
                r = self._http.get(url, timeout=self.fetch_timeout, **kw)
                if r.status_code == 200:
                    return r
                last = f"HTTP {r.status_code}"
                if r.status_code in (429, 503):
                    # Google throttles by IP: slow EVERYONE down, not just this thread.
                    limiter.back_off(self.backoff_base * (2**attempt) * 2 + random.uniform(0, 2))
                elif r.status_code in (400, 403, 404, 410):
                    break
            except requests.RequestException as exc:
                last = str(exc)
            if attempt < self.max_retries:
                time.sleep(self.backoff_base * (2**attempt) + random.uniform(0, 1))
        self._last_error = last
        return None

    # -- per-source single-page fetchers (return None on hard failure) ---
    def _google_once(self, q: str, hl: str, gl: str, ceid: str) -> Optional[List[RawResult]]:
        url = f"https://news.google.com/rss/search?q={quote_plus(q)}&hl={hl}&gl={gl}&ceid={ceid}"
        r = self._get(url)
        return None if r is None else parse_google_rss(r.text)

    def _bing_once(self, q: str, market: str, start: datetime, end: datetime) -> Optional[List[RawResult]]:
        age_days = (datetime.now() - start).days
        interval = ""
        if age_days <= 1:
            interval = '&qft=interval%3d"7"'   # past 24 hours
        elif age_days <= 7:
            interval = '&qft=interval%3d"8"'   # past week
        elif age_days <= 31:
            interval = '&qft=interval%3d"9"'   # past month
        url = f"https://www.bing.com/news/search?q={quote_plus(q)}&format=rss&setmkt={market}{interval}"
        r = self._get(url)
        return None if r is None else parse_bing_rss(r.text)

    def _serper_once(self, q: str, hl: str, gl: str, start: datetime, end: datetime) -> Optional[List[RawResult]]:
        body = {
            "q": q, "gl": gl.lower(), "hl": hl.split("-")[0], "num": 100,
            "tbs": f"cdr:1,cd_min:{start.strftime('%m/%d/%Y')},cd_max:{end.strftime('%m/%d/%Y')}",
        }
        last = "unknown"
        for attempt in range(self.max_retries + 1):
            self._limiter.wait()
            try:
                with self._lock:
                    self.request_count += 1
                r = self._http.post("https://google.serper.dev/news", json=body, timeout=self.fetch_timeout,
                                    headers={"X-API-KEY": self.serper_key})
                if r.status_code == 200:
                    out = []
                    for it in r.json().get("news", []):
                        link, title = it.get("link", ""), it.get("title", "")
                        if link and title:
                            out.append(RawResult(link, title, f"{title}. {it.get('snippet', '')}".strip(),
                                                 _site(link), _serper_date(it.get("date"))))
                    return out
                last = f"HTTP {r.status_code}"
                if r.status_code in (400, 401, 403):
                    break
            except (requests.RequestException, ValueError) as exc:
                last = str(exc)
            time.sleep(self.backoff_base * (2**attempt) + random.uniform(0, 1))
        self._last_error = last
        return None

    # -- completeness: split anything that hit the source's cap ----------
    def _fetch_all(self, fetch: Callable[[str, datetime, datetime], Optional[List[RawResult]]],
                   base_q: str, start: datetime, end: datetime, cap: int, depth: int = 0) -> Optional[List[RawResult]]:
        res = fetch(base_q, start, end)
        if res is None:
            return None
        if len(res) < cap - 2 or depth >= 4:
            return res
        self.truncated_splits += 1
        merged: Dict[str, RawResult] = {r.url: r for r in res}
        span = (end.date() - start.date()).days
        if span >= 1:  # halve the window
            mid = start + timedelta(days=span // 2)
            parts = [(start, mid), (mid + timedelta(days=1), end)]
            for s, e in parts:
                sub = self._fetch_all(fetch, base_q, s, e, cap, depth + 1)
                for r in sub or []:
                    merged.setdefault(r.url, r)
        else:  # single busy day: split by topic
            for topic in _TOPIC_SPLITS:
                sub = fetch(f"{base_q} ({topic})", start, end)
                for r in sub or []:
                    merged.setdefault(r.url, r)
        return list(merged.values())

    def _record_failure(self, task, label: str, redo) -> None:
        err = getattr(self, "_last_error", "unknown error")
        fq = FailedQuery(task.company.name, task.lane, label, label, err)
        with self._lock:
            self.failed_queries.append(fq)
            self._failed.append(_Failed(fq, redo))

    # -- the public search entry point ---------------------------------
    def search_task(self, task, start_dt: datetime, end_dt: datetime) -> List[RawResult]:
        results: Dict[str, RawResult] = {}
        for label, fn in self._jobs(task, start_dt, end_dt):
            out = fn()
            if out is None:
                self._record_failure(task, label, fn)
                continue
            for r in out:
                results.setdefault(r.url, r)
        return list(results.values())

    def _jobs(self, task, start_dt, end_dt):
        """Yields (label, callable) pairs for every search this task needs."""
        company = task.company
        # Google's after:/before: are exclusive-ish and tz-fuzzy: pad by a day;
        # exact windowing is re-applied downstream on the real publish date.
        s, e = start_dt - timedelta(days=1), end_dt + timedelta(days=1)
        names = _name_clause(company)
        jobs: List[Tuple[str, Callable[[], Optional[List[RawResult]]]]] = []

        def gq(q, hl, gl, ceid, label):
            def fetch(qq, a, b):
                return self._google_once(f"{qq} after:{_day(a)} before:{_day(b)}", hl, gl, ceid)
            jobs.append((label, lambda: self._fetch_all(fetch, q, s, e, GOOGLE_CAP)))

        def bq(q, market, label):
            def fetch(qq, a, b):
                return self._bing_once(qq, market, a, b)
            jobs.append((label, lambda: self._fetch_all(fetch, q, s, e, BING_CAP)))

        def sq(q, hl, gl, label):
            def fetch(qq, a, b):
                return self._serper_once(qq, hl, gl, a, b)
            jobs.append((label, lambda: self._fetch_all(fetch, q, s, e, 100)))

        if task.geography is None and not task.geographies:  # ---- global lane
            gq(names, "en-US", "US", "US:en", "Google News | global")
            if company.website:
                gq(f"site:{_site(company.website)}", "en-US", "US", "US:en", "Google News | own site")
            if self.use_bing:
                bq(names, "en-US", "Bing News | global")
            if self.serper_key:
                sq(names, "en", "us", "Serper | global")
            return jobs

        geos = task.geographies or [task.geography]
        for geo in geos:  # ---------------------------------------- country lane
            display, hl, gl, ceid = google_edition_params(geo.country)
            _, _, _, supported = edition_for(geo.country)
            q = names if supported else f'{names} "{display}"'
            gq(q, hl, gl, ceid, f"Google News | {display}")
            if self.use_bing and self.bing_per_country:
                _, iso, lang, _ = edition_for(geo.country)
                bq(f'{names} "{display}"', f"{lang.split('-')[0]}-{iso}", f"Bing News | {display}")
            if self.serper_key:
                sq(q, hl, gl, f"Serper | {display}")
        return jobs

    # -- end-of-run retry ----------------------------------------------
    def retry_failed(self) -> List[FailedQuery]:
        pending, self._failed, self.failed_queries = self._failed, [], []
        still: List[FailedQuery] = []
        for f in pending:
            out = f.redo()
            if out is None:
                still.append(f.fq)
        return still

    # -- link resolution -----------------------------------------------
    def resolve_url(self, url: str) -> str:
        """Google News redirect -> the real publisher URL (or the input on failure)."""
        if not _is_google_link(url):
            return url
        real = _decode_offline(url)
        if real:
            return real
        try:
            from googlenewsdecoder import gnewsdecoder  # type: ignore
            self._limiter.wait()
            res = gnewsdecoder(url, interval=0)
            if res.get("status") and res.get("decoded_url"):
                return res["decoded_url"]
        except Exception:  # noqa: BLE001 - keep the redirect link rather than lose the article
            pass
        return url

    def resolve_urls(self, urls: List[str], workers: int = 8) -> Dict[str, str]:
        from concurrent.futures import ThreadPoolExecutor
        todo = [u for u in dict.fromkeys(urls) if _is_google_link(u)]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return dict(zip(todo, pool.map(self.resolve_url, todo)))

    # -- full-text extraction (replaces Parallel Extract) ---------------
    def extract(self, urls: List[str], max_chars_per_result: int = 12000) -> Dict[str, Tuple[Optional[str], Optional[str]]]:
        out: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
        for u in urls:
            out[u] = self._extract_one(u, max_chars_per_result)
        return out

    def _extract_one(self, url: str, max_chars: int) -> Tuple[Optional[str], Optional[str]]:
        if _is_google_link(url):
            return None, None
        r = self._get(url, limiter=self._page_limiter, headers={"Accept": "text/html,*/*;q=0.8"})
        if r is None or "html" not in (r.headers.get("Content-Type", "html")).lower():
            return None, None
        return extract_text(r.text, max_chars)


def extract_text(page_html: str, max_chars: int = 12000) -> Tuple[Optional[str], Optional[str]]:
    """(article_text, page_title) from raw HTML; (None, title) when no body found."""
    title = None
    text = None
    try:
        from bs4 import BeautifulSoup  # type: ignore
        soup = BeautifulSoup(page_html, "html.parser")
        og = soup.find("meta", attrs={"property": "og:title"})
        title = (og.get("content") if og and og.get("content") else (soup.title.string if soup.title else None))
        title = title.strip() if title else None
    except Exception:  # noqa: BLE001
        soup = None
    try:
        import trafilatura  # type: ignore
        text = trafilatura.extract(page_html, include_comments=False, include_tables=False)
    except Exception:  # noqa: BLE001
        text = None
    if not text and soup is not None:
        for t in soup(["script", "style", "nav", "header", "footer", "aside", "form", "noscript"]):
            t.decompose()
        container = soup.find("article") or soup
        paras = [p.get_text(" ", strip=True) for p in container.find_all("p")]
        paras = [p for p in paras if len(p) >= 60]
        text = "\n".join(paras) if sum(len(p) for p in paras) >= 300 else None
    return (text[:max_chars] if text else None), title
