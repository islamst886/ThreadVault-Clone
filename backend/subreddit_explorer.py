"""
subreddit_explorer.py  (v2 — paginated streaming)
--------------------------------------------------
Queries Reddit's public JSON API (zero credentials) to power the
Community Explorer tab in ThreadVault.

Key async generators (usable in StreamingResponse):
    paginate_popular(target=300)     → yields batches page-by-page
    paginate_new(target=200)
    paginate_search(query, target=200)

Detail fetcher:
    get_subreddit_details(subreddit_name) → enriched single-sub dict
"""

import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

import httpx

logger = logging.getLogger(__name__)

# ── HTTP headers ───────────────────────────────────────────────────────────────
# Full Chrome header set — Reddit CDN 403-blocks anything that looks like a bot.
_HEADERS = {
    "User-Agent":                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language":           "en-US,en;q=0.9",
    "Accept-Encoding":           "gzip, deflate, br",
    "Sec-Fetch-Dest":            "document",
    "Sec-Fetch-Mode":            "navigate",
    "Sec-Fetch-Site":            "none",
    "Sec-Fetch-User":            "?1",
    "Upgrade-Insecure-Requests": "1",
    "Connection":                "keep-alive",
}

REQUEST_TIMEOUT  = 20.0   # seconds per request
PAGINATION_DELAY = 1.5    # seconds between paginated requests (polite)

_BASE = "https://www.reddit.com"


# ── Low-level HTTP fetch ───────────────────────────────────────────────────────

async def _get(
    client: httpx.AsyncClient,
    url: str,
    params: Optional[dict] = None,
    retries: int = 1,
) -> Optional[dict]:
    """
    Fetch a Reddit JSON endpoint. Auto-retries once on 429.

    Returns parsed JSON dict, or None on any failure.
    """
    for attempt in range(retries + 1):
        try:
            resp = await client.get(url, params=params, timeout=REQUEST_TIMEOUT)

            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 10))
                logger.warning("429 rate-limited on %s — waiting %ds", url, wait)
                await asyncio.sleep(wait)
                continue

            if resp.status_code in (403, 404):
                logger.debug("Skipping %s — status %d", url, resp.status_code)
                return None

            resp.raise_for_status()
            return resp.json()

        except httpx.TimeoutException:
            logger.warning("Timeout %s (attempt %d/%d)", url, attempt + 1, retries + 1)
        except httpx.HTTPStatusError as exc:
            logger.warning("HTTP %d for %s", exc.response.status_code, url)
            return None
        except Exception as exc:
            logger.error("Unexpected error fetching %s: %s", url, exc)
            return None

    logger.warning("Giving up on %s after %d attempts", url, retries + 1)
    return None


# ── Tag computation ────────────────────────────────────────────────────────────

def _size_tag(members: int) -> str:
    if members >= 10_000_000: return "Massive"
    if members >=  1_000_000: return "Large"
    if members >=    100_000: return "Medium Size"
    if members >=     10_000: return "Small"
    if members >=      1_000: return "Micro"
    return "Tiny"


def _activity_tag(active: int) -> str:
    if active >= 10_000: return "Super Active"
    if active >=  1_000: return "High Activity"
    if active >=    100: return "Active"
    if active >=     10: return "Slow Activity"
    return "Quiet"


def _compute_tags(members: int, active: int, age_days: int) -> list[str]:
    """Returns 2–3 tags: exactly one size, one activity, optional age."""
    tags = [_size_tag(members), _activity_tag(active)]
    if age_days < 180:
        tags.append("New")
    elif age_days < 365:
        tags.append("Growing")
    return tags


# ── Subreddit data extraction ──────────────────────────────────────────────────

