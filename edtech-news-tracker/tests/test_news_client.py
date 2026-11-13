"""Offline tests for the Google/Bing news client (no network needed).
Run:  python -m unittest discover -s tests -v"""
import base64
import sys
import unittest
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracker.excel_loader import Company, Geography, load_tracking_list  # noqa: E402
from tracker.news_client import (  # noqa: E402
    GOOGLE_CAP, NewsSearchClient, _decode_offline, edition_for, extract_text,
    google_edition_params, parse_bing_rss, parse_google_rss,
)
from tracker.query_builder import SearchTask, build_geography_task, build_global_broad_task  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

GOOGLE_RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Duolingo opens new office in Berlin - Handelsblatt</title>
<link>https://news.google.com/rss/articles/{gid}?oc=5</link>
<pubDate>Thu, 12 Nov 2026 09:30:00 GMT</pubDate>
<description>&lt;a href="x"&gt;Duolingo opens new office in Berlin&lt;/a&gt;&amp;nbsp;&lt;font&gt;Handelsblatt&lt;/font&gt;</description>
<source url="https://www.handelsblatt.com">Handelsblatt</source></item></channel></rss>"""

BING_RSS = """<?xml version="1.0"?><rss xmlns:News="http://news"><channel>
<item><title>Duolingo raises funding</title>
<link>http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;url=https%3a%2f%2fwww.example.com%2fnews%2fduolingo-raises-funding-2026&amp;c=1</link>
<description>The language app raised money.</description><pubDate>Thu, 12 Nov 2026 07:00:00 GMT</pubDate>
<News:Source>Example</News:Source></item></channel></rss>"""


class Resp:
    def __init__(self, text="", status=200, ctype="text/xml"):
        self.text, self.status_code, self.headers = text, status, {"Content-Type": ctype}


class FakeSession:
    def __init__(self, handler):
        self.handler, self.headers, self.urls = handler, {}, []

    def get(self, url, **kw):
        self.urls.append(url)
        return self.handler(url)


def fast_client(handler, **kw):
    c = NewsSearchClient(session=FakeSession(handler), requests_per_second=1000, backoff_base=0, **kw)
    return c


class Parsing(unittest.TestCase):
    def test_google(self):
        gid = base64.urlsafe_b64encode(b"\x08\x13\x22\x2ahttps://www.handelsblatt.com/x/duolingo-berlin-1\xd2\x01\x00").decode().rstrip("=")
        r = parse_google_rss(GOOGLE_RSS.format(gid=gid))[0]
        self.assertEqual(r.title, "Duolingo opens new office in Berlin")
        self.assertEqual(r.source_domain, "handelsblatt.com")
        self.assertTrue(r.published_date.startswith("2026-11-12"))
        self.assertEqual(_decode_offline(r.url), "https://www.handelsblatt.com/x/duolingo-berlin-1")

    def test_bing(self):
        r = parse_bing_rss(BING_RSS)[0]
        self.assertEqual(r.url, "https://www.example.com/news/duolingo-raises-funding-2026")
        self.assertIn("raised money", r.excerpt)

    def test_extract_text(self):
        body = "<html><head><title>T</title></head><body><article>" + "".join(
            f"<p>{'Sentence about Duolingo number %d. ' % i * 3}</p>" for i in range(6)) + "</article></body></html>"
        text, title = extract_text(body)
        self.assertEqual(title, "T")
        self.assertIn("Duolingo", text)


class Editions(unittest.TestCase):
    def test_every_country_in_the_workbook_resolves(self):
        _, geos, _ = load_tracking_list(ROOT / "edtech_tracking_list.xlsx")
        self.assertEqual(len(geos), 82)
        unknown = [g.country for g in geos if edition_for(g.country)[1:] == ("US", "en", False) and g.country not in ("US",)]
        self.assertEqual(unknown, [], f"countries missing from the edition table: {unknown}")
        for g in geos:
            name, hl, gl, ceid = google_edition_params(g.country)
            self.assertRegex(ceid, r"^[A-Z]{2}:[A-Za-z0-9-]+$")

    def test_known_editions(self):
        self.assertEqual(google_edition_params("Germany"), ("Germany", "de-DE", "DE", "DE:de"))
        self.assertEqual(google_edition_params("Mexico")[1:], ("es-419", "MX", "MX:es-419"))
        self.assertEqual(google_edition_params("Brazil")[1:], ("pt-BR", "BR", "BR:pt-419"))
        self.assertEqual(google_edition_params("India")[1:], ("en-IN", "IN", "IN:en"))
        self.assertEqual(google_edition_params("CzechRepublic")[0], "Czech Republic")
        self.assertEqual(google_edition_params("Cambodia")[1:], ("en-US", "US", "US:en"))  # no edition -> US fallback


class Search(unittest.TestCase):
    start, end = datetime(2026, 11, 12), datetime(2026, 11, 12, 23, 59, 59)

    def test_country_query_uses_local_edition_and_dates(self):
        c = fast_client(lambda url: Resp("<rss/>"), use_bing=False)
        co = Company("Duolingo", aliases=["Duolingo Inc"])
        task = build_geography_task(co, Geography("Germany", ["German"]), "2026-11-12", "2026-11-12")
        self.assertEqual(c.search_task(task, self.start, self.end), [])
        qs = parse_qs(urlsplit(c._http.urls[0]).query)
        self.assertEqual((qs["hl"][0], qs["gl"][0], qs["ceid"][0]), ("de-DE", "DE", "DE:de"))
        self.assertIn('"Duolingo" OR "Duolingo Inc"', qs["q"][0])
        self.assertIn("after:2026-11-11 before:2026-11-13", qs["q"][0])

    def test_country_without_edition_adds_country_name(self):
        c = fast_client(lambda url: Resp("<rss/>"), use_bing=False)
        task = build_geography_task(Company("Duolingo"), Geography("Cambodia", ["Khmer"]), "2026-11-12", "2026-11-12")
        c.search_task(task, self.start, self.end)
        q = parse_qs(urlsplit(c._http.urls[0]).query)["q"][0]
        self.assertIn('"Cambodia"', q)

    def test_global_lane_merges_google_and_bing_and_dedups(self):
        def handler(url):
            return Resp(BING_RSS if "bing.com" in url else GOOGLE_RSS.format(gid="CBMiAA"))
        c = fast_client(handler)
        task = build_global_broad_task(Company("Duolingo", website="https://www.duolingo.com"), "2026-11-12", "2026-11-12")
        res = c.search_task(task, self.start, self.end)
        self.assertTrue(any("bing" not in r.url and "example.com" in r.url for r in res))
        self.assertTrue(any("news.google.com" in r.url for r in res))
        self.assertTrue(any("site%3Aduolingo.com" in u or "site:duolingo.com" in u.replace("%3A", ":") for u in c._http.urls))

    def test_truncated_query_is_split_so_nothing_is_lost(self):
        def handler(url):
            q = parse_qs(urlsplit(url).query)["q"][0]
            n = GOOGLE_CAP if ("OR partnership" not in q and "(" not in q.split("before:")[0].split('"Duolingo"')[-1]) else 3
            tag = q.replace(" ", "_")[-25:]
            items = "".join(
                f"<item><title>Duolingo story {tag} {i} - Src</title><link>https://news.google.com/rss/articles/{tag}{i}</link>"
                f"<pubDate>Thu, 12 Nov 2026 09:30:00 GMT</pubDate><source url='https://s.com'>Src</source></item>"
                for i in range(n))
            return Resp(f"<rss><channel>{items}</channel></rss>")
        c = fast_client(handler, use_bing=False)
        task = build_global_broad_task(Company("Duolingo"), "2026-11-12", "2026-11-12")
        res = c.search_task(task, self.start, self.end)
        self.assertGreater(c.truncated_splits, 0)
        self.assertGreater(len(res), GOOGLE_CAP)  # more than one page's worth was recovered

    def test_failures_are_recorded_and_retried(self):
        state = {"ok": False}
        c = fast_client(lambda url: Resp(GOOGLE_RSS.format(gid="CBMiAA")) if state["ok"] else Resp("", 500), use_bing=False)
        c.max_retries = 1
        task = build_geography_task(Company("Duolingo"), Geography("Germany", ["German"]), "2026-11-12", "2026-11-12")
        self.assertEqual(c.search_task(task, self.start, self.end), [])
        self.assertEqual(len(c.failed_queries), 1)
        state["ok"] = True
        self.assertEqual(c.retry_failed(), [])


if __name__ == "__main__":
    unittest.main()
