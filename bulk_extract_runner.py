"""
bulk_extract_runner.py
----------------------
GitHub Actions entry-point for the ThreadVault bulk subreddit extractor.

Reads configuration from environment variables (set by the workflow):
  INPUT_SUBREDDITS  – comma-separated subreddit names (no r/ prefix)
  INPUT_POST_LIMIT  – max posts per subreddit   (default: 200)
  INPUT_YEARS_BACK  – years of history to fetch (default: 2)

Outputs
-------
  ./outputs/<subreddit>_<date>.docx  – one or more DOCX files per subreddit
  ./outputs/processed_urls.txt       – tracks which subreddits have been
                                       fully extracted so successive daily
                                       runs continue from where they left off.

Time-limit safety
-----------------
GitHub Actions free tier allows up to 6 hours per job.  This script tracks
wall-clock elapsed time and stops gracefully before reaching 5.5 hours,
saving whatever has been extracted so far.  Partial results are still uploaded
as artifacts, and the next scheduled run picks up from where this one stopped.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Add the backend package to the Python path ──────────────────────────────
# The workflow runs from the repo root; backend/ is a sibling directory.
REPO_ROOT   = Path(__file__).parent.resolve()
BACKEND_DIR = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))

# Import the core extraction function — no FastAPI, no Playwright needed.
from subreddit_bulk_extractor import bulk_extract_subreddits  # type: ignore  # noqa: E402

# ── Configuration constants ──────────────────────────────────────────────────
TIME_LIMIT_SECONDS = 5.5 * 3600   # 5.5 h — comfortable margin under GH's 6 h cap
OUTPUT_DIR         = REPO_ROOT / "outputs"
PROCESSED_FILE     = OUTPUT_DIR / "processed_urls.txt"


# ── Read inputs from environment variables ───────────────────────────────────

def _env(key: str, default: str) -> str:
    return os.environ.get(key, default).strip()


RAW_SUBS   = _env("INPUT_SUBREDDITS", "personaltraining,fitness")
POST_LIMIT = int(_env("INPUT_POST_LIMIT",  "200"))
YEARS_BACK = float(_env("INPUT_YEARS_BACK", "2"))

# Normalise: strip whitespace, strip any leading "r/" the user may have typed
ALL_SUBREDDITS: list[str] = [
    s.strip().lstrip("r/").lstrip("/")
    for s in RAW_SUBS.split(",")
    if s.strip()
]


# ── Processed-subreddit tracking ─────────────────────────────────────────────

def load_processed() -> set[str]:
    """
    Return the set of subreddit names (lower-case) that have already been
    fully extracted in a previous run.
    The file stores one name per line.
    """
    if not PROCESSED_FILE.exists():
        return set()
    lines = PROCESSED_FILE.read_text(encoding="utf-8").splitlines()
    return {ln.strip().lower() for ln in lines if ln.strip()}


def mark_processed(name: str) -> None:
    """Append a subreddit name to the processed file (append-only, idempotent)."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with PROCESSED_FILE.open("a", encoding="utf-8") as fh:
        fh.write(f"{name.lower()}\n")


# ── Summary printer ───────────────────────────────────────────────────────────

def print_summary(
    completed:      list[str],
    docx_files:     list[str],
    total_posts:    int,
    total_comments: int,
    skipped_count:  int,
    run_start:      float,
) -> None:
    elapsed = time.monotonic() - run_start
    print()
    print("=" * 60)
    print("=== EXTRACTION COMPLETE ===")
    print("=" * 60)
    print(f"Subreddits    : {', '.join(completed) if completed else 'none completed'}")
    print(f"Total posts   : {total_posts:,}")
    print(f"Total comments: {total_comments:,}")
    print(f"Files generated: {len(docx_files)}")
    if skipped_count:
        print(f"Subreddits skipped / errored: {skipped_count}")
    print(f"Time taken    : {elapsed / 60:.1f} minutes")
    print("Download from : GitHub Actions > This run > Artifacts")
    print("=" * 60)
    print()


# ── Main async runner ─────────────────────────────────────────────────────────

