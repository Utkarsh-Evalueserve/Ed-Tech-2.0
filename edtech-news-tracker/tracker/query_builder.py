"""
Builds Parallel.ai search requests for a company.

Query design (updated after real runs kept missing genuine local/regional
articles -- German, Korean, Ukrainian, Mexican, Romanian, Cambodian,
Turkish, Brazilian and Qatari outlets covering a company's local activity,
none of which a handful of broad, shared-across-82-countries searches could
reliably surface):

  1. Official/press lane (1 call/company) -- the company's own newsroom/
     press/blog/investor pages, major press-release wires, AND a partner
     organization's own newsroom when the announcement is about a deal
     involving this company. English, global.
  2. Global/broad lane (1 call/company) -- catches internationally-picked-
     up or syndicated stories not tied to one specific tracked country
     (e.g. a US lawsuit reported by a German financial site).
  3. Per-geography lane (1 call per company PER tracked geography) -- the
     real fix for local/regional recall: a dedicated, country-specific
     search (local institutions, local partnerships, conferences, awards,
     accreditation) rather than one shared call across all 82 countries.

For a 280-company, 82-geography list this is ~280 x 84 = ~23,500 calls --
deliberately comprehensive rather than fast, per an explicit decision to
prioritize catching every article over runtime (see README for the time/
cost tradeoff and the knobs to dial it back down if needed).

Brand names/aliases are always kept as-is (never translated). Any
company-specific disambiguation guidance (the Notes column) is appended to
every objective below -- see _notes_clause.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .config import CATEGORIES, PRESS_WIRE_DOMAINS
from .excel_loader import Company, Geography


@dataclass
class SearchTask:
    company: Company
    lane: str  # "official_press" | "global_broad" | "geography"
    objective: str
    search_queries: List[str]
    geography: Geography | None = None
    geographies: List[Geography] | None = None  # set for a multi-country group call


def _alias_clause(company: Company) -> str:
    names = company.all_names()
    return " OR ".join(f'"{n}"' for n in names)


def _domain(url: str) -> str:
    if not url:
        return ""
    url = url.replace("https://", "").replace("http://", "").replace("www.", "")
    return url.split("/")[0]


def _notes_clause(company: Company) -> str:
    """Surfaces a company's disambiguation notes (from the tracking list's
    Notes column, e.g. "Apollo Education Group ONLY. NOT Apollo private
    equity, Apollo Global Management, or Apollo Hospitals") directly in the
    search objective. An LLM-driven search follows a plain-language "NOT X,
    NOT Y" instruction far more reliably than any keyword-based filter
    applied after the fact, so this is the primary place disambiguation
    guidance should live."""
    if not company.notes:
        return ""
    return f" IMPORTANT disambiguation guidance for this company: {company.notes}"


def _date_window_clause(start_date: str, end_date: str) -> str:
    if start_date == end_date:
        return (
            f"The article MUST be published on {start_date}. Do not include older press "
            f"releases or articles, even ones still indexed, cached, or re-shared today -- "
            f"check the actual original publish date, not just whether the page currently "
            f"appears in search results. State the publish date explicitly when you find it."
        )
    return (
        f"The article MUST be published between {start_date} and {end_date} (inclusive). "
        f"Do not include older press releases or articles, even ones still indexed, cached, "
        f"or re-shared within this window -- check the actual original publish date. State "
        f"the publish date explicitly when you find it."
    )


def build_official_press_task(company: Company, start_date: str, end_date: str) -> SearchTask:
    site_hint = f" site:{_domain(company.website)}" if company.website else ""
    wire_sites = " OR ".join(f"site:{d}" for d in PRESS_WIRE_DOMAINS)
    aliases_txt = ", ".join(company.aliases) if company.aliases else "no aliases"

    objective = (
        f"Find news specifically ABOUT the education/ed-tech company '{company.name}' "
        f"(also known as: {aliases_txt}) -- not pages that merely mention the name in passing. "
        f"Primary sources: press releases or news posted on {company.name}'s own newsroom/press/"
        f"blog/investor-relations pages, OR press releases about {company.name} distributed via "
        f"major wire services (PR Newswire, Business Wire, GlobeNewswire, Cision, Accesswire). "
        f"ALSO include an announcement hosted on a PARTNER organization's own newsroom when the "
        f"announcement is specifically about a deal/partnership/program involving {company.name} "
        f"(e.g. the other party in a partnership posting the release on their own site rather than "
        f"{company.name}'s) -- do not require {company.name} to be the one hosting it. "
        f"Return the specific article/press-release URL, never a homepage, section front, author "
        f"index, or topic-tag listing page. Topics of interest: {', '.join(CATEGORIES)}. "
        f"Exclude: generic company profile/about/overview pages, directory listings, and routine "
        f"stock price or analyst rating commentary. {_date_window_clause(start_date, end_date)}"
        f"{_notes_clause(company)}"
    )
    queries = [
        f"{company.name} newsroom press release{site_hint}",
        f"{_alias_clause(company)} press release ({wire_sites})",
        f"{company.name} announcement OR partnership OR appointment OR award",
        f"{company.name} partners with OR signs agreement with OR collaborates with",
    ]
    return SearchTask(company, "official_press", objective, queries)


def build_global_broad_task(company: Company, start_date: str, end_date: str) -> SearchTask:
    aliases_txt = ", ".join(company.aliases) if company.aliases else "no aliases"

    objective = (
        f"Find any news coverage, from any country or source, that is SPECIFICALLY ABOUT the "
        f"education/ed-tech company '{company.name}' (also known as: {aliases_txt}) -- including "
        f"a story that originated elsewhere but was picked up, translated, or syndicated by an "
        f"international or non-English outlet (e.g. a US lawsuit reported by a German financial "
        f"site, or a product update covered by a regional tech blog). Only count articles where "
        f"this company is the actual subject, not a passing mention. Return the specific article "
        f"URL, never a homepage, section front, or topic-tag listing page. Topics of interest: "
        f"{', '.join(CATEGORIES)}. Exclude: generic company profile/directory pages and routine "
        f"stock/analyst commentary. {_date_window_clause(start_date, end_date)}{_notes_clause(company)}"
    )
    queries = [
        f'"{company.name}" news',
        f"{_alias_clause(company)} lawsuit OR controversy OR complaint OR investigation",
        f"{company.name} launches OR unveils OR announces",
        f"{company.name} signs OR appoints OR partners",
    ]
    return SearchTask(company, "global_broad", objective, queries)


def build_geography_task(
    company: Company, geography: Geography, start_date: str, end_date: str
) -> SearchTask:
    aliases_txt = ", ".join(company.aliases) if company.aliases else "no aliases"
    languages = ", ".join(geography.languages) if geography.languages else "the local language"
    source_hint = ""
    if geography.local_sources:
        source_hint = " Prioritize these known local sources if relevant: " + ", ".join(geography.local_sources) + "."

    objective = (
        f"Find news published in {geography.country}, in {languages} or in English, that is "
        f"SPECIFICALLY ABOUT the education/ed-tech company '{company.name}' (also known as: "
        f"{aliases_txt}) -- for example a local partnership with a school/university/ministry, a "
        f"conference or event appearance, an accreditation or cooperation agreement, a local "
        f"office opening or market entry, an award, a local product launch, or local press "
        f"coverage of the company's activities. Only count articles where this company is the "
        f"actual subject of the story, not a passing mention alongside many others. Return the "
        f"specific article URL, never a homepage, section front, or topic-tag listing page."
        f"{source_hint} Topics of interest: {', '.join(CATEGORIES)}. Exclude: generic company "
        f"profile/directory pages and routine stock/analyst commentary. "
        f"{_date_window_clause(start_date, end_date)}{_notes_clause(company)}"
    )
    queries = [
        f'"{company.name}" {geography.country}',
        f"{_alias_clause(company)} partnership OR MOU OR agreement {geography.country}",
        f"{company.name} conference OR event OR award {geography.country}",
    ]
    return SearchTask(company, "geography", objective, queries, geography=geography)


def build_geography_group_task(
    company: Company, geos: List[Geography], start_date: str, end_date: str
) -> SearchTask:
    """One search call covering several countries that share a language/region.
    A call costs the same however many countries it names, so grouping is what
    lets every company x every country be covered within a fixed budget."""
    if len(geos) == 1:
        return build_geography_task(company, geos[0], start_date, end_date)
    aliases_txt = ", ".join(company.aliases) if company.aliases else "no aliases"
    countries = [g.country for g in geos]
    languages = ", ".join(dict.fromkeys(l for g in geos for l in g.languages)) or "the local languages"
    sources = list(dict.fromkeys(s for g in geos for s in g.local_sources))
    source_hint = (" Prioritize these known local sources if relevant: " + ", ".join(sources) + ".") if sources else ""
    objective = (
        f"Find news published in ANY of these countries: {', '.join(countries)} -- in {languages} or in "
        f"English -- that is SPECIFICALLY ABOUT the education/ed-tech company '{company.name}' (also known "
        f"as: {aliases_txt}). Run the search for EACH country separately so none is missed. Examples: a "
        f"local partnership with a school/university/ministry, a conference or event appearance, an "
        f"accreditation or cooperation agreement, an office opening or market entry, an award, a local "
        f"product launch, or local press coverage. Only count articles where this company is the actual "
        f"subject, not a passing mention. Return the specific article URL, never a homepage, section "
        f"front, or topic-tag listing page.{source_hint} Topics of interest: {', '.join(CATEGORIES)}. "
        f"Exclude: generic company profile/directory pages and routine stock/analyst commentary. "
        f"{_date_window_clause(start_date, end_date)}{_notes_clause(company)}"
    )
    c_or = " OR ".join(countries)
    queries = [
        f'"{company.name}" ({c_or})',
        f"{_alias_clause(company)} partnership OR MOU OR agreement ({c_or})",
        f"{company.name} conference OR event OR award OR launch ({c_or})",
    ]
    return SearchTask(company, "geography", objective, queries, geographies=geos)


_TOPIC_FOCI = [
    ("partnerships, agreements, MOUs, collaborations, investments, funding, acquisitions, grants, "
     "scholarships and education-sector deployments",
     "partners with OR agreement OR MOU OR funding OR acquires OR grant OR scholarship"),
    ("product launches, new features, AI tools, research studies and reports, curricula and programmes, "
     "events, forums, workshops, conferences and webinars",
     "launches OR unveils OR new feature OR study OR report OR forum OR workshop OR conference"),
    ("leadership appointments, awards and recognitions, regulation and government policy, lawsuits, "
     "controversies, data breaches, layoffs and financial results",
     "appoints OR named OR award OR regulation OR lawsuit OR breach OR layoffs OR results"),
]


def build_topic_tasks(company: Company, start_date: str, end_date: str) -> List[SearchTask]:
    """Deep-dive lane for high-volume companies whose core searches came back
    full (e.g. OpenAI, Duolingo): one call per topic cluster so a day's worth
    of stories is not capped by a single 10-result call."""
    aliases_txt = ", ".join(company.aliases) if company.aliases else "no aliases"
    tasks = []
    for focus, kw in _TOPIC_FOCI:
        objective = (
            f"Find ALL distinct news stories, from any country or language, that are SPECIFICALLY ABOUT "
            f"'{company.name}' (also known as: {aliases_txt}), focused on: {focus}. Prefer the most "
            f"specific article URL for each distinct story (never a homepage, tag or listing page) and "
            f"return as many different stories as exist rather than repeated coverage of one. "
            f"{_date_window_clause(start_date, end_date)}{_notes_clause(company)}"
        )
        queries = [f"{_alias_clause(company)} {kw}", f'"{company.name}" news {start_date}']
        tasks.append(SearchTask(company, "topic_deep_dive", objective, queries))
    return tasks


def build_tasks_for_company(
    company: Company, geographies: List[Geography], start_date: str, end_date: str
) -> List[SearchTask]:
    tasks = [
        build_official_press_task(company, start_date, end_date),
        build_global_broad_task(company, start_date, end_date),
    ]
    for geo in geographies:
        tasks.append(build_geography_task(company, geo, start_date, end_date))
    return tasks
