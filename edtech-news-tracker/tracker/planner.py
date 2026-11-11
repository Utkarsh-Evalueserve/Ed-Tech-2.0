"""
Budget-aware search planning.

Core lanes (official/press + global) run for EVERY company. The remaining
budget is spent on country coverage: countries are grouped by shared language
into batches, and one call per (company, group) is planned. The group size is
chosen automatically so every company x every country is covered if the budget
allows; otherwise units are ordered round-robin across companies (priority
countries first) so the budget is spread evenly rather than exhausting on the
first few companies.
"""
from __future__ import annotations

import math
from collections import OrderedDict
from typing import List

from .excel_loader import Company, Geography


def group_geographies(geos: List[Geography], batch_size: int, priority: List[str]) -> List[List[Geography]]:
    pri = {c.lower(): i for i, c in enumerate(priority)}
    ordered = sorted(geos, key=lambda g: (pri.get(g.country.lower(), 10_000), g.country))
    by_lang: "OrderedDict[str, List[Geography]]" = OrderedDict()
    for g in ordered:
        by_lang.setdefault((g.languages[0].lower() if g.languages else "?"), []).append(g)
    groups: List[List[Geography]] = []
    leftovers: List[Geography] = []
    for members in by_lang.values():
        for i in range(0, len(members), batch_size):
            chunk = members[i : i + batch_size]
            # Full same-language chunks stay pure; partial ones are pooled so
            # one-off languages don't each burn a whole call.
            (groups if len(chunk) == batch_size else leftovers).append(chunk) if len(chunk) == batch_size else leftovers.extend(chunk)
    leftovers.sort(key=lambda g: pri.get(g.country.lower(), 10_000))
    for i in range(0, len(leftovers), batch_size):
        groups.append(leftovers[i : i + batch_size])
    # Most important groups first (best-ranked member).
    groups.sort(key=lambda grp: min(pri.get(g.country.lower(), 10_000) for g in grp))
    return groups


def choose_batch_size(setting: str, geos: List[Geography], n_companies: int, calls_available: int,
                      max_batch: int, priority: List[str]) -> int:
    if setting.strip().lower() != "auto":
        return max(1, int(setting))
    for bs in range(1, max_batch + 1):
        if n_companies * len(group_geographies(geos, bs, priority)) <= calls_available:
            return bs
    return max_batch


def plan_geo_units(companies: List[Company], groups: List[List[Geography]]):
    """Round-robin: group 0 for every company, then group 1 for every company..."""
    return [(c, gi) for gi in range(len(groups)) for c in companies]


def unit_key(kind: str, company: Company, gi: int | None = None) -> str:
    return f"{kind}::{company.name}" + (f"::{gi}" if gi is not None else "")
