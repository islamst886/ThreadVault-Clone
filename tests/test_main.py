"""
tests/test_main.py
------------------
Unit / integration tests for the FastAPI endpoints in main.py.

These tests use FastAPI's TestClient and mock out the heavyweight IO operations
(Google crawl, PRAW extraction, DOCX generation) so no real network calls are made.
"""

import asyncio
import os
import sys
import tempfile
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from main import app, _jobs, Job, STATUS_COMPLETE, STATUS_ERROR, STATUS_QUEUED  # type: ignore


@pytest.fixture(autouse=True)
def clear_jobs():
    """Reset the in-memory job store before each test."""
    _jobs.clear()
    yield
    _jobs.clear()


@pytest.fixture(autouse=True)
def mock_run_job():
    """Prevent POST /search from spawning real background Playwright jobs."""
    with patch("main._run_job") as mock:
        yield mock


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


# ── POST /search ──────────────────────────────────────────────────────────────

class TestPostSearch:
    def test_returns_202_with_job_id(self, client):
        resp = client.post("/search", json={"query": "python tips", "max_pages": 5})
        assert resp.status_code == 202
        body = resp.json()
        assert "job_id" in body
        assert body["status"] == STATUS_QUEUED

    def test_creates_job_in_store(self, client):
        resp = client.post("/search", json={"query": "test query"})
        job_id = resp.json()["job_id"]
        assert job_id in _jobs

    def test_default_max_pages_is_15(self, client):
        resp = client.post("/search", json={"query": "test"})
        job_id = resp.json()["job_id"]
        assert _jobs[job_id].max_pages == 15

    def test_custom_max_pages_stored(self, client):
        resp = client.post("/search", json={"query": "test", "max_pages": 7})
        job_id = resp.json()["job_id"]
        assert _jobs[job_id].max_pages == 7

    def test_query_stripped_of_whitespace(self, client):
        resp = client.post("/search", json={"query": "  hello world  "})
        job_id = resp.json()["job_id"]
        assert _jobs[job_id].query == "hello world"

    def test_empty_query_rejected(self, client):
        resp = client.post("/search", json={"query": ""})
        assert resp.status_code == 422   # Pydantic validation error

    def test_max_pages_gt_1000_rejected(self, client):
        resp = client.post("/search", json={"query": "test", "max_pages": 1001})
        assert resp.status_code == 422

    def test_max_pages_lt_1_rejected(self, client):
        resp = client.post("/search", json={"query": "test", "max_pages": 0})
        assert resp.status_code == 422

    def test_missing_query_field_rejected(self, client):
        resp = client.post("/search", json={"max_pages": 5})
        assert resp.status_code == 422

    def test_two_searches_get_unique_job_ids(self, client):
        r1 = client.post("/search", json={"query": "python"})
        r2 = client.post("/search", json={"query": "javascript"})
        assert r1.json()["job_id"] != r2.json()["job_id"]


# ── GET /status/{job_id} ──────────────────────────────────────────────────────

class TestGetStatus:
    def _create_job(self, **kwargs) -> str:
        """Helper to insert a synthetic job directly into the store."""
        job = Job(job_id="test-id", query="test", max_pages=5, **kwargs)
        _jobs["test-id"] = job
        return "test-id"

    def test_returns_404_for_unknown_job(self, client):
        resp = client.get("/status/does-not-exist")
        assert resp.status_code == 404

    def test_returns_queued_status(self, client):
        self._create_job()
        resp = client.get("/status/test-id")
        assert resp.status_code == 200
        assert resp.json()["status"] == STATUS_QUEUED

    def test_returns_correct_job_id(self, client):
        self._create_job()
        resp = client.get("/status/test-id")
        assert resp.json()["job_id"] == "test-id"

    def test_percent_is_zero_when_no_posts(self, client):
        self._create_job()
        resp = client.get("/status/test-id")
        assert resp.json()["percent"] == 0

    def test_percent_calculated_correctly(self, client):
        job = Job(job_id="test-id", query="q", max_pages=5,
                  posts_done=5, total_posts=10)
        _jobs["test-id"] = job
        resp = client.get("/status/test-id")
        assert resp.json()["percent"] == 50

    def test_complete_status_includes_download_url(self, client):
        job = Job(job_id="test-id", query="q", max_pages=5,
                  status=STATUS_COMPLETE, download_path="/tmp/file.docx")
        _jobs["test-id"] = job
        resp = client.get("/status/test-id")
        body = resp.json()
        assert body["status"] == STATUS_COMPLETE
        assert "download_url" in body
        assert body["download_url"] == "/download/test-id"

    def test_error_status_includes_error_message(self, client):
        job = Job(job_id="test-id", query="q", max_pages=5,
                  status=STATUS_ERROR, error="Something went wrong")
        _jobs["test-id"] = job
        resp = client.get("/status/test-id")
        body = resp.json()
        assert body["status"] == STATUS_ERROR
        assert body["error"] == "Something went wrong"

    def test_warning_included_when_set(self, client):
        job = Job(job_id="test-id", query="q", max_pages=5,
                  warning="Partial results due to CAPTCHA")
        _jobs["test-id"] = job
        resp = client.get("/status/test-id")
        assert "warning" in resp.json()


