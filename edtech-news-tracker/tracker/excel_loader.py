"""
Config loader for edtech_tracking_list.xlsx.

Reads the Companies and Geographies sheets fresh on every run (nothing about
the tracked companies/geographies is hard-coded) and validates the file at
startup, reporting blank rows, duplicates and missing columns in plain
language rather than crashing on the first problem.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List

import openpyxl


@dataclass
class Company:
    name: str
    aliases: List[str] = field(default_factory=list)
    website: str = ""
    hq_country: str = ""
    notes: str = ""  # free-text disambiguation guidance, e.g. "NOT Apollo Global Management"
    category_code: str = ""  # e.g. "A" (English/Language Learning) -- used to decide full vs. light geography coverage

    def all_names(self) -> List[str]:
        """Company name plus every alias, for search and disambiguation."""
        return [self.name] + [a for a in self.aliases if a]


@dataclass
class Geography:
    country: str
    languages: List[str] = field(default_factory=list)
    local_sources: List[str] = field(default_factory=list)


class TrackingListError(Exception):
    """Raised when the Excel file is missing required structure."""


def _normalize_header(h) -> str:
    return str(h).strip().lower() if h is not None else ""


def _find_column(headers: dict, *candidates: str) -> int | None:
    for cand in candidates:
        if cand in headers:
            return headers[cand]
    return None


def _split_list(raw) -> List[str]:
    if raw is None:
        return []
    return [p.strip() for p in str(raw).split(",") if p.strip()]


def load_tracking_list(path: Path) -> tuple[List[Company], List[Geography], List[str]]:
    """
    Returns (companies, geographies, validation_messages).
    Raises TrackingListError if the file/sheets/required columns are missing.
    """
    if not path.exists():
        raise TrackingListError(
            f"Tracking list not found at '{path}'. Place your Excel file there "
            f"(see README.md) before running the tracker."
        )

    wb = openpyxl.load_workbook(path, data_only=True)
    messages: List[str] = []

    for required_sheet in ("Companies", "Geographies"):
        if required_sheet not in wb.sheetnames:
            raise TrackingListError(
                f"'{path.name}' is missing the required '{required_sheet}' sheet. "
                f"Found sheets: {wb.sheetnames}."
            )

    companies = _load_companies(wb["Companies"], messages)
    geographies = _load_geographies(wb["Geographies"], messages)

    return companies, geographies, messages


def _header_map(ws) -> dict:
    header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True))
    return {_normalize_header(h): idx for idx, h in enumerate(header_row)}


def _load_companies(ws, messages: List[str]) -> List[Company]:
    headers = _header_map(ws)
    name_col = _find_column(headers, "company name", "company", "name")
    if name_col is None:
        raise TrackingListError(
            "Companies sheet is missing a 'Company Name' column. "
            f"Found columns: {list(headers.keys())}."
        )
    alias_col = _find_column(headers, "aliases", "alias")
    website_col = _find_column(headers, "official website", "website")
    hq_col = _find_column(headers, "hq country", "hq", "headquarters", "hq country ")
    notes_col = _find_column(headers, "notes", "disambiguation notes", "disambiguation")
    category_col = _find_column(headers, "category code", "category")

    seen: dict[str, Company] = {}
    blank_rows: List[int] = []
    dup_rows: List[str] = []

    for row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if row_idx - 2 >= ws.max_row:
            break
        name = row[name_col] if name_col < len(row) else None
        if name is None or not str(name).strip():
            if any(v is not None and str(v).strip() for v in row):
                blank_rows.append(row_idx)
            continue
        name = str(name).strip()
        key = name.lower()

        aliases = _split_list(row[alias_col]) if alias_col is not None and alias_col < len(row) else []
        website = str(row[website_col]).strip() if website_col is not None and website_col < len(row) and row[website_col] else ""
        hq_country = str(row[hq_col]).strip() if hq_col is not None and hq_col < len(row) and row[hq_col] else ""
        notes = str(row[notes_col]).strip() if notes_col is not None and notes_col < len(row) and row[notes_col] else ""
        category_code = str(row[category_col]).strip() if category_col is not None and category_col < len(row) and row[category_col] else ""

        if key in seen:
            dup_rows.append(name)
            existing = seen[key]
            for a in aliases:
                if a not in existing.aliases:
                    existing.aliases.append(a)
            existing.website = existing.website or website
            existing.hq_country = existing.hq_country or hq_country
            existing.notes = existing.notes or notes
            existing.category_code = existing.category_code or category_code
            continue

        seen[key] = Company(
            name=name, aliases=aliases, website=website, hq_country=hq_country,
            notes=notes, category_code=category_code,
        )

    if blank_rows:
        messages.append(
            f"Companies sheet: {len(blank_rows)} row(s) had a blank Company Name and were skipped "
            f"(rows: {blank_rows[:20]}{'...' if len(blank_rows) > 20 else ''})."
        )
    if dup_rows:
        messages.append(
            f"Companies sheet: {len(dup_rows)} duplicate company name(s) found and merged "
            f"(aliases combined, first non-blank Website/HQ kept): {sorted(set(dup_rows))[:20]}"
            f"{'...' if len(set(dup_rows)) > 20 else ''}."
        )

    return list(seen.values())


def _load_geographies(ws, messages: List[str]) -> List[Geography]:
    headers = _header_map(ws)
    country_col = _find_column(headers, "country", "geography")
    if country_col is None:
        raise TrackingListError(
            "Geographies sheet is missing a 'Country' column. "
            f"Found columns: {list(headers.keys())}."
        )
    lang_col = _find_column(headers, "primary language(s)", "primary language", "language(s)", "languages")
    sources_col = _find_column(headers, "local news sources", "local sources", "news sources")

    seen: dict[str, Geography] = {}
    blank_rows: List[int] = []
    dup_rows: List[str] = []

    for row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        country = row[country_col] if country_col < len(row) else None
        if country is None or not str(country).strip():
            if any(v is not None and str(v).strip() for v in row):
                blank_rows.append(row_idx)
            continue
        country = str(country).strip()
        key = country.lower()

        languages = _split_list(row[lang_col]) if lang_col is not None and lang_col < len(row) else []
        local_sources = _split_list(row[sources_col]) if sources_col is not None and sources_col < len(row) else []

        if key in seen:
            dup_rows.append(country)
            existing = seen[key]
            for l in languages:
                if l not in existing.languages:
                    existing.languages.append(l)
            for s in local_sources:
                if s not in existing.local_sources:
                    existing.local_sources.append(s)
            continue

        seen[key] = Geography(country=country, languages=languages or ["English"], local_sources=local_sources)

    if blank_rows:
        messages.append(
            f"Geographies sheet: {len(blank_rows)} row(s) had a blank Country and were skipped "
            f"(rows: {blank_rows[:20]}{'...' if len(blank_rows) > 20 else ''})."
        )
    if dup_rows:
        messages.append(
            f"Geographies sheet: {len(dup_rows)} duplicate country/countries found and merged: "
            f"{sorted(set(dup_rows))[:20]}{'...' if len(set(dup_rows)) > 20 else ''}."
        )

    return list(seen.values())
