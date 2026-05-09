"""
reddit_extractor.py
-------------------
Extracts structured data from Reddit posts using Reddit's PUBLIC JSON API.

NO credentials required. Reddit exposes every public post as JSON by
appending ".json" to the URL:

  https://www.reddit.com/r/python/comments/abc123/title/.json

Two top-level objects are returned:
  response[0]  → the post (submission listing)
  response[1]  → the comment tree (comment listing)

Uses httpx for fully async HTTP requests.
Adds a 1–2 second polite delay between requests.
"""

import asyncio
import logging
import os
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Callable

import httpx

# ── Logging ────────────────────────────────────────────────────────────────────
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# ── HTTP constants ──
REDDIT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,image/avif,"
        "image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

REQUEST_TIMEOUT  = 20.0   # seconds per request (raised: large threads take longer to serialize)

# Reddit's public JSON API allows roughly 10 requests/minute for anonymous
# (no-cookie) traffic.  6–12 s between requests keeps us at ~6–10 req/min,
# well inside that budget.  The jitter also makes the pattern less mechanical.
DELAY_MIN_SEC    = 6.0
DELAY_MAX_SEC    = 12.0

# ── Sort name mapping ──────────────────────────────────────────────────────────
# Reddit's public JSON API does not recognise `sort=best`; its internal name is
# `confidence`.  All other sort labels happen to match the API's own names.
SORT_MAP = {
    "best":          "confidence",
    "top":           "top",
    "new":           "new",
    "controversial": "controversial",
    "old":           "old",
    "qa":            "qa",
}

# ── Known image / video URL patterns (for media-type detection) ────────────────
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".gifv"}
_IMAGE_HOSTS      = {"i.redd.it", "i.imgur.com", "imgur.com", "preview.redd.it"}


# ── Pure / unit-testable helpers ───────────────────────────────────────────────

def build_json_url(post_url: str, sort: str, limit: int | str) -> str:
    """
    Converts a Reddit post URL into its public JSON API equivalent.
    """
    from urllib.parse import urlparse, urlunparse, urlencode
    
    parsed = urlparse(post_url)
    path = parsed.path
    if path.endswith("/"):
        path = path[:-1]
    
    # Ensure it ends with .json
    if not path.lower().endswith(".json"):
        path += ".json"
        
    # Reconstruct the base URL without query params/fragment
    clean_base = urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))
    
    # Build query parameters
    params = {}

    # Translate the frontend sort label to Reddit's internal API parameter name.
    # e.g. "best" → "confidence" so Reddit doesn't silently fall back to "top".
    api_sort = SORT_MAP.get(sort.lower(), "confidence")
    params["sort"] = api_sort

    # raw_json=1 disables Reddit's CDN HTML-encoding pass and reduces the chance
    # of the CDN serving a stale cached response that ignores the sort parameter.
    params["raw_json"] = 1

    # Force max comments and depth per prompt to squeeze maximum data
    # out of the single permitted HTTP request.
    params["limit"] = 500
    params["depth"] = 10
        
    query_str = urlencode(params)
    final_url = f"{clean_base}?{query_str}"
    return final_url


