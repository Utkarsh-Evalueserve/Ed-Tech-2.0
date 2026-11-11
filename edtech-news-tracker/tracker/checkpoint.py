"""
Resumability: if a run is interrupted, the next run for the SAME date window
picks up from the last fully-completed company instead of starting over.

State is a small JSON file plus a pickle of articles collected so far.
"""
from __future__ import annotations

import json
import pickle
from pathlib import Path
from typing import List

from .relevance import Article


def _paths(checkpoint_dir: Path, start_date: str, end_date: str) -> tuple[Path, Path]:
    tag = f"{start_date}_to_{end_date}"
    return checkpoint_dir / f"state_{tag}.json", checkpoint_dir / f"articles_{tag}.pkl"


def load(checkpoint_dir: Path, start_date: str, end_date: str) -> tuple[List[str], List[Article]]:
    """Returns (completed_company_names, articles_so_far). Empty if no checkpoint exists."""
    state_path, articles_path = _paths(checkpoint_dir, start_date, end_date)
    if not state_path.exists():
        return [], []
    try:
        state = json.loads(state_path.read_text())
        articles = pickle.loads(articles_path.read_bytes()) if articles_path.exists() else []
        return state.get("completed_companies", []), articles
    except Exception:  # noqa: BLE001 - a corrupt checkpoint should never block a run
        return [], []


def save(
    checkpoint_dir: Path,
    start_date: str,
    end_date: str,
    completed_companies: List[str],
    articles: List[Article],
) -> None:
    state_path, articles_path = _paths(checkpoint_dir, start_date, end_date)
    state_path.write_text(json.dumps({"completed_companies": completed_companies}))
    articles_path.write_bytes(pickle.dumps(articles))


def clear(checkpoint_dir: Path, start_date: str, end_date: str) -> None:
    state_path, articles_path = _paths(checkpoint_dir, start_date, end_date)
    state_path.unlink(missing_ok=True)
    articles_path.unlink(missing_ok=True)