def _parse_subreddit(data: dict) -> Optional[dict]:
    """
    Converts a raw Reddit subreddit ``data`` block into a clean ThreadVault dict.

    Silently returns None for: NSFW, non-public, quarantined subreddits.
    """
    if data.get("over18"):
        return None
    if data.get("subreddit_type") not in ("public", None):
        return None
    if data.get("quarantine"):
        return None

    display_name = data.get("display_name", "")
    if not display_name:
        return None

    members    = int(data.get("subscribers") or 0)
    active_now = int(data.get("active_user_count") or 0)

    # Timestamps & age
    created_utc = data.get("created_utc") or 0
    try:
        created_dt  = datetime.fromtimestamp(float(created_utc), tz=timezone.utc)
        created_str = created_dt.strftime("%Y-%m-%d")
        age_days    = max((datetime.now(tz=timezone.utc) - created_dt).days, 1)
    except (ValueError, OSError):
        created_str = ""
        age_days    = 1   # avoid div-by-zero; won't surface an age tag

    # Derived metrics
    activity_ratio         = round(active_now / members * 100, 2) if members > 0 else 0.0
    estimated_daily_growth = round(members / age_days, 1)
    growth_score           = round((active_now * 10) + (members / age_days), 2)

    # Enhanced growth metrics (Change 2)
    import math
    engagement_pct         = activity_ratio  # same value, alias for clarity
    momentum_score         = round(engagement_pct * math.log10(members), 4) if members > 1 else 0.0
    size_adjusted_activity = round(active_now / (members / 1000), 2) if members > 0 else 0.0
    approx_weekly_members  = round(estimated_daily_growth * 7, 1)
    approx_monthly_members = round(estimated_daily_growth * 30, 1)

    # Icon — prefer icon_img, fall back to community_icon; strip query params
    icon_url = (data.get("icon_img") or data.get("community_icon") or "").split("?")[0]

    return {
        "name":                    f"r/{display_name}",
        "display_name":            display_name,
        "title":                   (data.get("title") or "").strip(),
        "description":             (data.get("public_description") or "").strip(),
        "long_description":        (data.get("description") or "").strip(),
        "members":                 members,
        "active_now":              active_now,
        "activity_ratio":          activity_ratio,
        "engagement_pct":          engagement_pct,
        "estimated_daily_growth":  estimated_daily_growth,
        "growth_score":            growth_score,
        "momentum_score":          momentum_score,
        "size_adjusted_activity":  size_adjusted_activity,
        "approx_weekly_members":   approx_weekly_members,
        "approx_monthly_members":  approx_monthly_members,
        "created":                 created_str,
        "age_days":                age_days,
        "url":                     f"https://reddit.com/r/{display_name}",
        "icon_url":                icon_url,
        "nsfw":                    False,
        "tags":                    _compute_tags(members, active_now, age_days),
        "posts_per_day":           None,
        "moderators_count":        None,
        "rules_count":             None,
    }


def _extract_listing(body: dict) -> list[dict]:
    """Extracts & parses all t5 (subreddit) children from a Reddit listing."""
    results = []
    for child in body.get("data", {}).get("children", []):
        if child.get("kind") != "t5":
            continue
        parsed = _parse_subreddit(child.get("data", {}))
        if parsed is not None:
            results.append(parsed)
    return results


# ── Pagination core async generator ───────────────────────────────────────────

async def _paginate(
    url: str,
    base_params: dict,
    target: int,
    after: Optional[str] = None,
    min_members: Optional[int] = None,
    max_members: Optional[int] = None,
    min_active:  Optional[int] = None,
    max_active:  Optional[int] = None,
) -> AsyncIterator[list[dict]]:
    """
    Core async generator: fetches Reddit listing pages until ``target`` unique
    public subreddits that PASS the optional member/active filters are collected,
    or Reddit's listing is exhausted.

    Server-side filtering means Load More consistently returns communities that
    match the filter rather than re-filtering a fixed pool client-side.

    Yields batches of matching subreddits progressively.
    Final item yielded is a sentinel: [{"__cursor__": "<str|None>"}]
    """
    def _passes(sub: dict) -> bool:
        m = sub["members"]
        a = sub["active_now"]
        if min_members is not None and m < min_members: return False
        if max_members is not None and m > max_members: return False
        if min_active  is not None and a < min_active:  return False
        if max_active  is not None and a > max_active:  return False
        return True

    seen:         set[str]       = set()
    total:        int            = 0
    final_cursor: Optional[str] = after

    async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True) as client:
        while total < target:
            params = {**base_params, "limit": 100}
            if final_cursor:
                params["after"] = final_cursor

            body = await _get(client, url, params=params)
            if not body:
                logger.warning("[Explorer] _paginate: empty response from %s", url)
                break

            data_block   = body.get("data", {})
            final_cursor = data_block.get("after")

            raw_batch = _extract_listing(body)

            # Deduplicate then apply server-side filter, honour target cap
            batch: list[dict] = []
            for sub in raw_batch:
                if sub["display_name"] in seen:
                    continue
                seen.add(sub["display_name"])
                if not _passes(sub):
                    continue                       # skip — doesn't match filter
                batch.append(sub)
                total += 1
                if total >= target:
                    break

            if batch:
                yield batch

            if not final_cursor or not raw_batch:
                final_cursor = None
                break

            if total < target:
                await asyncio.sleep(PAGINATION_DELAY)

    logger.info("[Explorer] _paginate done — collected %d (cursor=%s)", total, final_cursor)
    yield [{"__cursor__": final_cursor}]


