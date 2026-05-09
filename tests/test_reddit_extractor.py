"""
tests/test_reddit_extractor.py
-------------------------------
Unit tests for reddit_extractor.py (httpx / public JSON API version).

All tests use mocked httpx responses — no real network calls are made.
"""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from reddit_extractor import (  # type: ignore
    build_json_url,
    detect_comment_media,
    detect_media_note,
    format_timestamp,
    parse_comments,
    safe_author,
    safe_body,
)


# ── build_json_url ────────────────────────────────────────────────────────────

class TestBuildJsonUrl:
    def test_appends_json_extension(self):
        url = "https://www.reddit.com/r/python/comments/abc123/title/"
        result = build_json_url(url, sort="best", limit=25)
        assert ".json" in result

    def test_strips_trailing_slash(self):
        url = "https://www.reddit.com/r/python/comments/abc123/title/"
        result = build_json_url(url, sort="best", limit=25)
        assert not result.startswith(".json")
        assert "title.json" in result

    def test_adds_limit_param(self):
        url = "https://www.reddit.com/r/python/comments/abc123/title"
        result = build_json_url(url, sort="best", limit=50)
        assert "limit=500" in result
        assert "depth=10" in result

    def test_sort_best_translates_to_confidence(self):
        # "best" is Reddit's UI label; the public API requires "confidence"
        url = "https://www.reddit.com/r/python/comments/abc123/title"
        result = build_json_url(url, sort="best", limit=25)
        assert "sort=confidence" in result
        assert "limit=500" in result
        assert "depth=10" in result
        assert "raw_json=1" in result

    def test_sort_other_is_appended(self):
        url = "https://www.reddit.com/r/python/comments/abc123/title"
        result = build_json_url(url, sort="top", limit=25)
        assert "sort=top" in result

    def test_limit_all_becomes_500(self):
        url = "https://www.reddit.com/r/python/comments/abc123/title"
        result = build_json_url(url, sort="best", limit="all")
        assert "limit=500" in result


# ── format_timestamp ──────────────────────────────────────────────────────────

class TestFormatTimestamp:
    def test_known_epoch(self):
        result = format_timestamp(1700000000.0)
        assert result == "2023-11-14 22:13 UTC"

    def test_ends_with_utc(self):
        assert format_timestamp(0.0).endswith("UTC")

    def test_epoch_zero_is_unix_origin(self):
        assert format_timestamp(0.0) == "1970-01-01 00:00 UTC"


# ── safe_author ───────────────────────────────────────────────────────────────

class TestSafeAuthor:
    def test_returns_author_string(self):
        assert safe_author({"author": "john_doe"}) == "john_doe"

    def test_returns_deleted_when_none(self):
        assert safe_author({"author": None}) == "[deleted]"

    def test_returns_deleted_when_empty(self):
        assert safe_author({"author": ""}) == "[deleted]"

    def test_returns_deleted_when_key_missing(self):
        assert safe_author({}) == "[deleted]"

    def test_returns_deleted_when_explicitly_deleted(self):
        assert safe_author({"author": "[deleted]"}) == "[deleted]"


# ── safe_body ─────────────────────────────────────────────────────────────────

class TestSafeBody:
    def test_returns_stripped_text(self):
        assert safe_body({"body": "  hello  "}) == "hello"

    def test_returns_empty_when_none(self):
        assert safe_body({"body": None}) == ""

    def test_returns_removed_passthrough(self):
        assert safe_body({"body": "[removed]"}) == "[removed]"

    def test_uses_selftext_key_for_posts(self):
        assert safe_body({"selftext": "post body"}, key="selftext") == "post body"

    def test_returns_empty_when_key_missing(self):
        assert safe_body({}) == ""


# ── detect_media_note ─────────────────────────────────────────────────────────

