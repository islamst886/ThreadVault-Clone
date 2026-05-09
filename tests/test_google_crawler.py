"""
tests/test_google_crawler.py
----------------------------
Unit tests for the pure (non-Playwright) helper functions in google_crawler.py.
These tests do NOT require a browser or network access.
"""

import pytest
import sys
import os

# Allow importing from the backend directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from google_crawler import (  # type: ignore
    build_google_query,
    is_reddit_post_url,
    is_captcha_page,
    clean_google_redirect_url,
    deduplicate,
)


# ── build_google_query ────────────────────────────────────────────────────────

class TestBuildGoogleQuery:
    def test_appends_site_reddit(self):
        result = build_google_query("best python libraries")
        assert "site%3Areddit.com" in result or "site:reddit.com" in result.replace("+", " ")

    def test_encodes_spaces_as_plus(self):
        result = build_google_query("hello world")
        assert " " not in result

    def test_strips_leading_trailing_whitespace(self):
        result = build_google_query("  productivity apps  ")
        assert result == build_google_query("productivity apps")

    def test_single_word_query(self):
        result = build_google_query("python")
        assert "python" in result
        assert "reddit" in result


# ── is_reddit_post_url ────────────────────────────────────────────────────────

class TestIsRedditPostUrl:
    # --- Should return True ---
    def test_basic_post_url(self):
        assert is_reddit_post_url(
            "https://www.reddit.com/r/python/comments/abc123/my_title/"
        )

    def test_post_url_without_trailing_slash(self):
        assert is_reddit_post_url(
            "https://reddit.com/r/learnprogramming/comments/xyz999/learning_python"
        )

    def test_post_url_with_http(self):
        assert is_reddit_post_url(
            "http://www.reddit.com/r/gaming/comments/def456/great_game/"
        )

    # --- Should return False ---
    def test_subreddit_homepage(self):
        assert not is_reddit_post_url("https://www.reddit.com/r/python/")

    def test_reddit_root(self):
        assert not is_reddit_post_url("https://www.reddit.com/")

    def test_user_profile(self):
        assert not is_reddit_post_url("https://www.reddit.com/user/someuser/")

    def test_wiki_page(self):
        assert not is_reddit_post_url("https://www.reddit.com/r/python/wiki/index/")

    def test_non_reddit_url(self):
        assert not is_reddit_post_url("https://www.google.com/search?q=python")

    def test_empty_string(self):
        assert not is_reddit_post_url("")


# ── is_captcha_page ───────────────────────────────────────────────────────────

class TestIsCaptchaPage:
    def test_detects_sorry_url(self):
        assert is_captcha_page("https://accounts.google.com/sorry/index", "")

    def test_detects_recaptcha_in_text(self):
        assert is_captcha_page("https://www.google.com/search", "please complete the recaptcha")

    def test_detects_unusual_traffic(self):
        assert is_captcha_page("https://www.google.com/search", "our systems detected unusual traffic")

    def test_detects_captcha_keyword(self):
        assert is_captcha_page("https://www.google.com/", "please solve this captcha challenge")

    def test_normal_page_is_not_captcha(self):
        assert not is_captcha_page("https://www.google.com/search", "python best practices reddit")

    def test_case_insensitive(self):
        assert is_captcha_page("https://www.google.com/", "ReCAPTCHA required")


# ── clean_google_redirect_url ─────────────────────────────────────────────────

class TestCleanGoogleRedirectUrl:
    def test_unwraps_redirect(self):
        result = clean_google_redirect_url(
            "/url?q=https://www.reddit.com/r/python/comments/abc/title/&sa=U"
        )
        assert result == "https://www.reddit.com/r/python/comments/abc/title/"

    def test_passthrough_for_non_redirect(self):
        url = "https://www.reddit.com/r/python/comments/abc123/title/"
        assert clean_google_redirect_url(url) == url

    def test_handles_empty_string(self):
        assert clean_google_redirect_url("") == ""


# ── deduplicate ───────────────────────────────────────────────────────────────

class TestDeduplicate:
    def test_removes_duplicates(self):
        urls = [
            "https://reddit.com/r/python/comments/a/",
            "https://reddit.com/r/python/comments/b/",
            "https://reddit.com/r/python/comments/a/",  # duplicate
        ]
        result = deduplicate(urls)
        assert len(result) == 2
        assert result[0].endswith("/a/")
        assert result[1].endswith("/b/")

    def test_preserves_order(self):
        urls = ["url_c", "url_a", "url_b"]
        assert deduplicate(urls) == ["url_c", "url_a", "url_b"]

    def test_empty_list(self):
        assert deduplicate([]) == []

    def test_no_duplicates_unchanged(self):
        urls = ["url_a", "url_b", "url_c"]
        assert deduplicate(urls) == urls