async def main() -> None:
    run_start = time.monotonic()

    print()
    print("=" * 60)
    print("  ThreadVault — Bulk Subreddit Extractor (GitHub Actions)")
    print("=" * 60)
    print(f"  Started at    : {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"  Subreddits    : {', '.join(ALL_SUBREDDITS)}")
    print(f"  Post limit    : {POST_LIMIT} per subreddit")
    print(f"  Years back    : {YEARS_BACK}")
    print(f"  Output dir    : {OUTPUT_DIR}")
    print(f"  Time limit    : {TIME_LIMIT_SECONDS / 3600:.1f} hours")
    print("=" * 60)
    print()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── Determine which subreddits still need processing ─────────────────────
    already_done = load_processed()
    pending: list[str] = [s for s in ALL_SUBREDDITS if s.lower() not in already_done]

    if not pending:
        print("✅ All requested subreddits were already extracted in a previous run.")
        print("   Delete ./outputs/processed_urls.txt to force a fresh extraction.")
        print_summary([], [], 0, 0, 0, run_start)
        return

    if already_done:
        skipped_prev = [s for s in ALL_SUBREDDITS if s.lower() in already_done]
        print(
            f"⏭  Skipping {len(skipped_prev)} already-processed subreddit(s): "
            f"{', '.join(skipped_prev)}"
        )
        print()

    # ── Process each pending subreddit one at a time ─────────────────────────
    # Running subreddits individually lets us:
    #   • Write each DOCX to disk immediately after the subreddit finishes,
    #     so a time-out mid-run doesn't discard earlier completed work.
    #   • Check the elapsed time budget before starting the next subreddit.
    #   • Mark each subreddit as processed the moment it is done.

    total_posts:     int       = 0
    total_comments:  int       = 0
    all_docx_files:  list[str] = []
    completed_subs:  list[str] = []
    skipped_entries: list[dict] = []
    time_limit_hit: bool = False

    for idx, sub in enumerate(pending, start=1):
        elapsed = time.monotonic() - run_start

        # ── Time-limit guard — check BEFORE starting the next subreddit ──────
        if elapsed >= TIME_LIMIT_SECONDS:
            remaining_subs = pending[idx - 1:]
            print()
            print("⚠️  TIME LIMIT APPROACHING — Saved partial results.")
            print(f"   Completed : {', '.join(completed_subs) or 'none'}")
            print(f"   Remaining : {', '.join(remaining_subs)}")
            print("   Run again to continue.")
            time_limit_hit = True
            break

        remaining_budget_sec = TIME_LIMIT_SECONDS - elapsed
        print(
            f"[{idx}/{len(pending)}] ▶ Starting r/{sub}  "
            f"(elapsed: {elapsed / 60:.1f} min, "
            f"budget remaining: {remaining_budget_sec / 3600:.2f} h)"
        )

        try:
            # Cap the entire subreddit extraction at the remaining time budget.
            # If the extraction is still running when the budget expires,
            # asyncio.wait_for raises TimeoutError so we can upload what we have.
            result: dict = await asyncio.wait_for(
                bulk_extract_subreddits(
                    subreddits=[sub],
                    comment_sort="top",
                    comment_limit=25,
                    post_limit_per_subreddit=POST_LIMIT,
                    years_back=YEARS_BACK,
                    output_dir=str(OUTPUT_DIR),
                ),
                timeout=remaining_budget_sec,
            )

        except asyncio.TimeoutError:
            print(f"\n⏰ r/{sub}: Timed out mid-extraction.")
            print("   TIME LIMIT APPROACHING — Saved partial results. Run again to continue.")
            time_limit_hit = True
            break

        except Exception as exc:
            print(f"\n❌ r/{sub}: Unexpected error — {exc}")
            skipped_entries.append({"name": sub, "reason": str(exc)})
            continue

        # ── Tally results ─────────────────────────────────────────────────────
        posts_n    = result.get("total_posts_extracted",    0)
        comments_n = result.get("total_comments_extracted", 0)
        new_files  = result.get("docx_files",               [])

        total_posts    += posts_n
        total_comments += comments_n
        all_docx_files.extend(new_files)
        skipped_entries.extend(result.get("subreddits_skipped", []))
        completed_subs.append(sub)

        # Persist this subreddit so the next run skips it
        mark_processed(sub)

        print(
            f"   ✓ r/{sub}: {posts_n:,} posts | {comments_n:,} comments | "
            f"{len(new_files)} DOCX file(s)"
        )

    # ── If the time limit was hit, emit the warning message ───────────────────
    if time_limit_hit and not completed_subs:
        print()
        print("⚠️  TIME LIMIT APPROACHING — Saved partial results. Run again to continue.")

    # ── Final summary ─────────────────────────────────────────────────────────
    print_summary(
        completed=completed_subs,
        docx_files=all_docx_files,
        total_posts=total_posts,
        total_comments=total_comments,
        skipped_count=len(skipped_entries),
        run_start=run_start,
    )

    # Surface a non-zero exit code so GitHub marks the run as failed when
    # nothing was extracted at all (helps distinguish from a partial run).
    if not completed_subs and not time_limit_hit:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