class TestDetectMediaNote:
    def test_self_post_is_none(self):
        note = detect_media_note({"is_self": True}, "body text")
        assert note is None

    def test_is_video_flag(self):
        note = detect_media_note({"is_self": False, "is_video": True, "url": ""}, "")
        assert note == "[POST CONTAINS: Video — not copied]"

    def test_post_hint_image(self):
        note = detect_media_note({
            "is_self": False, "is_video": False,
            "post_hint": "image", "url": "https://i.redd.it/abc.jpg"
        }, "")
        assert note == "[POST CONTAINS: Image — not copied]"

    def test_rich_video(self):
        note = detect_media_note({
            "is_self": False, "is_video": False,
            "post_hint": "rich:video", "url": "https://youtube.com/..."
        }, "")
        assert note == "[POST CONTAINS: GIF/Embed — not copied]"

    def test_external_link(self):
        note = detect_media_note({
            "is_self": False, "is_video": False,
            "post_hint": "link", "url": "https://www.nytimes.com/article"
        }, "")
        assert "POST CONTAINS LINK:" in note

    def test_fallback_no_body(self):
        note = detect_media_note({
            "is_self": False, "is_video": False,
            "post_hint": "", "url": "https://example.com"
        }, "")
        assert note == "[POST CONTAINS: Link or external content — not copied]"


# ── detect_comment_media ──────────────────────────────────────────────────────

class TestDetectCommentMedia:
    def test_detects_jpg(self):
        assert detect_comment_media("See: https://i.imgur.com/x.jpg") is not None

    def test_detects_gif(self):
        assert detect_comment_media("lol https://example.com/funny.gif") is not None

    def test_detects_video_link(self):
        assert detect_comment_media("Watch: https://youtube.com/watch?v=12345") is not None

    def test_plain_text_none(self):
        assert detect_comment_media("Just a normal comment.") is None

    def test_empty_string_none(self):
        assert detect_comment_media("") is None


# ── parse_comments ────────────────────────────────────────────────────────────

def _make_comment_child(author="user", body="hello", score=10, depth=0, replies=None):
    child = {
        "kind": "t1",
        "data": {
            "author": author,
            "body": body,
            "score": score,
            "created_utc": 1700000000.0,
            "replies": replies or "",
        }
    }
    return child


def _wrap_replies(children: list) -> dict:
    """Wraps children in a Reddit-style replies listing object."""
    return {
        "kind": "Listing",
        "data": {"children": children}
    }


class TestParseComments:
    def test_empty_listing_returns_empty_list(self):
        listing = {"data": {"children": []}}
        assert parse_comments(listing) == []

    def test_single_comment_parsed(self):
        listing = {"data": {"children": [_make_comment_child()]}}
        result = parse_comments(listing)
        assert len(result) == 1
        assert result[0]["author"] == "user"

    def test_more_kind_placeholder(self):
        more_child = {"kind": "more", "data": {"children": [], "count": 5}}
        listing = {"data": {"children": [more_child]}}
        result = parse_comments(listing)
        assert len(result) == 1
        assert "5 more replies not loaded" in result[0]["body"]

    def test_nested_reply_parsed(self):
        reply = _make_comment_child(author="replier", body="reply text")
        parent = _make_comment_child(replies=_wrap_replies([reply]))
        listing = {"data": {"children": [parent]}}
        result = parse_comments(listing)
        assert len(result) == 1
        assert len(result[0]["replies"]) == 1
        assert result[0]["replies"][0]["author"] == "replier"

    def test_deleted_author_shown(self):
        child = _make_comment_child(author=None)
        listing = {"data": {"children": [child]}}
        result = parse_comments(listing)
        assert result[0]["author"] == "[deleted]"

    def test_multiple_top_level_comments(self):
        listing = {"data": {"children": [
            _make_comment_child(author="a"),
            _make_comment_child(author="b"),
            _make_comment_child(author="c"),
        ]}}
        result = parse_comments(listing)
        assert len(result) == 3
        assert [c["author"] for c in result] == ["a", "b", "c"]


