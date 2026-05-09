"""
youtube_extractor.py
--------------------
YouTube Data API v3 extraction pipeline.

For each channel:
  1. Resolve the channel name / handle / ID → channel metadata
  2. Fetch uploads playlist, pull video IDs (up to MAX_UPLOAD_SCAN videos)
  3. Batch-fetch video statistics, sort by view count, keep top N
  4. For every video, page through all top-level comments (order=relevance)
     and expand every reply thread fully.

No google-api-python-client dependency — plain httpx REST calls.
"""

import asyncio
import os
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import httpx

# ── Constants ────────────────────────────────────────────────────────────────
YT_API = "https://www.googleapis.com/youtube/v3"
MAX_UPLOAD_SCAN = 1000   # scan this many videos before sorting by views
MAX_REPLY_PAGES = 50    # cap on reply pagination per comment thread


# ── Low-level helper ─────────────────────────────────────────────────────────

def _api_key() -> str:
    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not key:
        raise ValueError(
            "YOUTUBE_API_KEY environment variable is not set. "
            "Add it to your .env file and restart the server."
        )
    return key


async def _get(
    client: httpx.AsyncClient,
    endpoint: str,
    params: dict,
    retries: int = 3,
) -> dict:
    """Make a single YouTube Data API GET request with retry logic."""
    params = {**params, "key": _api_key()}
    url = f"{YT_API}/{endpoint}"

    for attempt in range(retries):
        try:
            resp = await client.get(url, params=params, timeout=20.0)
            if resp.status_code == 403:
                # Could be disabled comments or quota exhausted
                return {"_error": "forbidden", "_status": 403}
            if resp.status_code == 429:
                await asyncio.sleep(5 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json()
        except httpx.TimeoutException:
            if attempt < retries - 1:
                await asyncio.sleep(2)
            else:
                raise
    return {}


# ── Channel resolution ────────────────────────────────────────────────────────

async def resolve_channel(client: httpx.AsyncClient, name: str) -> dict:
    """
    Resolve a channel by handle (@name), name, or Channel ID (UCxxxxx).
    Returns a full channel metadata dict.
    Raises ValueError if the channel cannot be found.
    """
    name = name.strip()
    channel_id: Optional[str] = None

    # 1. Direct channel ID
    if name.startswith("UC") and len(name) >= 22:
        channel_id = name

    # 2. Handle (@name)
    if not channel_id:
        handle = name if name.startswith("@") else f"@{name}"
        data = await _get(client, "channels", {"part": "id", "forHandle": handle})
        if data.get("items"):
            channel_id = data["items"][0]["id"]

    # 3. Username (legacy)
    if not channel_id:
        data = await _get(
            client, "channels",
            {"part": "id", "forUsername": name.lstrip("@")},
        )
        if data.get("items"):
            channel_id = data["items"][0]["id"]

    # 4. Search fallback
    if not channel_id:
        data = await _get(
            client, "search",
            {"part": "snippet", "q": name, "type": "channel", "maxResults": 3},
        )
        if data.get("items"):
            channel_id = data["items"][0]["snippet"]["channelId"]

    if not channel_id:
        raise ValueError(f"Cannot find YouTube channel: {name!r}")

    # Fetch full metadata including uploads playlist
    data = await _get(
        client, "channels",
        {"part": "snippet,statistics,contentDetails", "id": channel_id},
    )
    items = data.get("items", [])
    if not items:
        raise ValueError(f"Cannot load channel data for ID: {channel_id}")

    return _parse_channel(items[0])


def _parse_channel(item: dict) -> dict:
    snippet  = item.get("snippet", {})
    stats    = item.get("statistics", {})
    content  = item.get("contentDetails", {})
    cid      = item["id"]
    handle   = snippet.get("customUrl", "")
    url      = f"https://www.youtube.com/{handle}" if handle else f"https://www.youtube.com/channel/{cid}"
    return {
        "channel_id":         cid,
        "channel_name":       snippet.get("title", ""),
        "channel_handle":     handle,
        "channel_url":        url,
        "description":        snippet.get("description", "")[:600],
        "subscriber_count":   int(stats.get("subscriberCount", 0) or 0),
        "video_count":        int(stats.get("videoCount", 0) or 0),
        "total_view_count":   int(stats.get("viewCount", 0) or 0),
        "uploads_playlist_id": content.get("relatedPlaylists", {}).get("uploads", ""),
        "country":            snippet.get("country", ""),
        "published_at":       snippet.get("publishedAt", ""),
    }


# ── Top videos ────────────────────────────────────────────────────────────────

async def get_top_videos(
    client: httpx.AsyncClient,
    channel: dict,
    max_videos: int = 25,
    status_cb: Optional[Callable[[str], None]] = None,
) -> list[dict]:
    """
    Scan the channel's uploads playlist (up to MAX_UPLOAD_SCAN entries),
    batch-fetch statistics, sort by view count descending, return top max_videos.
    """
    playlist_id = channel.get("uploads_playlist_id", "")
    if not playlist_id:
        return []

    if status_cb:
        status_cb(f"Fetching video list for {channel['channel_name']}…")

    # Step 1 — collect video IDs from the uploads playlist
    video_ids: list[str] = []
    page_token: Optional[str] = None

    for _ in range(MAX_UPLOAD_SCAN // 50 + 1):
        params: dict[str, Any] = {
            "part": "contentDetails",
            "playlistId": playlist_id,
            "maxResults": 50,
        }
        if page_token:
            params["pageToken"] = page_token

        data = await _get(client, "playlistItems", params)
        for item in data.get("items", []):
            vid_id = item.get("contentDetails", {}).get("videoId")
            if vid_id:
                video_ids.append(vid_id)

        page_token = data.get("nextPageToken")
        if not page_token or len(video_ids) >= MAX_UPLOAD_SCAN:
            break

    if not video_ids:
        return []

    # Step 2 — batch-fetch video statistics (50 per request)
    videos: list[dict] = []
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i: i + 50]
        data = await _get(
            client, "videos",
            {"part": "snippet,statistics,contentDetails", "id": ",".join(batch)},
        )
        for item in data.get("items", []):
            videos.append(_parse_video(item, channel))

    # Step 3 — sort by view count and return top N
    videos.sort(key=lambda v: v["view_count"], reverse=True)
    return videos[:max_videos]


def _parse_video(item: dict, channel: dict) -> dict:
    snippet  = item.get("snippet", {})
    stats    = item.get("statistics", {})
    content  = item.get("contentDetails", {})
    vid_id   = item["id"]
    return {
        "video_id":          vid_id,
        "title":             snippet.get("title", ""),
        "url":               f"https://www.youtube.com/watch?v={vid_id}",
        "channel_name":      channel.get("channel_name", ""),
        "channel_url":       channel.get("channel_url", ""),
        "published_at":      snippet.get("publishedAt", ""),
        "description":       snippet.get("description", "")[:500],
        "thumbnail_url":     (snippet.get("thumbnails", {}).get("high", {}) or
                              snippet.get("thumbnails", {}).get("default", {})).get("url", ""),
        "view_count":        int(stats.get("viewCount", 0) or 0),
        "like_count":        int(stats.get("likeCount", 0) or 0),
        "comment_count":     int(stats.get("commentCount", 0) or 0),
        "duration":          _parse_duration(content.get("duration", "")),
        "comments":          [],   # populated later
    }


def _parse_duration(iso: str) -> str:
    """Convert ISO 8601 duration (PT1H2M3S) to human-readable string."""
    if not iso:
        return ""
    iso = iso.replace("PT", "")
    parts, result = {}, []
    for ch, label in [("H", "h"), ("M", "m"), ("S", "s")]:
        if ch in iso:
            idx = iso.index(ch)
            parts[label] = iso[:idx]
            iso = iso[idx + 1:]
    for label in ["h", "m", "s"]:
        if label in parts:
            result.append(f"{parts[label]}{label}")
    return " ".join(result) or iso


# ── Comments ─────────────────────────────────────────────────────────────────

async def get_comments(
    client: httpx.AsyncClient,
    video: dict,
    max_top_level: Optional[int] = None,
    status_cb: Optional[Callable[[str], None]] = None,
) -> list[dict]:
    """
    Fetch top-level comments sorted by relevance, plus all nested replies.
    max_top_level=None means fetch all (paginate until exhausted).
    Returns a list of comment dicts, each with a 'replies' list.
    """
    vid_id = video["video_id"]
    title  = video["title"][:40]
    comments: list[dict] = []
    page_token: Optional[str] = None

    while True:
        params: dict[str, Any] = {
            "part":        "snippet,replies",
            "videoId":     vid_id,
            "order":       "relevance",
            "maxResults":  100,
            "textFormat":  "plainText",
        }
        if page_token:
            params["pageToken"] = page_token

        data = await _get(client, "commentThreads", params)

        if data.get("_error") == "forbidden":
            # Comments disabled for this video
            if status_cb:
                status_cb(f"⚠ Comments disabled: \"{title}\"")
            return []

        items = data.get("items", [])
        for item in items:
            top = item["snippet"]["topLevelComment"]["snippet"]
            reply_count = item["snippet"].get("totalReplyCount", 0)

            # Replies already included in this response (up to 5)
            snippet_replies: list[dict] = []
            for r in item.get("replies", {}).get("comments", []):
                s = r["snippet"]
                snippet_replies.append({
                    "author":       s.get("authorDisplayName", ""),
                    "text":         s.get("textDisplay", ""),
                    "likes":        int(s.get("likeCount", 0) or 0),
                    "published_at": _fmt_date(s.get("publishedAt", "")),
                })

            # If more replies exist, fetch them fully
            full_replies = snippet_replies
            if reply_count > len(snippet_replies):
                try:
                    full_replies = await _get_all_replies(client, item["id"])
                except Exception:
                    pass  # keep the partial set

            comments.append({
                "author":       top.get("authorDisplayName", ""),
                "text":         top.get("textDisplay", ""),
                "likes":        int(top.get("likeCount", 0) or 0),
                "published_at": _fmt_date(top.get("publishedAt", "")),
                "reply_count":  reply_count,
                "replies":      full_replies,
            })

        # Check limit
        if max_top_level and len(comments) >= max_top_level:
            comments = comments[:max_top_level]
            break

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    if status_cb:
        status_cb(
            f"  → \"{title}\" — {len(comments):,} comments"
        )
    return comments


async def _get_all_replies(
    client: httpx.AsyncClient,
    parent_id: str,
) -> list[dict]:
    """Fetch all replies for one top-level comment thread."""
    replies: list[dict] = []
    page_token: Optional[str] = None

    for _ in range(MAX_REPLY_PAGES):
        params: dict[str, Any] = {
            "part":       "snippet",
            "parentId":   parent_id,
            "maxResults": 100,
            "textFormat": "plainText",
        }
        if page_token:
            params["pageToken"] = page_token

        data = await _get(client, "comments", params)
        for item in data.get("items", []):
            s = item["snippet"]
            replies.append({
                "author":       s.get("authorDisplayName", ""),
                "text":         s.get("textDisplay", ""),
                "likes":        int(s.get("likeCount", 0) or 0),
                "published_at": _fmt_date(s.get("publishedAt", "")),
            })

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return replies


def _fmt_date(iso: str) -> str:
    """Format ISO 8601 datetime to human-readable YYYY-MM-DD."""
    if not iso:
        return ""
    try:
        return iso[:10]
    except Exception:
        return iso


# ── Full channel extraction ───────────────────────────────────────────────────

async def extract_channel(
    channel_name: str,
    max_videos: int = 25,
    max_comments_per_video: Optional[int] = None,
    status_cb: Optional[Callable[[str], None]] = None,
) -> dict:
    """
    Full extraction pipeline for a single channel.

    Returns a dict containing channel metadata and a `videos` list,
    each video having a `comments` list (with nested replies).
    """
    async with httpx.AsyncClient(
        headers={"Accept-Encoding": "gzip"},
        follow_redirects=True,
    ) as client:
        # 1. Resolve channel
        if status_cb:
            status_cb(f"Resolving channel: {channel_name}…")
        channel = await resolve_channel(client, channel_name)
        if status_cb:
            status_cb(
                f"Found: {channel['channel_name']} "
                f"({channel['subscriber_count']:,} subscribers)"
            )

        # 2. Get top videos
        videos = await get_top_videos(
            client, channel,
            max_videos=max_videos,
            status_cb=status_cb,
        )
        if status_cb:
            status_cb(
                f"Top {len(videos)} videos identified — "
                "now fetching comments…"
            )

        # 3. Get comments for each video
        for idx, video in enumerate(videos, 1):
            if status_cb:
                status_cb(
                    f"Fetching comments: video {idx}/{len(videos)} "
                    f"— \"{video['title'][:45]}\"…"
                )
            video["comments"] = await get_comments(
                client, video,
                max_top_level=max_comments_per_video,
                status_cb=status_cb,
            )

        channel["videos"] = videos
        channel["extracted_at"] = datetime.now(timezone.utc).isoformat()
        return channel