# ── GET /download/{job_id} ────────────────────────────────────────────────────

class TestGetDownload:
    def test_returns_404_for_unknown_job(self, client):
        resp = client.get("/download/does-not-exist")
        assert resp.status_code == 404

    def test_returns_425_when_still_processing(self, client):
        job = Job(job_id="test-id", query="q", max_pages=5, status="extracting")
        _jobs["test-id"] = job
        resp = client.get("/download/test-id")
        assert resp.status_code == 425

    def test_returns_500_when_job_errored(self, client):
        job = Job(job_id="test-id", query="q", max_pages=5,
                  status=STATUS_ERROR, error="Crawl failed")
        _jobs["test-id"] = job
        resp = client.get("/download/test-id")
        assert resp.status_code == 500

    def test_returns_500_when_file_missing_from_disk(self, client):
        job = Job(job_id="test-id", query="q", max_pages=5,
                  status=STATUS_COMPLETE, download_path="/nonexistent/file.docx")
        _jobs["test-id"] = job
        resp = client.get("/download/test-id")
        assert resp.status_code == 500

    def test_streams_file_when_complete(self, client, tmp_path):
        # Create a real (tiny) file to serve
        docx_file = tmp_path / "test.docx"
        docx_file.write_bytes(b"PK\x03\x04fake docx content")

        job = Job(job_id="test-id", query="q", max_pages=5,
                  status=STATUS_COMPLETE, download_path=str(docx_file))
        _jobs["test-id"] = job

        resp = client.get("/download/test-id")
        assert resp.status_code == 200
        assert "wordprocessingml" in resp.headers["content-type"]

    def test_download_has_attachment_header(self, client, tmp_path):
        docx_file = tmp_path / "export.docx"
        docx_file.write_bytes(b"fake content")
        job = Job(job_id="test-id", query="q", max_pages=5,
                  status=STATUS_COMPLETE, download_path=str(docx_file))
        _jobs["test-id"] = job

        resp = client.get("/download/test-id")
        assert "attachment" in resp.headers.get("content-disposition", "")


# ── GET /jobs (debug endpoint) ────────────────────────────────────────────────

class TestJobsDebugEndpoint:
    def test_returns_empty_dict_initially(self, client):
        resp = client.get("/jobs")
        assert resp.status_code == 200
        assert resp.json() == {}

    def test_lists_existing_jobs(self, client):
        job = Job(job_id="test-id", query="python", max_pages=5)
        _jobs["test-id"] = job
        resp = client.get("/jobs")
        assert "test-id" in resp.json()

    def test_job_entry_has_expected_keys(self, client):
        job = Job(job_id="test-id", query="python", max_pages=5)
        _jobs["test-id"] = job
        entry = client.get("/jobs").json()["test-id"]
        for key in ("status", "query", "posts_done", "total_posts", "created_at"):
            assert key in entry


# ── Root endpoint ─────────────────────────────────────────────────────────────

class TestRoot:
    def test_root_returns_200(self, client):
        resp = client.get("/")
        assert resp.status_code == 200

    def test_root_serves_frontend_or_fallback(self, client):
        resp = client.get("/")
        # If static dir exists, it serves HTML. Otherwise, the fallback JSON.
        assert "ThreadVault" in resp.text