def format_timestamp(utc_epoch: float) -> str:
    """
    Converts a UTC epoch float to "YYYY-MM-DD HH:MM UTC".

    Args:
        utc_epoch: Seconds since Unix epoch (UTC).

    Returns:
        Human-readable UTC timestamp string.

    Example:
        >>> format_timestamp(1700000000.0)
        '2023-11-14 22:13 UTC'
    """
    dt = datetime.fromtimestamp(utc_epoch, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def safe_author(data: dict) -> str:
    """
    Extracts author field; returns '[deleted]' when absent or deleted.

    Args:
        data: Reddit API object dict (post or comment).

    Returns:
        Author username string.
    """
    author = data.get("author") or ""
    if not author or author in ("[deleted]", ""):
        return "[deleted]"
    return author


def safe_body(data: dict, key: str = "body") -> str:
    """
    Extracts body text from a dict; normalises None / '[removed]' / '[deleted]'.

    Args:
        data: Reddit API object dict.
        key:  Which key to read ('body' for comments, 'selftext' for posts).

    Returns:
        Stripped body string.
    """
    text = data.get(key) or ""
    return text.strip()


def detect_media_note(post_data: dict, body: str) -> Optional[str]:
    """
    Determines post media note from Reddit JSON fields as requested.
    """
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

    # Fallback to pure logic if hint is empty
    if not body.strip() and not hint:
        return "[POST CONTAINS: Link or external content — not copied]"
        
    return None


def detect_comment_media(body: str) -> Optional[str]:
    """
    Scans comment text for embedded media URLs via regex.
    """
    notes = []
    
    image_pattern = re.compile(
        r"https?://\S+\.(?:jpg|jpeg|png|gif|webp|bmp)",
        re.IGNORECASE,
    )
    if image_pattern.search(body):
        notes.append("[CONTAINS: Image link — not copied]")
        
    video_pattern = re.compile(
        r"https?://(www\.)?(youtube\.com|youtu\.be|streamable\.com|v\.redd\.it)/\S+",
        re.IGNORECASE,
    )
    if video_pattern.search(body):
        notes.append("[CONTAINS: Video link — not copied]")
        
    if not notes:
        return None
    return " ".join(notes)


# ── Comment tree parser ────────────────────────────────────────────────────────

def _parse_comment(child: dict, depth: int) -> Optional[dict]:
    """
    Recursively parses a single comment node from Reddit's JSON comment tree.
    """
    kind = child.get("kind", "")
    data = child.get("data", {})

    if kind == "more":
        # Collapsed comment stub — replace with placeholder per prompt
        count = data.get("count", 0)
        return {
            "author": "",
            "body": f"[{count} more replies not loaded]",
            "score": 0,
            "posted_at": "",
            "depth": depth,
            "media_note": None,
            "replies": [],
        }

    if kind != "t1":
        return None

    author     = safe_author(data)
    body       = safe_body(data, key="body")
    if body == "[deleted]":
        body = "[removed]"  # handle standard reddit display conventions
        
    score      = data.get("score") or 0
    posted_at  = format_timestamp(data.get("created_utc", 0))
    media_note = detect_comment_media(body)

    comment = {
        "author": author,
        "body": body,
        "score": score,
        "posted_at": posted_at,
        "depth": depth,
        "media_note": media_note,
        "replies": [],
    }

    # ── Recurse into replies (unbounded) ───────────────────────────────────────
    replies_obj = data.get("replies")
    if isinstance(replies_obj, dict):
        reply_children = replies_obj.get("data", {}).get("children", [])
        for reply_child in reply_children:
            parsed = _parse_comment(reply_child, depth + 1)
            if parsed is not None:
                comment["replies"].append(parsed)

    return comment


def parse_comments(comments_listing: dict) -> list[dict]:
    """
    Parses the full comment tree from Reddit's response[1] object.
    """
    children = comments_listing.get("data", {}).get("children", [])
    comments = []

    for child in children:
        parsed = _parse_comment(child, depth=0)
        if parsed is not None:
            comments.append(parsed)

    return comments


# ── HTTP fetch ─────────────────────────────────────────────────────────────────

async def fetch_post_json(
    json_url: str,
    client: httpx.AsyncClient,
    status_callback: Optional[Callable[[Optional[str]], None]] = None,
) -> Optional[list]:
    """
    Fetches and returns the parsed JSON list from Reddit's public API.

    Args:
        json_url: The `.json`-suffixed Reddit URL.
        client:   Shared httpx.AsyncClient instance.

    Returns:
        The parsed JSON list [post_listing, comments_listing],
        or None if the request failed or returned unexpected data.
    """
    logger.info("Fetching JSON: %s", json_url)
    max_retries = 5
    backoff_delays = [5, 15, 30, 60, 120]

    for attempt in range(1, max_retries + 1):
        try:
            response = await client.get(json_url, timeout=REQUEST_TIMEOUT)

            if response.status_code == 403:
                logger.warning("403 Forbidden — subreddit is private or post removed: %s", json_url)
                return None

            if response.status_code == 404:
                logger.warning("404 Not Found — post deleted: %s", json_url)
                return None

            if response.status_code == 429:
                delay = backoff_delays[attempt - 1] if attempt <= len(backoff_delays) else 120
                logger.warning("429 Too Many Requests — rate limited. Waiting %ds (attempt %d/%d)…", delay, attempt, max_retries)
                for remaining in range(delay, 0, -1):
                    if status_callback:
                        status_callback(f"429 Too Many Requests — rate limited. Pausing {remaining}s (attempt {attempt}/{max_retries})…")
                    await asyncio.sleep(1)
                # Always clear the substatus / reopen the gate after backoff,
                # even if we are about to retry — the gate in main.py relies on
                # this None signal to know the backoff window has closed.
                if status_callback:
                    status_callback(None)
                continue

            response.raise_for_status()

            # ── Reddit proactive rate-limit header check ──────────────────────
            rem_str = response.headers.get("X-Ratelimit-Remaining")
            res_str = response.headers.get("X-Ratelimit-Reset")

            if rem_str and res_str:
                try:
                    remaining = float(rem_str)
                    reset_sec = float(res_str)
                    if remaining < 15.0:
                        logger.warning("Rate limit nearly reached (%.1f left). Pausing %.0fs…", remaining, reset_sec)
                        for sec_left in range(int(reset_sec), 0, -1):
                            if status_callback:
                                status_callback(f"Rate limit nearly reached ({remaining:.1f} left). Pausing {sec_left}s…")
                            await asyncio.sleep(1)
                        if status_callback:
                            status_callback(None)
                except ValueError:
                    pass

            data = response.json()
            if not isinstance(data, list) or len(data) < 2:
                logger.warning("Unexpected JSON structure at: %s", json_url)
                return None

            return data

        except httpx.TimeoutException:
            retry_delay = 10
            if attempt < max_retries:
                msg = f"Request timed out (attempt {attempt}/{max_retries}). Retrying in {retry_delay}s…"
                logger.warning(msg)
                if status_callback:
                    status_callback(msg)
                await asyncio.sleep(retry_delay)
                if status_callback:
                    status_callback(None)
                continue
            logger.warning("Request timed out after %d attempts: %s", max_retries, json_url)
            return None

        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code in (500, 502, 503, 504) and attempt < max_retries:
                msg = f"Server error {code} (attempt {attempt}/{max_retries}). Retrying in 10s…"
                logger.warning(msg)
                if status_callback:
                    status_callback(msg)
                await asyncio.sleep(10)
                if status_callback:
                    status_callback(None)
                continue
            logger.warning("HTTP error %s for: %s", code, json_url)
            return None

        except Exception as exc:
            logger.error("Unexpected error fetching %s: %s", json_url, exc)
            return None

    logger.warning("Failed to fetch %s after %d attempts.", json_url, max_retries)
    return None


# ── Main extraction entry point ────────────────────────────────────────────────

def _count_comments(comments: list[dict]) -> int:
    count = len(comments)
    for c in comments:
        count += _count_comments(c["replies"])
    return count


async def _arctic_shift_fallback(url: str, post_id: str, sort: str, limit: int | str, client: httpx.AsyncClient, status_callback) -> Optional[dict]:
    # 1. Fetch post
    post_url = "https://arctic-shift.photon-reddit.com/api/posts/search"
    try:
        p_resp = await client.get(post_url, params={"id": post_id, "limit": 1})
        if p_resp.status_code != 200:
            return None
        p_data = p_resp.json().get("data", [])
        if not p_data:
            return None
        p = p_data[0]
    except Exception as e:
        logger.warning("Arctic shift post fetch failed: %s", e)
        return None
        
    title = p.get("title", "")
    body = safe_body(p, key="selftext")
    media_note = detect_media_note(p, body)
    upvote_ratio_pct = f"{int(p.get('upvote_ratio', 0.0) * 100)}%"

    post = {
        "title": title,
        "body": body,
        "subreddit": p.get("subreddit", ""),
        "author": safe_author(p),
        "score": p.get("score", 0),
        "upvote_ratio": upvote_ratio_pct,
        "num_comments": p.get("num_comments", 0),
        "flair": p.get("link_flair_text") or None,
        "posted_at": format_timestamp(p.get("created_utc", 0)),
        "url": url,
        "media_note": media_note,
    }

    # 2. Fetch comments
    c_url = "https://arctic-shift.photon-reddit.com/api/comments/search"
    api_sort = sort if sort in ("top", "desc", "asc") else "top"
    c_params = {
        "link_id": f"t3_{post_id}" if not post_id.startswith("t3_") else post_id,
        "limit": 100,
        "sort": api_sort
    }
    all_comments = []
    cursor = None
    for _ in range(5):
        if cursor: c_params["after_id"] = cursor
        try:
            c_resp = await client.get(c_url, params=c_params, timeout=20.0)
            if c_resp.status_code == 429:
                await asyncio.sleep(30)
                continue
            if c_resp.status_code != 200:
                break
            c_data = c_resp.json()
            items = c_data.get("data", [])
            all_comments.extend(items)
            cursor = c_data.get("metadata", {}).get("after_id")
            if not cursor: break
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.warning("Arctic shift comments fetch failed: %s", e)
            break
            
    comment_map = {}
    for fc in all_comments:
        c_id = fc.get("id")
        if not c_id: continue
        c_body = fc.get("body") or ""
        if c_body == "[deleted]": c_body = "[removed]"
        comment_map[c_id] = {
            "id": c_id,
            "parent_id": fc.get("parent_id", ""),
            "author": fc.get("author") or "[deleted]",
            "body": c_body.strip(),
            "score": fc.get("score", 0),
            "posted_at": format_timestamp(fc.get("created_utc", 0)),
            "depth": 0,
            "media_note": detect_comment_media(c_body),
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
    
    return {
        "post": post,
        "comments": top_level,
        "sort_used": sort,
        "limit_used": limit,
        "total_comments_extracted": _count_comments(top_level),
    }


async def extract_post(
    url: str,
    sort: str = "best",
    limit: int | str = 25,
    client: Optional[httpx.AsyncClient] = None,
    status_callback: Optional[Callable[[Optional[str]], None]] = None,
) -> Optional[dict]:
    """
    Main async entry point. Fetches and extracts all data for a Reddit post.
    """
    if str(limit).lower() == "all":
        logger.warning("WARNING: 'all' selected. Popular posts may have thousands of comments. This will be slow and produce a very large file.")

    json_url = build_json_url(url, sort=sort, limit=limit)
    api_sort = SORT_MAP.get(sort.lower(), "confidence")
    logger.info(
        "Accessing post: %s [Sort: %s → API: %s, Limit: %s]",
        url, sort, api_sort, limit,
    )
    logger.info("Constructed JSON URL: %s", json_url)
    own_client = client is None

    if own_client:
        client = httpx.AsyncClient(
            headers=REDDIT_HEADERS,
            timeout=20.0,
            follow_redirects=True,
            http2=False,
        )

    try:
        raw = await fetch_post_json(json_url, client, status_callback=status_callback)
        if raw is None:
            logger.info("Reddit API failed or returned 403/404. Falling back to Arctic Shift for %s", url)
            match = re.search(r'/comments/([^/]+)', url)
            if not match:
                logger.warning("Could not parse post ID from URL: %s", url)
                return None
            post_id = match.group(1)
            return await _arctic_shift_fallback(url, post_id, sort, limit, client, status_callback)

        post_listing, comments_listing = raw[0], raw[1]

        # ── Locate the post data object ────────────────────────────────────────
        post_children = post_listing.get("data", {}).get("children", [])
        if not post_children:
            logger.warning("No post data found in listing for: %s", url)
            return None

        p = post_children[0].get("data", {})

        # Guard against deleted/removed posts that return empty placeholders
        title = p.get("title")
        if not title:
            logger.warning("Post appears deleted or removed: %s", url)
            return None

        body = safe_body(p, key="selftext")
        media_note = detect_media_note(p, body)
        upvote_ratio_pct = f"{int(p.get('upvote_ratio', 0.0) * 100)}%"

        post = {
            "title": title,
            "body": body,
            "subreddit": p.get("subreddit", ""),
            "author": safe_author(p),
            "score": p.get("score", 0),
            "upvote_ratio": upvote_ratio_pct,
            "num_comments": p.get("num_comments", 0),
            "flair": p.get("link_flair_text") or None,
            "posted_at": format_timestamp(p.get("created_utc", 0)),
            "url": url,
            "media_note": media_note,
        }

        # ── Extract comment tree ───────────────────────────────────────────────
        comments = parse_comments(comments_listing)
        
        return {
            "post": post,
            "comments": comments,
            "sort_used": sort,
            "limit_used": limit,
            "total_comments_extracted": _count_comments(comments),
        }

    finally:
        if own_client:
            await client.aclose()


async def extract_multiple_posts(
    urls: list[str],
    sort: str = "best",
    limit: int | str = 25,
    delay_min: float = DELAY_MIN_SEC,
    delay_max: float = DELAY_MAX_SEC,
) -> list[dict]:
    """
    Extracts data for a list of Reddit post URLs with polite delays.

    Uses a single shared httpx.AsyncClient across all requests for
    connection reuse (more efficient, lower overhead).

    Args:
        urls:       List of Reddit post URL strings.
        max_depth:  Reply depth limit (default 3).
        delay_min:  Minimum delay between requests in seconds.
        delay_max:  Maximum delay between requests in seconds.

    Returns:
        List of successfully extracted post dicts (may be shorter than urls).
    """
    results: list[dict] = []

    async with httpx.AsyncClient(
        headers=REDDIT_HEADERS,
        timeout=20.0,
        follow_redirects=True,
        http2=False,
    ) as client:
        for i, url in enumerate(urls, start=1):
            logger.info("Processing post %d / %d: %s", i, len(urls), url)

            post = await extract_post(url, sort=sort, limit=limit, client=client)
            if post is not None:
                results.append(post)

            # Polite delay between requests (skip after the last one)
            if i < len(urls):
                delay = random.uniform(delay_min, delay_max)
                logger.debug("Waiting %.2fs before next request...", delay)
                await asyncio.sleep(delay)

    logger.info(
        "Extraction complete: %d / %d posts successfully extracted.",
        len(results), len(urls),
    )
    return results


# ── CLI convenience runner ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    import sys

    test_url = (
        sys.argv[1] if len(sys.argv) > 1
        else "https://www.reddit.com/r/Python/comments/zr3pmc/"
             "what_are_your_go_to_python_libraries/"
    )

    async def _main():
        post = await extract_post(test_url)
        if post:
            print(json.dumps(post, indent=2, ensure_ascii=False))
            print(f"\nExtracted {len(post['comments'])} top-level comments.")
        else:
            print("Extraction failed. Check logs above.")

    asyncio.run(_main())
