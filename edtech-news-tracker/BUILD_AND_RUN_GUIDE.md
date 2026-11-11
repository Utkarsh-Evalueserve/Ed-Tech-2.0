# EdTech News Tracker — Build & Run Guide

A single reference covering how this project came together, how to run it
day-to-day, every issue we hit while setting it up on Windows + VS Code (and
the fix), and a reusable checklist for building a similar tool next time.

---

## Part 1 — How this project was designed and built

1. **Pin down scope before writing any code.** The brief specified: what to
   track (300 companies, 82 geographies), what sources are mandatory
   (official sites, press wires, local-language news), what to include vs.
   exclude (news categories), and what "done" looks like (a timestamped
   Excel report with specific columns). All of that was nailed down before
   touching a keyboard.

2. **Find and validate the real input data early.** The actual
   `Competitors_Geography`-style source file turned out to be two
   independent, stacked lists (a company column and a country column that
   didn't correspond row-by-row) rather than the richer structure the brief
   described. This was checked explicitly (counting rows, checking for
   1:1 pairing) before building the loader, instead of assuming the file
   matched the spec.

3. **Verify the external API's real interface before integrating it.**
   Web search summaries of the Parallel.ai SDK turned out to be slightly
   wrong (`processor`/`max_results` vs. the actual `mode`/
   `advanced_settings.max_results`). The fix was to install the SDK
   (`pip install parallel-web`) and inspect it directly:
   ```python
   import inspect
   from parallel import Parallel
   print(inspect.signature(Parallel.search))
   ```
   **Lesson for next time:** never trust a documentation summary for exact
   parameter names — install the library and introspect it, or read its
   source, before writing the integration layer.

4. **Design module boundaries before writing logic.** Each concern got its
   own file: settings/secrets, input loading + validation, request
   building, API client (retry/backoff/concurrency), translation,
   relevance scoring + classification, deduplication, Excel export, and
   checkpoint/resume. This makes each piece independently testable and
   easy to swap out later.

5. **Build reliability in from the start, not as an afterthought:**
   - Retries with exponential backoff + a concurrency limit on the API client.
   - Failed queries are logged and retried once more at the end of the run
     — never silently skipped.
   - Low-confidence matches are kept and flagged for manual review, never
     silently dropped (the brief's core principle: missing an article is
     worse than a false positive).
   - A checkpoint file after every completed company, so an interrupted
     run resumes instead of restarting.

6. **Test each module offline with fake data before touching the real API.**
   Excel loading, query building, translation fallback, relevance scoring,
   dedup, and export were all run against hand-built mock inputs first.
   This caught real bugs (e.g. the translator attempting — and failing — a
   network call even for already-English text) without spending API
   quota or needing a key.

7. **Write the README for the least technical reader**, with exact
   commands, not just descriptions.

---

## Part 2 — Full environment setup on Windows + VS Code (from zero)

1. **Install Python 3.11 or 3.12** (avoid brand-new versions like 3.14 —
   some packages don't have prebuilt Windows wheels for them yet, which
   causes confusing install failures).
   - Simplest route: download the installer from
     `https://www.python.org/downloads/release/python-3119/` → "Windows
     installer (64-bit)" (a `.exe` file — **not** the `.tar.xz` source
     archive, which is for Linux/macOS and requires compiling).
   - On the first installer screen, check **"Add python.exe to PATH"**,
     then "Install Now".
   - On a company-managed laptop, installers may be blocked by IT policy —
     if so, ask IT to install Python 3.11 directly rather than fighting it.
   - (We also tried the newer "Python Install Manager" tool — if you use
     it and see `py install 3.11` fail with a "legacy py.exe" warning,
     uninstall the old **"Python Launcher"** entry from Windows' "Installed
     Apps" first, then retry in a fresh terminal.)

2. **Verify the install** in a new terminal:
   ```
   python --version
   ```

3. **Get the project folder onto your machine** (clone the repo / pull the
   right branch, or copy the project folder).

4. **Open the exact project folder in VS Code** — `File` → `Open Folder...`
   → select the specific project folder (e.g. `edtech-news-tracker`), not
   a parent folder. Opening the wrong/parent folder was the single biggest
   source of confusion while setting this up (it can show an unrelated
   project or branch if one happens to exist nearby).

5. **Select the right interpreter:** `Ctrl+Shift+P` → "Python: Select
   Interpreter" → pick the 3.11/3.12 entry.

6. **Open a fresh terminal** (`Terminal` → `New Terminal`) so it starts
   with that interpreter active. Confirm:
   ```
   python --version
   ```

7. **Confirm you're in the right folder:**
   ```
   dir
   ```
   You should see `requirements.txt`, the main `.py` script, etc. If not,
   `cd` into the correct path (find it with
   `Get-ChildItem -Path "C:\Users\<you>" -Recurse -Filter "<main_script>.py"`
   if unsure where it landed).

8. **Install dependencies:**
   ```
   python -m pip install --upgrade pip
   python -m pip install -r requirements.txt
   ```

9. **Set up secrets:** copy `.env.example` → rename the copy to `.env` →
   open it and paste your real API key in → save. (`.env` is already
   excluded via `.gitignore`, so it's never committed.)

10. **(Recommended) Do a small first run.** Back up the real data file,
    temporarily trim it down to 2-3 rows, save and close it (Excel locks
    open files, which blocks the script from reading them), then run the
    script once to confirm everything's wired up before committing to a
    full, long run.

---

## Part 3 — Day-to-day running (quick reference)

1. Open the project folder in VS Code.
2. Open a terminal, confirm you're in the project folder (`dir` shows
   `requirements.txt`).
3. Run:
   ```
   python edtech_news_tracker.py
   ```
4. Press **Enter** twice when asked for dates to default to the last 24
   hours (or type explicit `YYYY-MM-DD` dates for a custom window).
5. Watch the progress bar. A full run across every company/geography is
   deliberately thorough and can take a long time — that's by design
   (missing a relevant article is treated as worse than a slow run).
6. `Ctrl+C` is always safe to stop it early — progress is checkpointed
   per company, so rerunning with the **same dates** resumes instead of
   starting over.
7. Results land in `output/EdTech_News_<start>_to_<end>.xlsx` (a `News`
   tab and a `Run Log` tab).

---

## Part 4 — Troubleshooting log (real issues hit, and the fix)

| Symptom | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: No module named 'pydantic'` (or any package) | `pip install` and `python run.py` used **different** Python interpreters | Always check `python --version` right before both the install step and the run step, in the same terminal |
| Installer `.tar.xz` file won't run on Windows | That's the Unix **source** archive, not a Windows installer | Download the `.exe` installer from python.org instead |
| `py install 3.11` → "legacy py.exe" warning | An old "Python Launcher" app was intercepting the new Python Install Manager's `py` command | Uninstall "Python Launcher" via Windows "Installed Apps", reopen terminal, retry |
| Install Manager fails mid-install with a generic error on a work laptop | Likely blocked by corporate endpoint protection / policy | Run the terminal as Administrator; if that still fails, have IT install it directly |
| `pip install -r requirements.txt` → "Could not open requirements file" | Terminal wasn't actually inside the project folder | Run `dir` to check; `cd` into the correct folder (use `Get-ChildItem -Recurse -Filter` to find it if unsure) |
| VS Code Explorer shows an unfamiliar/unrelated script and data files | A different local folder or branch was open — multiple local copies of a similarly-named repo can exist | Use `Get-ChildItem -Recurse -Filter "<expected_filename>.py"` to find the real file's path, then `File → Open Folder` directly to that exact path |
| Notification: "terminal environment injection is disabled... .env" | Informational only — VS Code offering to auto-load `.env` into the terminal's shell environment | Harmless to dismiss; the script loads `.env` itself via `python-dotenv` regardless of this setting |
| Script sits at "0%" for a while | Normal — large jobs with many API calls per item take time by design | Be patient on a full run, or trim the input file for a fast test run first |
| Script can't read the input Excel file | The file was still open in Excel, which locks it | Close the file in Excel before running the script |

---

## Part 5 — Reusable playbook for the next similar project

Use this checklist whenever building another API-driven data-collection
tool (news monitoring, pricing trackers, competitor scrapers, etc.):

1. **Write the scope down first**: what to collect, from where, how often,
   what the output should look like. Resolve this before any code.
2. **Get and inspect the real input data early** — don't assume it matches
   whatever structure was described; check row counts, column alignment,
   duplicates, blanks.
3. **Verify the external API's actual current interface** by installing
   its SDK and introspecting it (`inspect.signature(...)`), rather than
   trusting a search summary or possibly-stale documentation.
4. **Split the code into single-purpose modules** up front: config/secrets,
   input loading + validation, request building, API client (with
   retry/backoff/concurrency limits), post-processing (filtering, scoring,
   dedup), export, and checkpoint/resume if it's a long batch job.
5. **Test each module offline with hand-built fake data** before spending
   any real API quota — this catches most logic bugs for free.
6. **Handle secrets correctly from the first commit**: `.env` +
   `.env.example` + `.gitignore`. Never hard-code or print/log a key.
7. **Never silently drop ambiguous results** — flag low-confidence items
   for human review instead, if the cost of missing something outweighs
   the cost of a false positive.
8. **Write the README for the least technical person who'll run it**,
   with exact commands, not just prose descriptions.
9. **On a new machine, always confirm the interpreter first**
   (`python --version`, right before both `pip install` and `python run`,
   in the same terminal) — this single mismatch causes the large majority
   of "module not found" style errors.
10. **Keep a troubleshooting log** (like Part 4 above) and add to it the
    first time any new setup error appears — the second project like this
    one will go much faster because of it.