# ── Public async generators ────────────────────────────────────────────────────

async def paginate_popular(
    target: int = 300, after: Optional[str] = None,
    min_members: Optional[int] = None, max_members: Optional[int] = None,
    min_active:  Optional[int] = None, max_active:  Optional[int] = None,
) -> AsyncIterator[list[dict]]:
    """Yields batches of popular subreddits that pass optional size/activity filters."""
    url = f"{_BASE}/subreddits/popular.json"
    logger.info("[Explorer] popular (target=%d after=%s mins=%s maxs=%s mina=%s maxa=%s)",
                target, after, min_members, max_members, min_active, max_active)
    async for batch in _paginate(url, {"raw_json": 1}, target, after,
                                  min_members, max_members, min_active, max_active):
        yield batch


async def paginate_new(
    target: int = 200, after: Optional[str] = None,
    min_members: Optional[int] = None, max_members: Optional[int] = None,
    min_active:  Optional[int] = None, max_active:  Optional[int] = None,
) -> AsyncIterator[list[dict]]:
    """Yields batches of newly created subreddits that pass optional filters."""
    url = f"{_BASE}/subreddits/new.json"
    logger.info("[Explorer] new (target=%d after=%s)", target, after)
    async for batch in _paginate(url, {"raw_json": 1}, target, after,
                                  min_members, max_members, min_active, max_active):
        yield batch


async def paginate_search(
    query: str, target: int = 200, after: Optional[str] = None,
    min_members: Optional[int] = None, max_members: Optional[int] = None,
    min_active:  Optional[int] = None, max_active:  Optional[int] = None,
) -> AsyncIterator[list[dict]]:
    """Yields batches of subreddits matching ``query`` that pass optional filters."""
    url    = f"{_BASE}/subreddits/search.json"
    params = {"q": query, "include_over_18": "false", "raw_json": 1}
    logger.info("[Explorer] search '%s' (target=%d after=%s)", query, target, after)
    async for batch in _paginate(url, params, target, after,
                                  min_members, max_members, min_active, max_active):
        yield batch


