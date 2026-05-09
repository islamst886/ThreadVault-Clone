"""
run_extraction.py
-----------------
GitHub Actions runner for the ThreadVault bulk subreddit extractor.

Called by the workflow as:
    python .github/workflows/run_extraction.py

Environment variables (set by the workflow before calling this script):
    SUBREDDITS    – comma-separated subreddit names (no r/ prefix)
    POST_LIMIT    – max posts per subreddit
    YEARS_BACK    – years of history to fetch
    COMMENT_SORT  – comment sort mode (top / best / new / controversial / old)
    COMMENT_LIMIT – top-level comments per post (int, or "all")

Design notes
------------
* bulk_extract_subreddits() already calls generate_bulk_docx() internally
  and returns the finished file paths in result["docx_files"].
  Do NOT call generate_bulk_docx() again here.

* The function is called one subreddit at a time so that:
    - Completed DOCX files survive a mid-run timeout.
    - Time-budget checks happen between subreddits, not inside one.

* Processed subreddits are recorded in ./outputs/processed_urls.txt
  so the next day's scheduled run skips them automatically.
  Delete that file to force a full re-extraction.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Resolve paths ─────────────────────────────────────────────────────────────
# This file lives at: .github/workflows/run_extraction.py
# Repo root is three levels up:  .github/workflows/ → .github/ → repo root
_WORKFLOWS_DIR = Path(__file__).parent.resolve()
_REPO_ROOT      = _WORKFLOWS_DIR.parent.parent
_BACKEND_DIR    = _REPO_ROOT / "backend"

# Add backend/ to the import path so Python finds subreddit_bulk_extractor
sys.path.insert(0, str(_BACKEND_DIR))

# Ensure backend/__init__.py exists (harmless if already present)
_init = _BACKEND_DIR / "__init__.py"
if not _init.exists():
    _init.touch()

# Import AFTER adjusting sys.path — no "backend." prefix
from subreddit_bulk_extractor import bulk_extract_subreddits  # type: ignore  # noqa: E402

# ── Constants ─────────────────────────────────────────────────────────────────
MAX_SECONDS    = 5.5 * 3600          # 5.5 h safety cap (GH free tier = 6 h)
OUTPUT_DIR     = _REPO_ROOT / "outputs"
PROCESSED_FILE = OUTPUT_DIR / "processed_state.json"


# ── Read environment variables ────────────────────────────────────────────────

def _env(key: str, default: str) -> str:
    return os.environ.get(key, default).strip()


subreddits_raw = _env("SUBREDDITS",    "personaltraining,fitness")
post_limit     = int(_env("POST_LIMIT",    "200"))
years_back     = float(_env("YEARS_BACK",  "2"))
comment_sort   = _env("COMMENT_SORT",  "top")
_cl_raw        = _env("COMMENT_LIMIT", "25")
comment_limit: int | str = "all" if _cl_raw.lower() == "all" else int(_cl_raw)

subreddits: list[str] = [
    s.strip().lstrip("r/").lstrip("/")
    for s in subreddits_raw.split(",")
    if s.strip()
]


# ── Processed-subreddit helpers ───────────────────────────────────────────────

def load_processed() -> set[str]:
    if not PROCESSED_FILE.exists():
        return set()
    try:
        data = json.loads(PROCESSED_FILE.read_text(encoding="utf-8"))
        skipped_subs = set()
        for sub, info in data.items():
            if info.get("status") == "success" and info.get("posts_extracted", 0) > 0:
                docx_files = info.get("docx_files", [])
                all_exist = len(docx_files) > 0 and all((OUTPUT_DIR / Path(f).name).exists() for f in docx_files)
                if all_exist:
                    skipped_subs.add(sub.lower())
        return skipped_subs
    except Exception as e:
        print(f"Error loading {PROCESSED_FILE}: {e}")
        return set()


def mark_processed(name: str, posts_extracted: int, docx_files: list[str]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    data = {}
    if PROCESSED_FILE.exists():
        try:
            data = json.loads(PROCESSED_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    status = "success" if posts_extracted > 0 else "blocked"
    data[name.lower()] = {
        "processed_at": datetime.now(tz=timezone.utc).isoformat(),
        "posts_extracted": posts_extracted,
        "docx_files": [Path(f).name for f in docx_files],
        "status": status
    }
    
    with PROCESSED_FILE.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    run_start = time.monotonic()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print()
    print(f"Starting extraction at {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"Subreddits    : {subreddits}")
    print(f"Post limit    : {post_limit} per subreddit")
    print(f"Years back    : {years_back}")
    print(f"Comment sort  : {comment_sort}")
    print(f"Comment limit : {comment_limit}")
    print(f"Output dir    : {OUTPUT_DIR}")
    print(f"Time cap      : {MAX_SECONDS / 3600:.1f} hours")
    print()

    # Skip subreddits already completed in a prior run
    already_done = load_processed()
    pending = [s for s in subreddits if s.lower() not in already_done]

    if not pending:
        print("All requested subreddits were already extracted. "
              "Delete outputs/processed_state.json to re-run.")
        return

    if already_done:
        skipped = [s for s in subreddits if s.lower() in already_done]
        print(f"Skipping already-processed: {', '.join(skipped)}")

    # ── Process one subreddit at a time ───────────────────────────────────────
    total_posts    = 0
    total_comments = 0
    all_files:  list[str] = []
    completed:  list[str] = []
    time_limit_hit = False

    for idx, sub in enumerate(pending, start=1):
        elapsed = time.monotonic() - run_start

        if elapsed >= MAX_SECONDS:
            remaining = pending[idx - 1:]
            print()
            print("TIME LIMIT APPROACHING — Saved partial results. Run again to continue.")
            print(f"  Completed : {', '.join(completed) or 'none'}")
            print(f"  Remaining : {', '.join(remaining)}")
            time_limit_hit = True
            break

        budget = MAX_SECONDS - elapsed
        print(f"[{idx}/{len(pending)}] Starting r/{sub}  "
              f"(elapsed: {elapsed / 60:.1f} min, budget left: {budget / 3600:.2f} h)")

        try:
            result: dict = await asyncio.wait_for(
                bulk_extract_subreddits(
                    subreddits=[sub],
                    comment_sort=comment_sort,
                    comment_limit=comment_limit,
                    post_limit_per_subreddit=post_limit,
                    years_back=years_back,
                    output_dir=str(OUTPUT_DIR),
                ),
                timeout=budget,
            )
        except asyncio.TimeoutError:
            print(f"r/{sub}: Timed out mid-extraction.")
            print("TIME LIMIT APPROACHING — Saved partial results. Run again to continue.")
            time_limit_hit = True
            break
        except Exception as exc:
            print(f"r/{sub}: Error — {exc}")
            continue

        posts_n    = result.get("total_posts_extracted",    0)
        comments_n = result.get("total_comments_extracted", 0)
        new_files  = result.get("docx_files",               [])

        total_posts    += posts_n
        total_comments += comments_n
        all_files.extend(new_files)
        completed.append(sub)
        mark_processed(sub, posts_extracted=posts_n, docx_files=new_files)

        print(f"   r/{sub}: {posts_n:,} posts | {comments_n:,} comments | "
              f"{len(new_files)} DOCX file(s) generated")
        for f in new_files:
            size_kb = round(os.path.getsize(f) / 1024) if os.path.exists(f) else 0
            print(f"       {os.path.basename(f)}  ({size_kb} KB)")

    # ── Final summary ─────────────────────────────────────────────────────────
    elapsed_total = time.monotonic() - run_start
    print()
    print("=" * 56)
    print("=== EXTRACTION COMPLETE ===")
    print("=" * 56)
    print(f"Subreddits    : {', '.join(completed) if completed else 'none'}")
    print(f"Total posts   : {total_posts:,}")
    print(f"Total comments: {total_comments:,}")
    print(f"Files generated: {len(all_files)}")
    print(f"Time taken    : {elapsed_total / 60:.1f} minutes")
    print("Download from : GitHub Actions > This run > Artifacts")
    print("=" * 56)
    print()

    if not completed and not time_limit_hit:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
