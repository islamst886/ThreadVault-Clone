"""
subreddit_bulk_extractor.py
---------------------------
Extracts ALL posts (within a configurable time window) from a list of subreddits,
including all comments for each post, then compiles everything into DOCX files.

Date filtering strategy
~~~~~~~~~~~~~~~~~~~~~~~
Uses Arctic Shift API to fetch posts between two Unix timestamps directly.

Rate-limit policy
~~~~~~~~~~~~~~~~~
  - 1.0 s polite delay between post page requests.
  - 0.5 s polite delay between comment page requests.
  - asyncio.Semaphore(2) for concurrent post extraction.
  - HTTP 429 → wait 30 s, then retry.
  - Individual post failures → skip, insert a placeholder in the DOCX.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import httpx

# ── Local imports ──────────────────────────────────────────────────────────────
from docx_generator import generate_bulk_docx as _gen_bulk_docx  # type: ignore

# ── Logging ────────────────────────────────────────────────────────────────────
logger = logging.getLogger(__name__)

ARCTIC_SHIFT_HEADERS: dict[str, str] = {
    "User-Agent": "ThreadVault/1.0 research tool github.com/Badrul886/ThreadVault",
    "Accept": "application/json"
}

LISTING_DELAY_SEC  = 1.0    
COMMENT_DELAY_SEC  = 0.5    
POST_SEMAPHORE_N   = 2      

STATUS_COLLECTING = "collecting"
STATUS_EXTRACTING = "extracting"
STATUS_GENERATING = "generating"
STATUS_COMPLETE   = "complete"
STATUS_ERROR      = "error"


@dataclass
class BulkJob:
    job_id:     str
    subreddits: list[str]

    status:   str = STATUS_COLLECTING
    error:    Optional[str] = None
    substatus: Optional[str] = None

    current_subreddit: str = ""
    subreddit_index:   int = 0
    total_subreddits:  int = 0

    posts_done_this_sub:  int = 0
    total_posts_this_sub: int = 0
    total_posts_done:     int = 0
    total_posts_all_subs: int = 0

    percent:     float = 0.0
    eta_minutes: int   = 0

    subreddits_processed:     int        = 0
    subreddits_skipped:       list[dict] = field(default_factory=list)
    total_posts_extracted:    int        = 0
    total_comments_extracted: int        = 0
    docx_files:               list[str]  = field(default_factory=list)

    started_at:   float          = field(default_factory=time.monotonic)
    completed_at: Optional[float] = None

    log_messages: list[str] = field(default_factory=list)

    def push_log(self, msg: str) -> None:
        from datetime import datetime, timezone as _tz
        ts   = datetime.now(tz=_tz.utc).strftime("%H:%M")
        line = f"[{ts}] {msg}"
        self.log_messages.append(line)
        if len(self.log_messages) > 20:
            self.log_messages = self.log_messages[-20:]

    def to_status_dict(self) -> dict[str, Any]:
        _phase_map = {
            STATUS_COLLECTING: "scanning_posts",
            STATUS_EXTRACTING: "extracting_posts",
            STATUS_GENERATING: "writing_docx",
            STATUS_COMPLETE:   "complete",
            STATUS_ERROR:      "error",
        }
        elapsed        = time.monotonic() - self.started_at
        time_taken_min = round(elapsed / 60, 1) if self.status == STATUS_COMPLETE else None
        download_urls  = [
            f"/bulk-download/{self.job_id}/{i}"
            for i in range(len(self.docx_files))
        ]
        return {
            "job_id":                   self.job_id,
            "status":                   self.status,
            "substatus":                self.substatus,
            "error":                    self.error,
            "phase":                    _phase_map.get(self.status, self.status),
            "current_subreddit":        self.current_subreddit,
            "subreddit_index":          self.subreddit_index,
            "total_subreddits":         self.total_subreddits,
            "posts_done_this_sub":      self.posts_done_this_sub,
            "total_posts_this_sub":     self.total_posts_this_sub,
            "total_posts_done":         self.total_posts_done,
            "total_posts_all_subs":     self.total_posts_all_subs,
            "total_comments_done":      self.total_comments_extracted,
            "percent":                  self.percent,
            "eta_minutes":              self.eta_minutes,
            "log":                      list(self.log_messages[-8:]),
            "subreddits_processed":     self.subreddits_processed,
            "subreddits_skipped":       self.subreddits_skipped,
            "total_posts_extracted":    self.total_posts_extracted,
            "total_comments_extracted": self.total_comments_extracted,
            "files_generated":          len(self.docx_files),
            "download_urls":            download_urls,
            "docx_files":               self.docx_files,
            "time_taken_minutes":       time_taken_min,
        }

_bulk_jobs: dict[str, BulkJob] = {}

def get_bulk_jobs() -> dict[str, BulkJob]:
    return _bulk_jobs

# ── Formatting Helpers ─────────────────────────────────────────────────────────

def format_timestamp(utc_epoch: float) -> str:
    dt = datetime.fromtimestamp(utc_epoch, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")

def safe_author(data: dict) -> str:
    author = data.get("author") or ""
    if not author or author in ("[deleted]", ""):
        return "[deleted]"
    return author

def safe_body(data: dict, key: str = "body") -> str:
    text = data.get(key) or ""
    return text.strip()

def detect_media_note(post_data: dict, body: str) -> Optional[str]:
    if post_data.get("is_video"):
        return "[POST CONTAINS: Video — not copied]"
    hint = post_data.get("post_hint", "")
    if hint == "image":
        return "[POST CONTAINS: Image — not copied]"
    if hint == "rich:video":
        return "[POST CONTAINS: GIF/Embed — not copied]"
    if hint == "link":
        url = post_data.get("url", "")
        return f"[POST CONTAINS LINK: {url}]"
    if not body.strip() and not hint:
        return "[POST CONTAINS: Link or external content — not copied]"
    return None

def detect_comment_media(body: str) -> Optional[str]:
    notes = []
    if re.search(r"https?://\S+\.(?:jpg|jpeg|png|gif|webp|bmp)", body, re.IGNORECASE):
        notes.append("[CONTAINS: Image link — not copied]")
    if re.search(r"https?://(www\.)?(youtube\.com|youtu\.be|streamable\.com|v\.redd\.it)/\S+", body, re.IGNORECASE):
        notes.append("[CONTAINS: Video link — not copied]")
    return " ".join(notes) if notes else None

def _count_comments(comments: list[dict]) -> int:
    count = len(comments)
    for c in comments:
        count += _count_comments(c["replies"])
    return count

# ── Arctic Shift Helpers ───────────────────────────────────────────────────────

async def fetch_subreddit_info(name: str, client: httpx.AsyncClient) -> dict:
    url_a = "https://arctic-shift.photon-reddit.com/api/subreddits/search"
    url_b = "https://arctic-shift.photon-reddit.com/api/subreddits"
    
    try:
        resp = await client.get(url_a, params={"q": name, "limit": 1})
        if resp.status_code == 429:
            await asyncio.sleep(30)
            resp = await client.get(url_a, params={"q": name, "limit": 1})
        if resp.status_code == 200:
            data = resp.json().get("data", [])
            if data:
                return data[0]
    except Exception as exc:
        logger.warning("r/%s about Option A error: %s", name, exc)

    try:
        resp = await client.get(url_b, params={"name": name})
        if resp.status_code == 429:
            await asyncio.sleep(30)
            resp = await client.get(url_b, params={"name": name})
        if resp.status_code == 200:
            data = resp.json().get("data", [])
            if data:
                return data[0]
    except Exception as exc:
        logger.warning("r/%s about Option B error: %s", name, exc)
        
    return {}

async def fetch_posts_page(
    subreddit: str,
    after_ts: float,
    before_ts: float,
    client: httpx.AsyncClient,
    job: BulkJob,
    after_id: Optional[str] = None,
) -> dict:
    url = "https://arctic-shift.photon-reddit.com/api/posts/search"
    
    # If a cursor is provided, it's the created_utc of the last post.
    # Use it to paginate backwards in time.
    current_before = float(after_id) if after_id else before_ts
    
    params: dict[str, Any] = {
        "subreddit": subreddit,
        "after": int(after_ts),
        "before": int(current_before),
        "limit": 100,
        "sort": "desc"
    }

    for attempt in range(1, 3):
        try:
            resp = await client.get(url, params=params, timeout=20.0)
            if resp.status_code == 429:
                wait = 30
                msg = f"r/{subreddit}: Arctic Shift rate limit. Waiting {wait}s..."
                logger.warning(msg)
                job.push_log(msg)
                job.substatus = msg
                await asyncio.sleep(wait)
                job.substatus = None
                continue
            
            if resp.status_code != 200:
                logger.warning("r/%s posts page error: HTTP %s", subreddit, resp.status_code)
                return {"error": resp.status_code}
            
            data = resp.json()
            posts = data.get("data", [])
            
            next_cursor = None
            if len(posts) == 100:
                # Use the created_utc of the last post as the cursor for the next page
                last_post = posts[-1]
                # subtract 1 to ensure we don't fetch the same post again
                next_cursor = str(int(last_post.get("created_utc", 0)) - 1)
                
            return {
                "posts": posts,
                "next_cursor": next_cursor
            }
        except Exception as exc:
            logger.warning("r/%s posts page exception: %s", subreddit, exc)
            if attempt == 2:
                return {"error": "Exception"}
            await asyncio.sleep(5)
    return {"error": "Max retries"}

def build_comment_tree(flat_comments: list[dict], limit: int | str) -> list[dict]:
    comment_map = {}
    for fc in flat_comments:
        c_id = fc.get("id")
        if not c_id: continue
        
        body = fc.get("body") or ""
        if body == "[deleted]": body = "[removed]"
        
        comment_map[c_id] = {
            "id": c_id,
            "parent_id": fc.get("parent_id", ""),
            "author": fc.get("author") or "[deleted]",
            "body": body.strip(),
            "score": fc.get("score", 0),
            "posted_at": format_timestamp(fc.get("created_utc", 0)),
            "depth": 0,
            "media_note": detect_comment_media(body),
            "replies": []
        }

    top_level = []
    
    for c_id, node in comment_map.items():
        pid = node["parent_id"]
        if pid.startswith("t3_"):
            top_level.append(node)
        elif pid.startswith("t1_"):
            parent_cid = pid[3:]
            if parent_cid in comment_map:
                node["depth"] = comment_map[parent_cid]["depth"] + 1
                comment_map[parent_cid]["replies"].append(node)
            else:
                pass
    
    def sort_tree(nodes):
        nodes.sort(key=lambda x: x["score"], reverse=True)
        for n in nodes:
            sort_tree(n["replies"])
            
    sort_tree(top_level)
    
    if str(limit).lower() != "all":
        try:
            lim = int(limit)
            top_level = top_level[:lim]
        except:
            pass
            
    def clean_tree(nodes):
        for n in nodes:
            n.pop("id", None)
            n.pop("parent_id", None)
            clean_tree(n["replies"])
            
    clean_tree(top_level)
    
    return top_level

async def fetch_comments(post_id: str, sort: str, limit: int | str, client: httpx.AsyncClient) -> list[dict]:
    url = "https://arctic-shift.photon-reddit.com/api/comments/search"
    api_sort = sort if sort in ("desc", "asc") else "desc"
    clean_id = post_id.replace("t3_", "").replace("t1_", "")
    params = {
        "link_id": clean_id,
        "limit": 100,
        "sort": api_sort
    }
    
    all_comments = []
    cursor = None
    
    for _ in range(5):
        if cursor: params["after_id"] = cursor
        try:
            resp = await client.get(url, params=params, timeout=20.0)
            if resp.status_code == 429:
                await asyncio.sleep(30)
                continue
            if resp.status_code != 200:
                break
            data = resp.json()
            items = data.get("data", [])
            all_comments.extend(items)
            cursor = data.get("metadata", {}).get("after_id")
            if not cursor: break
            await asyncio.sleep(COMMENT_DELAY_SEC)
        except Exception as e:
            logger.warning("fetch_comments error for %s: %s", post_id, e)
            break

    return build_comment_tree(all_comments, limit)

# ── Main entry point ───────────────────────────────────────────────────────────

async def bulk_extract_subreddits(
    subreddits: list[str],
    comment_sort: str = "top",
    comment_limit: int | str = 25,
    post_limit_per_subreddit: int = 500,
    years_back: float = 2,
    job_id: Optional[str] = None,
    output_dir: str = "output",
) -> dict:
    started_at = time.monotonic()

    job: BulkJob
    if job_id and job_id in _bulk_jobs:
        job = _bulk_jobs[job_id]
    else:
        job = BulkJob(job_id=job_id or "local", subreddits=list(subreddits))
        if job_id:
            _bulk_jobs[job_id] = job

    job.total_subreddits = len(subreddits)
    job.started_at       = started_at

    cutoff_dt  = datetime.now(tz=timezone.utc) - timedelta(days=365 * years_back)
    cutoff_utc = cutoff_dt.timestamp()

    logger.info(
        "[BulkExtract] Started. %d subreddit(s). Cutoff: %s. "
        "Post limit/sub: %d. Comment sort: %s, limit: %s.",
        len(subreddits),
        cutoff_dt.strftime("%Y-%m-%d"),
        post_limit_per_subreddit,
        comment_sort,
        comment_limit,
    )

    os.makedirs(output_dir, exist_ok=True)

    subreddits_skipped:       list[dict] = []
    docx_files:               list[str]  = []
    total_posts_extracted:    int        = 0
    total_comments_extracted: int        = 0
    all_sub_results: list[dict] = []

    job.status = STATUS_COLLECTING
    plan: list[tuple[str, dict, list[dict], bool]] = []

    async with httpx.AsyncClient(
        headers=ARCTIC_SHIFT_HEADERS,
        timeout=20.0,
        follow_redirects=True,
    ) as client:

        for sub_name in subreddits:
            job.current_subreddit  = sub_name
            job.subreddit_index   += 1

            msg = f"r/{sub_name}: Fetching about via Arctic Shift…"
            logger.info("[BulkExtract] %s", msg)
            job.push_log(msg)
            about_raw = await fetch_subreddit_info(sub_name, client)

            if not about_raw:
                msg = f"r/{sub_name}: Subreddit info unavailable, using defaults. Continuing with post extraction."
                logger.warning("[BulkExtract] %s", msg)
                job.push_log(msg)
                about = {
                    "title": f"r/{sub_name}",
                    "public_description": "Description not available",
                    "subscribers": "Unknown",
                    "display_name": sub_name
                }
            else:
                msg = f"r/{sub_name}: {about_raw.get('subscribers', 'Unknown')} subscribers. Fetching posts…"
                logger.info("[BulkExtract] %s", msg)
                job.push_log(msg)
                about = about_raw

            cursor = None
            all_posts = []
            limit_hit = False
            posts_endpoint_error = False
            posts_endpoint_status = None

            while True:
                job.substatus = f"r/{sub_name}: Fetching posts… (collected {len(all_posts)} so far)"
                page = await fetch_posts_page(sub_name, cutoff_utc, time.time(), client, job, cursor)
                
                if page is None or "error" in page:
                    posts_endpoint_error = True
                    posts_endpoint_status = page.get("error") if page else "Unknown"
                    break
                
                posts = page.get("posts", [])
                all_posts.extend(posts)
                
                if not page.get("next_cursor"):
                    break
                
                if len(all_posts) >= post_limit_per_subreddit:
                    all_posts = all_posts[:post_limit_per_subreddit]
                    limit_hit = True
                    break
                
                cursor = page.get("next_cursor")
                await asyncio.sleep(LISTING_DELAY_SEC)

            job.substatus = None

            if posts_endpoint_error:
                reason = f"Posts API returned error {posts_endpoint_status}."
                subreddits_skipped.append({"name": sub_name, "reason": reason})
                msg = f"r/{sub_name}: {reason} Skipping."
                logger.warning("[BulkExtract] %s", msg)
                job.push_log(msg)
                continue

            found = len(all_posts)
            if found == 0:
                reason = f"No posts found in date range."
                subreddits_skipped.append({"name": sub_name, "reason": reason})
                msg = f"r/{sub_name}: {reason} Skipping."
                logger.info("[BulkExtract] %s", msg)
                job.push_log(msg)
                continue
            
            # Attempt to extract missing subscriber count from first post
            if about.get("subscribers") == "Unknown" and len(all_posts) > 0:
                first_post = all_posts[0]
                if "subreddit_subscribers" in first_post:
                    about["subscribers"] = first_post["subreddit_subscribers"]

            if limit_hit:
                msg = f"r/{sub_name}: Found posts. Will extract {found} (limit reached)."
            else:
                msg = f"r/{sub_name}: Found {found} posts in the past {years_back} year(s)."
            
            logger.info("[BulkExtract] %s", msg)
            job.push_log(msg)
            plan.append((sub_name, about, all_posts, limit_hit))

        job.total_posts_all_subs = sum(len(posts) for _, _, posts, _ in plan)
        job.total_posts_done     = 0
        job.status               = STATUS_EXTRACTING

        logger.info(
            "[BulkExtract] Phase 1 complete. %d subreddit(s) to process. %d total posts.",
            len(plan), job.total_posts_all_subs
        )

        _rl_gate = asyncio.Event()
        _rl_gate.set()

        sem = asyncio.Semaphore(POST_SEMAPHORE_N)

        for sub_idx, (sub_name, about, posts, limit_hit) in enumerate(plan, start=1):

            job.current_subreddit    = sub_name
            job.subreddit_index      = sub_idx
            job.posts_done_this_sub  = 0
            job.total_posts_this_sub = len(posts)

            msg = f"r/{sub_name} [{sub_idx}/{len(plan)}]: Extracting comments for {len(posts)} posts…"
            logger.info("[BulkExtract] %s", msg)
            job.push_log(msg)

            sub_extracted: list[dict] = []
            sub_lock = asyncio.Lock()

            async def _extract_one(post_data: dict) -> None:
                post_id = post_data.get("id", "")
                permalink = post_data.get("permalink", f"/r/{sub_name}/comments/{post_id}/")
                post_url = f"https://www.reddit.com{permalink}"

                await _rl_gate.wait()

                async with sem:
                    def _status_cb(m: Optional[str]) -> None:
                        job.substatus = m
                        if m and "429" in m:
                            if _rl_gate.is_set(): _rl_gate.clear()
                        elif m is None and not _rl_gate.is_set():
                            _rl_gate.set()

                    result: Optional[dict] = None
                    try:
                        comments = await fetch_comments(post_id, comment_sort, comment_limit, client)
                        
                        body = safe_body(post_data, key="selftext")
                        upvote_ratio_pct = f"{int(post_data.get('upvote_ratio', 0.0) * 100)}%"
                        
                        result = {
                            "post": {
                                "title": post_data.get("title", ""),
                                "body": body,
                                "subreddit": sub_name,
                                "author": safe_author(post_data),
                                "score": post_data.get("score", 0),
                                "upvote_ratio": upvote_ratio_pct,
                                "num_comments": post_data.get("num_comments", 0),
                                "flair": post_data.get("link_flair_text"),
                                "posted_at": format_timestamp(post_data.get("created_utc", 0)),
                                "url": post_url,
                                "media_note": detect_media_note(post_data, body)
                            },
                            "comments": comments,
                            "sort_used": comment_sort,
                            "limit_used": comment_limit,
                            "total_comments_extracted": _count_comments(comments)
                        }
                    except Exception as exc:
                        msg = f"r/{sub_name}: Error fetching comments for {post_url}: {exc}. Inserting placeholder."
                        logger.warning("[BulkExtract] %s", msg)
                        job.push_log(msg)

                async with sub_lock:
                    if result is not None:
                        sub_extracted.append(result)
                    else:
                        sub_extracted.append({
                            "post": {
                                "title": f"[Post could not be fetched: {post_url}]",
                                "body": "",
                                "subreddit": sub_name,
                                "author": "[unknown]",
                                "score": 0,
                                "upvote_ratio": "0%",
                                "num_comments": 0,
                                "flair": None,
                                "posted_at": "",
                                "url": post_url,
                                "media_note": None,
                            },
                            "comments": [],
                            "sort_used": comment_sort,
                            "limit_used": comment_limit,
                            "total_comments_extracted": 0,
                        })

                    job.posts_done_this_sub += 1
                    job.total_posts_done    += 1

                    elapsed = time.monotonic() - started_at
                    done    = job.total_posts_done
                    total   = job.total_posts_all_subs
                    if done > 0 and total > 0:
                        avg_sec      = elapsed / done
                        remaining    = total - done
                        job.eta_minutes = round((avg_sec * remaining) / 60)
                        job.percent     = round((done / total) * 100, 2)

                    msg = (
                        f"r/{sub_name}: {job.posts_done_this_sub}/{job.total_posts_this_sub} posts | "
                        f"Overall: {job.total_posts_done}/{job.total_posts_all_subs} ({job.percent:.1f}%) | ETA: {job.eta_minutes} min"
                    )
                    logger.info("[BulkExtract] %s", msg)
                    job.push_log(msg)

            tasks = [_extract_one(p) for p in posts]
            await asyncio.gather(*tasks, return_exceptions=True)

            successful = [p for p in sub_extracted if not p["post"]["title"].startswith("[Post could not be fetched")]
            failed_count = len(sub_extracted) - len(successful)
            sub_comments = sum(p.get("total_comments_extracted", 0) for p in successful)
            total_posts_extracted    += len(successful)
            total_comments_extracted += sub_comments

            msg = f"r/{sub_name}: {len(successful)} extracted | {failed_count} failed/skipped | {sub_comments} comments."
            logger.info("[BulkExtract] %s", msg)
            job.push_log(msg)

            all_sub_results.append({
                "name":        sub_name,
                "about":       about,
                "posts":       sub_extracted,
                "total_found": len(posts),
                "limit_hit":   limit_hit,
            })

            job.subreddits_processed += 1
            job.status    = STATUS_EXTRACTING
            job.substatus = None

    if all_sub_results:
        job.status    = STATUS_GENERATING
        job.substatus = "Generating DOCX file(s)…"
        msg = f"Phase 3: Generating DOCX for {len(all_sub_results)} subreddit(s)."
        logger.info("[BulkExtract] %s", msg)
        job.push_log(msg)

        subreddit_data = {
            "subreddits": all_sub_results,
            "meta": {
                "comment_sort":  comment_sort,
                "comment_limit": comment_limit,
                "years_back":    years_back,
            },
        }
        try:
            docx_files = await _gen_bulk_docx(
                subreddit_data=subreddit_data,
                output_dir=output_dir,
                max_posts_per_file=200,
            )
            logger.info("[BulkExtract] DOCX generation complete: %d file(s).", len(docx_files))
        except Exception as exc:
            logger.error("[BulkExtract] DOCX generation failed: %s", exc)

        job.substatus = None

    elapsed_total   = time.monotonic() - started_at
    elapsed_minutes = round(elapsed_total / 60, 2)

    job.status                   = STATUS_COMPLETE
    job.docx_files               = docx_files
    job.total_posts_extracted    = total_posts_extracted
    job.total_comments_extracted = total_comments_extracted
    job.subreddits_skipped       = subreddits_skipped
    job.percent                  = 100.0

    msg = f"Job complete in {elapsed_minutes} min. {len(docx_files)} file(s) generated."
    logger.info("[BulkExtract] %s", msg)
    job.push_log(msg)
    job.completed_at = time.monotonic()

    return {
        "subreddits_processed":     job.subreddits_processed,
        "subreddits_skipped":       subreddits_skipped,
        "total_posts_extracted":    total_posts_extracted,
        "total_comments_extracted": total_comments_extracted,
        "docx_files":               docx_files,
        "extraction_time_minutes":  elapsed_minutes,
    }