async def paginate_growing(
    target: int = 500,
) -> AsyncIterator[list[dict]]:
    """
    Growing tab (Change 3):
    - Fetches from /subreddits/popular.json (up to 500 communities)
    - Also fetches broad search queries to cast a wider net:
      ["new", "growing", "community", "discussion"]
    - Hard filters: age>=180 days, members>=5000, active>=10, public, non-NSFW
    - Calculates momentum_score for each
    - Sorts by momentum_score DESCENDING
    - Yields the final sorted list in one batch
    """
    HARD_MIN_AGE     = 180
    HARD_MIN_MEMBERS = 5_000

    def _passes_growing(sub: dict) -> bool:
        if sub["age_days"] < HARD_MIN_AGE:     return False
        if sub["members"] < HARD_MIN_MEMBERS:  return False
        return True

    seen: set[str]    = set()
    collected: list[dict] = []

    async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True) as client:

        # Step 1 — popular.json pages
        popular_url = f"{_BASE}/subreddits/popular.json"
        cursor = None
        fetched = 0
        while fetched < target:
            params = {"raw_json": 1, "limit": 100}
            if cursor:
                params["after"] = cursor
            body = await _get(client, popular_url, params=params)
            if not body:
                break
            data_block = body.get("data", {})
            cursor = data_block.get("after")
            raw_batch = _extract_listing(body)
            for sub in raw_batch:
                if sub["display_name"] in seen:
                    continue
                seen.add(sub["display_name"])
                fetched += 1
                if _passes_growing(sub):
                    collected.append(sub)
            if not cursor or not raw_batch:
                break
            await asyncio.sleep(PAGINATION_DELAY)

        # Step 2 — broad search queries
        search_url = f"{_BASE}/subreddits/search.json"
        for keyword in ["new", "growing", "community", "discussion"]:
            cursor = None
            for _ in range(3):   # up to 3 pages per keyword
                params = {"q": keyword, "include_over_18": "false", "raw_json": 1, "limit": 100}
                if cursor:
                    params["after"] = cursor
                body = await _get(client, search_url, params=params)
                if not body:
                    break
                data_block = body.get("data", {})
                cursor = data_block.get("after")
                raw_batch = _extract_listing(body)
                for sub in raw_batch:
                    if sub["display_name"] in seen:
                        continue
                    seen.add(sub["display_name"])
                    if _passes_growing(sub):
                        collected.append(sub)
                if not cursor or not raw_batch:
                    break
                await asyncio.sleep(PAGINATION_DELAY)

    # Step 3 — sort by momentum_score DESC
    collected.sort(key=lambda s: s.get("momentum_score", 0), reverse=True)

    logger.info("[Explorer] paginate_growing — %d communities after hard filters", len(collected))

    # Yield entire sorted list as one batch (frontend expects this for sort)
    if collected:
        yield collected
    yield [{"__cursor__": None}]


# ── Single-subreddit detail fetcher ───────────────────────────────────────────

async def get_subreddit_details(subreddit_name: str) -> Optional[dict]:
    """
    Fetches enriched details for one subreddit.

    Extra data beyond the basic listing fields:
      - long_description  (full sidebar text)
      - moderators_count  (/about/moderators.json)
      - rules_count       (/about/rules.json)
      - posts_per_day     (estimated from /new.json?limit=25 timestamps)

    Returns None if the community is unavailable / private / quarantined.
    """
    name = subreddit_name.lstrip("r/").strip().strip("/")
    logger.info("[Explorer] Fetching details for r/%s", name)

    async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True) as client:

        # 1. Core about page
        about_body = await _get(client, f"{_BASE}/r/{name}/about.json",
                                params={"raw_json": 1})
        if not about_body:
            return None

        result = _parse_subreddit(about_body.get("data", {}))
        if result is None:
            return None

        await asyncio.sleep(random.uniform(1.0, 1.5))

        # 2. Moderator count
        mod_body = await _get(client, f"{_BASE}/r/{name}/about/moderators.json",
                              params={"raw_json": 1})
        if mod_body:
            result["moderators_count"] = len(mod_body.get("data", {}).get("children", []))

        await asyncio.sleep(random.uniform(1.0, 1.5))

        # 3. Rule count
        rules_body = await _get(client, f"{_BASE}/r/{name}/about/rules.json",
                                params={"raw_json": 1})
        if rules_body:
            result["rules_count"] = len(rules_body.get("rules", []))

        await asyncio.sleep(random.uniform(1.0, 1.5))

        # 4. Posts-per-day from recent /new feed
        new_body = await _get(client, f"{_BASE}/r/{name}/new.json",
                              params={"limit": 25, "raw_json": 1})
        if new_body:
            timestamps = [
                float(c["data"]["created_utc"])
                for c in new_body.get("data", {}).get("children", [])
                if c.get("kind") == "t3" and c.get("data", {}).get("created_utc")
            ]
            if len(timestamps) >= 2:
                timestamps.sort(reverse=True)
                span_h = (timestamps[0] - timestamps[-1]) / 3600
                if span_h > 0:
                    result["posts_per_day"] = round(len(timestamps) / (span_h / 24), 1)

    logger.info("[Explorer] Details ready for r/%s", name)
    return result
