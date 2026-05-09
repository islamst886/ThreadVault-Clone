"""
main.py
-------
ThreadVault FastAPI backend.

Orchestrates the full pipeline:
  1. POST /search   → launches a background job (Google crawl + Reddit extract + DOCX)
  2. GET  /status/{job_id} → polls live progress of a running job
  3. GET  /download/{job_id} → streams the finished .docx file

Architecture:
  - Jobs run as asyncio tasks so the event loop stays free.
  - Reddit extraction uses httpx (fully async) — no credentials required.
  - python-docx (sync) is off-loaded to a thread pool via asyncio.to_thread().
  - Job state lives in a plain in-memory dict — no database needed.
"""

import asyncio
import sys

# Force ProactorEventLoop on Windows so Playwright can spawn subprocesses
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import mimetypes
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")

from pydantic import BaseModel, Field, field_validator

import reddit_extractor as _re  # type: ignore
import ai_analyzer as _ai  # type: ignore
from google_crawler import crawl_multiple_google_queries_sync  # type: ignore
import subreddit_explorer as _se  # type: ignore
import lead_finder as _lf  # type: ignore
from docx_generator import generate_docx  # type: ignore
import subreddit_bulk_extractor as _sbe  # type: ignore
import youtube_extractor as _yt  # type: ignore
from youtube_docx_generator import generate_youtube_docx  # type: ignore


# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("threadVault.main")

# ── Load .env (if present) ─────────────────────────────────────────────────────
load_dotenv()

# ── Directory paths (resolved relative to this file) ─────────────────────────
_BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR  = os.path.join(_BASE_DIR, "..")
OUTPUT_DIR = os.path.join(_ROOT_DIR, "output")
STATIC_DIR = os.path.join(_ROOT_DIR, "static")

# ── Job status constants ───────────────────────────────────────────────────────
STATUS_QUEUED      = "queued"
STATUS_CRAWLING    = "crawling"
STATUS_EXTRACTING  = "extracting"
STATUS_ANALYZING   = "analyzing"     # Gemini AI analysis stage
STATUS_GENERATING  = "generating"
STATUS_COMPLETE    = "complete"
STATUS_ERROR       = "error"
STATUS_BLOCKED     = "blocked"       # Google CAPTCHA caught — partial result


# ── Job state container ────────────────────────────────────────────────────────

@dataclass
class Job:
    """In-memory state for a single research job."""
    job_id:       str
    query:        str
    max_pages:    int
    sort:         str = "best"
    limit:        int | str = 25
    status:       str = STATUS_QUEUED
    urls_found:   int = 0
    posts_done:   int = 0
    total_posts:  int = 0
    warning:      Optional[str] = None
    substatus:    Optional[str] = None
    error:        Optional[str] = None
    download_path: Optional[str] = None
    posts:        list[dict] = field(default_factory=list)   # populated after extraction
    created_at:   datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    completed_at: Optional[datetime] = None

    @property
    def percent(self) -> int:
        if self.total_posts == 0:
            return 0
        return int((self.posts_done / self.total_posts) * 100)

    def to_status_dict(self) -> dict[str, Any]:
        """Serialises job state for the /status endpoint response."""
        base = {
            "job_id":      self.job_id,
            "status":      self.status,
            "query":       self.query,
            "urls_found":  self.urls_found,
            "posts_done":  self.posts_done,
            "total_posts": self.total_posts,
            "percent":     self.percent,
            "created_at":  self.created_at.isoformat(),
        }
        if self.warning:
            base["warning"] = self.warning
        if self.substatus:
            base["substatus"] = self.substatus
        if self.error:
            base["error"] = self.error
        if self.status == STATUS_COMPLETE:
            base["download_url"] = f"/download/{self.job_id}"
            base["completed_at"] = self.completed_at.isoformat() if self.completed_at else None
        return base


# ── Application state ──────────────────────────────────────────────────────────
_jobs: dict[str, Job] = {}      # job_id → Job


# ── Lifespan (startup / shutdown) ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Runs once at startup to initialise shared resources.
    No credentials required — Reddit data is fetched via the public JSON API.
    """
    logger.info("ThreadVault API starting up…")

    # Ensure the output directory exists
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    logger.info("Output directory: %s", os.path.abspath(OUTPUT_DIR))

    yield  # Server runs

    logger.info("ThreadVault API shutting down.")


# ── FastAPI app ────────────────────────────────────────────────────────────────

app = FastAPI(
    title="ThreadVault API",
    description="Reddit research tool — crawls Google, extracts Reddit posts, exports .docx",
    version="1.0.0",
    lifespan=lifespan,
)

# ── CORS (allow all origins for local dev; tighten for production) ─────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Serve the frontend from /static AND root ─────────────────────────────
# Mount at /static keeps the old path working.
# Mount at / lets index.html load /style.css, /search.js, /explorer.js directly.
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    logger.info("Static files mounted from: %s", STATIC_DIR)


# ── Pydantic request / response models ────────────────────────────────────────

class SearchRequest(BaseModel):
    query:     str = Field(..., min_length=1, max_length=500, description="Search keywords")
    max_pages: int = Field(15,  ge=1, le=1000, description="Max Google result pages to crawl")
    sort:      str = Field("best", description="Reddit comment sort mode")
    limit:     Any = Field(25, description="Number of top-level comments to extract (int or 'all')")

    @field_validator("query")
    @classmethod
    def strip_whitespace(cls, v: str) -> str:
        return v.strip()
        
    @field_validator("sort")
    @classmethod
    def validate_sort(cls, v: str) -> str:
        valid_sorts = {"best", "top", "new", "controversial", "old", "qa"}
        v_low = v.lower()
        if v_low not in valid_sorts:
            raise ValueError(f"Invalid sort mode. Must be one of {valid_sorts}")
        return v_low

    @field_validator("limit")
    @classmethod
    def validate_limit(cls, v: Any) -> Any:
        if str(v).lower() == "all":
            return "all"
        try:
            val = int(v)
            if val < 1:
                raise ValueError()
            return val
        except ValueError:
            raise ValueError("Limit must be a positive integer or 'all'")


class SearchResponse(BaseModel):
    job_id:   str
    status:   str
    message:  str


# ── Background pipeline ────────────────────────────────────────────────────────

async def _run_job(job_id: str) -> None:
    """
    Full pipeline for a single research job — runs as a background asyncio task.

    Stages:
      1. [crawling]   Google Search → collect Reddit post URLs via Playwright.
      2. [extracting] For each URL, fetch structured data via Reddit public JSON API.
      3. [generating] Compile all posts into a .docx file.
      4. [complete]   Job marked done; download URL becomes available.

    All errors are caught; the job.status reflects the outcome so the frontend
    can display meaningful feedback.
    """
    job = _jobs[job_id]
    logger.info("[Job %s] Pipeline started | query='%s' | max_pages=%d",
                job_id, job.query, job.max_pages)

    # ── Stage 1: Google crawl (isolated thread to fix Windows asyncio loops) ───
    job.status = STATUS_CRAWLING
    logger.info("[Job %s] Stage 1/3 — Crawling Google…", job_id)
    try:
        reddit_urls: list[str] = await asyncio.to_thread(
            crawl_multiple_google_queries_sync,
            [job.query],
            max_pages=job.max_pages,
            headless=False,
        )
    except Exception as exc:
        logger.error("[Job %s] Google crawl raised an exception: %s", job_id, exc)
        job.status = STATUS_ERROR
        job.error  = f"Google crawl failed: {exc}"
        return

    job.urls_found  = len(reddit_urls)
    job.total_posts = len(reddit_urls)
    logger.info("[Job %s] Crawl complete — %d Reddit URLs found.", job_id, job.urls_found)

    # If the crawl returned nothing, detect whether it was a CAPTCHA block
    if not reddit_urls:
        # A zero-result crawl most likely means Google blocked us
        job.status  = STATUS_BLOCKED
        job.warning = (
            "Google returned zero results. This may be caused by a CAPTCHA block. "
            "Try again later or reduce the number of pages."
        )
        logger.warning("[Job %s] No URLs collected — possible CAPTCHA block.", job_id)
        return

    # ── Stage 2: Reddit data extraction (public JSON API, no credentials) ───────
    job.status = STATUS_EXTRACTING
    logger.info("[Job %s] Stage 2/3 — Extracting %d posts via Reddit JSON API…", job_id, len(reddit_urls))

    import random as _random

    posts: list[dict] = []
    failed_urls: list[str] = []

    # ── Rate-limit gate ────────────────────────────────────────────────────────
    # Cleared (False) when any task hits a 429 and all others should pause.
    _rl_gate = asyncio.Event()
    _rl_gate.set()  # Start open — no rate limit active yet

    # Sequential: one post at a time prevents two tasks from burning through
    # all 429-retry attempts simultaneously and both returning None.
    sem = asyncio.Semaphore(1)

    async def _fetch_one(url: str) -> bool:
        """Fetch one post. Returns True on success, False if it should be retried."""
        # Wait if another task is already sitting out a rate-limit backoff.
        await _rl_gate.wait()

        async with sem:
            def _status_cb(msg: str | None) -> None:
                job.substatus = msg
                if msg and "429" in msg:
                    # Tell all waiting tasks to hold off.
                    if _rl_gate.is_set():
                        _rl_gate.clear()
                elif msg is None and not _rl_gate.is_set():
                    # Backoff finished — let the next task proceed.
                    _rl_gate.set()

            post = await _re.extract_post(
                url,
                sort=job.sort,
                limit=job.limit,
                client=http_client,
                status_callback=_status_cb,
            )

        # ── Polite delay OUTSIDE the semaphore ─────────────────────────────────
        # Releasing sem before sleeping means the next task is not blocked
        # waiting for the courtesy delay.  The sleep is also inside a try so a
        # CancelledError here does not silently drop an already-captured post.
        try:
            await asyncio.sleep(_random.uniform(_re.DELAY_MIN_SEC, _re.DELAY_MAX_SEC))
        except asyncio.CancelledError:
            pass  # Delay interrupted — still return the result we already have

        if post is not None:
            posts.append(post)
            return True
        return False

    async def _first_pass_task(url: str) -> None:
        try:
            ok = await _fetch_one(url)
            if not ok:
                failed_urls.append(url)
        except asyncio.CancelledError:
            # Task was cancelled externally — queue for retry, then re-raise
            # so gather knows this task ended abnormally.
            failed_urls.append(url)
            raise
        except Exception as exc:
            logger.warning("[Job %s] Unexpected error for %s: %s", job_id, url, exc)
            failed_urls.append(url)
        finally:
            job.posts_done += 1
            logger.info(
                "[Job %s] Progress: %d / %d | Succeeded so far: %d",
                job_id, job.posts_done, job.total_posts, len(posts),
            )

    async def _retry_pass(urls: list[str], round_num: int) -> list[str]:
        """Retry a batch of URLs. Returns the subset that still failed."""
        still_failed: list[str] = []

        async def _retry_task(url: str) -> None:
            try:
                ok = await _fetch_one(url)
                if not ok:
                    still_failed.append(url)
            except Exception as exc:
                logger.warning(
                    "[Job %s] Retry %d error for %s: %s",
                    job_id, round_num, url, exc,
                )
                still_failed.append(url)
            logger.info(
                "[Job %s] Retry %d: %d / %d posts succeeded so far.",
                job_id, round_num, len(posts), job.total_posts,
            )

        retry_tasks = [_retry_task(u) for u in urls]
        await asyncio.gather(*retry_tasks, return_exceptions=True)
        return still_failed

    # ── Run extraction ─────────────────────────────────────────────────────────
    async with httpx.AsyncClient(
        headers=_re.REDDIT_HEADERS,
        follow_redirects=True,
    ) as http_client:

        # First pass — all URLs
        first_tasks = [_first_pass_task(url) for url in reddit_urls]
        await asyncio.gather(*first_tasks, return_exceptions=True)

        # Retry passes (up to 2 rounds) for any posts that returned None
        for _round in range(1, 3):
            if not failed_urls:
                break

            batch = failed_urls.copy()
            failed_urls.clear()
            wait_secs = 30 * _round

            logger.info(
                "[Job %s] Retry %d/2 — waiting %ds then reattempting %d posts.",
                job_id, _round, wait_secs, len(batch),
            )
            job.substatus = (
                f"Retrying {len(batch)} posts (attempt {_round}/2) "
                f"\u2014 waiting {wait_secs}s…"
            )
            await asyncio.sleep(wait_secs)
            job.substatus = f"Retrying {len(batch)} posts (attempt {_round}/2)…"

            remaining = await _retry_pass(batch, _round)
            failed_urls.extend(remaining)
            job.substatus = None

    # Reconcile posts_done with the real success count.
    # During the first pass, posts_done was incremented for every *attempted*
    # URL (so the progress bar reaches 100 %).  Now that retries are finished,
    # overwrite it with the actual number of posts that made it into `posts`,
    # so the frontend success screen shows the true extracted count.
    job.posts_done = len(posts)
    job.posts = posts   # persist for /results endpoint

    logger.info(
        "[Job %s] Extraction done: %d / %d succeeded | %d permanently failed.",
        job_id, len(posts), job.total_posts, len(failed_urls),
    )

    if not posts:
        job.status = STATUS_ERROR
        job.error  = "All Reddit post extractions failed or were skipped."
        logger.error("[Job %s] No posts extracted — aborting.", job_id)
        return

    logger.info("[Job %s] %d posts extracted successfully.", job_id, len(posts))


    # ── Stage 2.5: AI analysis ─────────────────────────────────────────────────
    job.status = STATUS_ANALYZING
    logger.info("[Job %s] Stage 2.5/3 — Running AI analysis on %d posts…", job_id, len(posts))

    def _ai_status_cb(msg: str | None) -> None:
        job.substatus = msg

    try:
        await _ai.analyze_posts(posts, status_callback=_ai_status_cb)
    except Exception as exc:
        # Non-fatal — log and continue to DOCX generation
        logger.warning("[Job %s] AI analysis stage raised an unexpected error: %s", job_id, exc)
        job.substatus = None

    logger.info("[Job %s] AI analysis stage complete.", job_id)


    # ── Stage 3: DOCX generation ───────────────────────────────────────────────
    job.status = STATUS_GENERATING
    logger.info("[Job %s] Stage 3/3 — Generating DOCX…", job_id)

    def _docx_progress(idx: int, total: int):
        job.substatus = f"Writing post {idx} of {total} to document..."

    try:
        # python-docx is synchronous — run in thread
        file_path: str = await asyncio.to_thread(
            generate_docx,
            posts,
            job.query,
            OUTPUT_DIR,
            _docx_progress,
        )
    except Exception as exc:
        job.status = STATUS_ERROR
        job.error  = f"DOCX generation failed: {exc}"
        logger.error("[Job %s] DOCX generation error: %s", job_id, exc)
        return

    # ── Done ───────────────────────────────────────────────────────────────────
    job.status       = STATUS_COMPLETE
    job.download_path = file_path
    job.completed_at  = datetime.now(tz=timezone.utc)

    logger.info(
        "[Job %s] ✅ Complete — %d posts | DOCX: %s",
        job_id, len(posts), file_path,
    )


# ── API Endpoints ──────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def root() -> FileResponse:
    """Serve the frontend SPA from root — open http://localhost:8000 in browser."""
    index = os.path.join(STATIC_DIR, "index.html")
    if os.path.isfile(index):
        return FileResponse(index, media_type="text/html")
    # Fallback JSON if static dir is missing
    return JSONResponse({
        "service": "ThreadVault API",
        "version": "1.0.0",
        "note":    "Frontend not found. Place index.html in /static/",
        "endpoints": ["/search", "/status/{job_id}", "/download/{job_id}"],
    })


@app.post("/search", response_model=SearchResponse, status_code=202)
async def start_search(request: SearchRequest) -> SearchResponse:
    """
    Kicks off a new research job.

    Accepts the user's search query and an optional max_pages limit.
    Immediately returns a job_id that can be polled via GET /status/{job_id}.

    The actual crawl + extraction + DOCX generation runs in the background.
    """
    job_id = str(uuid.uuid4())
    job = Job(
        job_id=job_id,
        query=request.query,
        max_pages=request.max_pages,
        sort=request.sort,
        limit=request.limit,
    )
    _jobs[job_id] = job

    logger.info(
        "New job created: %s | query='%s' | max_pages=%d",
        job_id, request.query, request.max_pages,
    )

    # Launch the pipeline as a fire-and-forget asyncio task
    asyncio.create_task(_run_job(job_id))

    return SearchResponse(
        job_id=job_id,
        status=STATUS_QUEUED,
        message=(
            f"Job queued. Poll GET /status/{job_id} for progress. "
            f"Searching for: '{request.query}'"
        ),
    )


@app.get("/status/{job_id}")
async def get_status(job_id: str) -> JSONResponse:
    """
    Returns the current state of a running or completed job.

    While processing:
        { "status": "extracting", "posts_done": 10, "total_posts": 23, "percent": 43, ... }
    When complete:
        { "status": "complete", "download_url": "/download/<job_id>", ... }
    On error:
        { "status": "error", "error": "...", ... }
    """
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    return JSONResponse(content=job.to_status_dict())


@app.get("/download/{job_id}")
async def download_file(job_id: str) -> FileResponse:
    """
    Streams the generated .docx file as an attachment download.

    Only available when the job status is 'complete'.
    Returns 404 if the job does not exist, 425 (Too Early) if still processing,
    and 500 if the file was lost after completion.
    """
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    if job.status in (STATUS_QUEUED, STATUS_CRAWLING, STATUS_EXTRACTING, STATUS_ANALYZING, STATUS_GENERATING):
        raise HTTPException(
            status_code=425,
            detail=f"Job is still {job.status}. Poll /status/{job_id} and retry when complete.",
        )

    if job.status in (STATUS_ERROR, STATUS_BLOCKED):
        detail = job.error or job.warning or "Job did not complete successfully."
        raise HTTPException(status_code=500, detail=detail)

    if not job.download_path or not os.path.isfile(job.download_path):
        raise HTTPException(
            status_code=500,
            detail="Generated file not found on disk. The server may have restarted.",
        )

    filename = os.path.basename(job.download_path)
    logger.info("[Job %s] Serving download: %s", job_id, filename)

    return FileResponse(
        path=job.download_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=filename,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/results/{job_id}", include_in_schema=False)
async def get_results(job_id: str) -> JSONResponse:
    """
    Returns the raw posts list (with ai_analysis fields) for the dashboard.
    Only available once the job is complete.
    """
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    if job.status not in (STATUS_COMPLETE, STATUS_BLOCKED):
        raise HTTPException(
            status_code=425,
            detail=f"Job is still {job.status}. Wait for completion before fetching results.",
        )
    return JSONResponse(content={"posts": job.posts})


@app.get("/jobs", include_in_schema=False)
async def list_jobs() -> JSONResponse:
    """
    Debug endpoint — lists all jobs and their statuses.
    Not exposed in the OpenAPI schema; remove or lock down for production.
    """
    return JSONResponse(content={
        jid: {
            "status":      j.status,
            "query":       j.query,
            "posts_done":  j.posts_done,
            "total_posts": j.total_posts,
            "created_at":  j.created_at.isoformat(),
        }
        for jid, j in _jobs.items()
    })


# ── Community Explorer endpoints (SSE streaming) ──────────────────────────
# Endpoints stream SSE events so the browser can render communities as they
# arrive, page by page, without waiting for the full result set.

def _sse(obj: dict) -> str:
    """Format a Python dict as a single SSE data line."""
    return f"data: {json.dumps(obj)}\n\n"


@app.get("/communities/popular/stream")
async def communities_popular_stream(
    target:      int            = 300,
    after:       Optional[str]  = None,
    min_members: Optional[int]  = None,
    max_members: Optional[int]  = None,
    min_active:  Optional[int]  = None,
    max_active:  Optional[int]  = None,
):
    """
    SSE: streams popular subreddits matching optional size/activity filters.
    GET /communities/popular/stream?target=300
    GET /communities/popular/stream?target=300&min_members=10000&max_members=99999
    GET /communities/popular/stream?target=300&after=t5_xyz  ← resume
    Events: start → batch(+) → done | error
    """
    async def _gen():
        yield _sse({"type": "start", "target": target})
        loaded = 0
        cursor = None
        try:
            async for batch in _se.paginate_popular(
                target=target, after=after,
                min_members=min_members, max_members=max_members,
                min_active=min_active, max_active=max_active,
            ):
                if len(batch) == 1 and "__cursor__" in batch[0]:
                    cursor = batch[0]["__cursor__"]
                    continue
                loaded += len(batch)
                yield _sse({"type": "batch", "results": batch,
                            "loaded": loaded, "target": target})
            yield _sse({"type": "done", "total": loaded, "after": cursor})
        except Exception as exc:
            logger.error("[Stream popular] %s", exc)
            yield _sse({"type": "error", "message": str(exc)})

    return StreamingResponse(
        _gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/communities/new/stream")
async def communities_new_stream(
    target:      int            = 200,
    after:       Optional[str]  = None,
    min_members: Optional[int]  = None,
    max_members: Optional[int]  = None,
    min_active:  Optional[int]  = None,
    max_active:  Optional[int]  = None,
):
    """
    SSE: streams newly-created subreddits matching optional filters.
    GET /communities/new/stream?target=200
    GET /communities/new/stream?target=200&after=t5_xyz  ← resume
    """
    async def _gen():
        yield _sse({"type": "start", "target": target})
        loaded = 0
        cursor = None
        try:
            async for batch in _se.paginate_new(
                target=target, after=after,
                min_members=min_members, max_members=max_members,
                min_active=min_active, max_active=max_active,
            ):
                if len(batch) == 1 and "__cursor__" in batch[0]:
                    cursor = batch[0]["__cursor__"]
                    continue
                loaded += len(batch)
                yield _sse({"type": "batch", "results": batch,
                            "loaded": loaded, "target": target})
            yield _sse({"type": "done", "total": loaded, "after": cursor})
        except Exception as exc:
            logger.error("[Stream new] %s", exc)
            yield _sse({"type": "error", "message": str(exc)})

    return StreamingResponse(
        _gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/communities/search/stream")
async def communities_search_stream(
    q:           str,
    target:      int            = 200,
    after:       Optional[str]  = None,
    min_members: Optional[int]  = None,
    max_members: Optional[int]  = None,
    min_active:  Optional[int]  = None,
    max_active:  Optional[int]  = None,
):
    """
    SSE: streams subreddits matching keyword + optional filters.
    GET /communities/search/stream?q=fitness&target=200
    GET /communities/search/stream?q=fitness&target=200&after=t5_xyz  ← resume
    """
    if not q or not q.strip():
        raise HTTPException(status_code=400, detail="Query 'q' is required.")

    async def _gen():
        yield _sse({"type": "start", "target": target, "query": q})
        loaded = 0
        cursor = None
        try:
            async for batch in _se.paginate_search(
                q.strip(), target=target, after=after,
                min_members=min_members, max_members=max_members,
                min_active=min_active, max_active=max_active,
            ):
                if len(batch) == 1 and "__cursor__" in batch[0]:
                    cursor = batch[0]["__cursor__"]
                    continue
                loaded += len(batch)
                yield _sse({"type": "batch", "results": batch,
                            "loaded": loaded, "target": target})
            yield _sse({"type": "done", "total": loaded, "after": cursor})
        except Exception as exc:
            logger.error("[Stream search] %s", exc)
            yield _sse({"type": "error", "message": str(exc)})

    return StreamingResponse(
        _gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )



@app.get("/communities/growing/stream")
async def communities_growing_stream(target: int = 500):
    """
    SSE: streams 'Growing' communities — established subreddits (6mo+, 5K+ members,
    10+ active) sorted by momentum_score (engagement% × log10(members)).
    GET /communities/growing/stream?target=500
    Events: start → batch → done | error
    """
    async def _gen():
        yield _sse({"type": "start", "target": target})
        loaded = 0
        cursor = None
        try:
            async for batch in _se.paginate_growing(target=target):
                if len(batch) == 1 and "__cursor__" in batch[0]:
                    cursor = batch[0]["__cursor__"]
                    continue
                loaded += len(batch)
                yield _sse({"type": "batch", "results": batch,
                            "loaded": loaded, "target": target})
            yield _sse({"type": "done", "total": loaded, "after": cursor})
        except Exception as exc:
            logger.error("[Stream growing] %s", exc)
            yield _sse({"type": "error", "message": str(exc)})

    return StreamingResponse(
        _gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/communities/details/{subreddit_name}")

async def communities_details(subreddit_name: str) -> JSONResponse:
    """
    Returns enriched detail for a single subreddit.
    GET /communities/details/python
    """
    result = await _se.get_subreddit_details(subreddit_name)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"Community r/{subreddit_name} not found or is private/quarantined.",
        )
    return JSONResponse(content=result)


# ────────────────────────────────────────────────────────────────────────────
# VALIDATION HUB — Lead Finder endpoints
# ────────────────────────────────────────────────────────────────────────────

class LeadRequest(BaseModel):
    saas_description: str = Field(..., min_length=5)
    target_customer:  str = Field(..., min_length=3)
    problems:         str = Field(..., min_length=5)
    subreddits:       str = Field("", description="Optional comma-separated r/ list")
    depth:            str = Field("standard", pattern="^(quick|standard|deep)$")


@app.post("/leads/start")
async def leads_start(request: LeadRequest) -> JSONResponse:
    """
    Launch a lead-finding job in the background.
    Returns immediately with {job_id}.
    """
    import uuid
    job_id = str(uuid.uuid4())

    job = _lf.LeadJob(
        job_id           = job_id,
        saas_description = request.saas_description.strip(),
        target_customer  = request.target_customer.strip(),
        problems         = request.problems.strip(),
        subreddits       = request.subreddits.strip(),
        depth            = request.depth,
    )
    _lf.get_lead_jobs()[job_id] = job

    async def _bg():
        await _lf.run_lead_finder(job_id)

    asyncio.create_task(_bg())
    logger.info("[Leads] Job %s started (depth=%s)", job_id, request.depth)
    return JSONResponse({"job_id": job_id})


@app.get("/leads/status/{job_id}")
async def leads_status(job_id: str) -> JSONResponse:
    """
    Poll live progress of a running lead-finding job.
    """
    job = _lf.get_lead_jobs().get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Lead job not found.")
    return JSONResponse({
        "job_id":        job.job_id,
        "status":        job.status,
        "substatus":     job.substatus,
        "error":         job.error,
        "urls_found":    job.urls_found,
        "posts_analyzed": job.posts_analyzed,
        "leads_found":   len(job.leads),
        "total_queries": job.total_queries,
        "queries_done":  job.queries_done,
        "queries":       job.queries,
    })


@app.get("/leads/results/{job_id}")
async def leads_results(job_id: str) -> JSONResponse:
    """
    Return the full sorted leads list once the job is complete.
    """
    job = _lf.get_lead_jobs().get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Lead job not found.")
    if job.status != _lf.STATUS_COMPLETE:
        raise HTTPException(status_code=409, detail="Job not yet complete.")
    return JSONResponse({"leads": job.leads, "total": len(job.leads)})


# ── Message Builder endpoints ──────────────────────────────────────────────────

class GenerateMessageRequest(BaseModel):
    lead:             dict  = Field(...)
    saas_description: str   = Field(..., min_length=5)


class GenerateAllMessagesRequest(BaseModel):
    job_id:           str   = Field(...)
    saas_description: str   = Field(..., min_length=5)


@app.post("/leads/generate-message")
async def leads_generate_message(request: GenerateMessageRequest) -> JSONResponse:
    """
    Generate 3 personalized message versions for a single lead.
    Runs Gemini calls in parallel; responds when all 3 are done.
    """
    try:
        result = await _lf.generate_outreach_message(
            lead             = request.lead,
            saas_description = request.saas_description.strip(),
        )
        return JSONResponse(result)
    except Exception as exc:
        logger.error("[Leads] Message generation failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/leads/generate-all-messages")
async def leads_generate_all(request: GenerateAllMessagesRequest) -> JSONResponse:
    """
    Generate messages for all HIGH PRIORITY leads (score >= 8) from a job.
    Runs up to 3 leads concurrently.
    """
    job = _lf.get_lead_jobs().get(request.job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Lead job not found.")
    if job.status != _lf.STATUS_COMPLETE:
        raise HTTPException(status_code=409, detail="Job not yet complete.")

    high_priority = [l for l in job.leads if l.get("score", 0) >= 8]
    if not high_priority:
        return JSONResponse({"results": [], "total": 0, "message": "No high-priority leads found."})

    try:
        results = await _lf.generate_messages_batch(
            leads            = high_priority,
            saas_description = request.saas_description.strip(),
        )
        # Cache on the job so export can use it
        job._batch_messages = results  # type: ignore[attr-defined]
        return JSONResponse({
            "results": results,
            "total":   len(results),
            "message": (
                f"Generated {len(results)} personalized messages. "
                "Each one references something different from what that person said. "
                "No two are alike."
            ),
        })
    except Exception as exc:
        logger.error("[Leads] Batch generation failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/leads/export-messages/{job_id}")
async def leads_export_messages(job_id: str) -> Response:
    """
    Export all batch-generated messages for a job as a DOCX file.
    Must call /leads/generate-all-messages first.
    """
    job = _lf.get_lead_jobs().get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Lead job not found.")

    batch = getattr(job, "_batch_messages", None)
    if not batch:
        raise HTTPException(status_code=409, detail="No batch messages generated yet. Call /leads/generate-all-messages first.")

    import tempfile, os as _os
    with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        _lf.generate_messages_docx(batch, tmp_path)
        with open(tmp_path, "rb") as fh:
            docx_bytes = fh.read()
    finally:
        try:
            _os.unlink(tmp_path)
        except Exception:
            pass

    return Response(
        content     = docx_bytes,
        media_type  = "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers     = {"Content-Disposition": f"attachment; filename=\"outreach_messages_{job_id[:8]}.docx\""},
    )


# ── Pipeline endpoints ─────────────────────────────────────────────────────────

class FollowupRequest(BaseModel):
    lead:             dict = Field(...)
    saas_description: str  = Field(..., min_length=3)


class ExportPipelineRequest(BaseModel):
    stages:           dict = Field(...)
    profile_name:     str  = Field(default="My Validation Project")
    saas_description: str  = Field(default="")
    target_customer:  str  = Field(default="")


@app.post("/leads/generate-followup")
async def leads_generate_followup(request: FollowupRequest) -> JSONResponse:
    """Generate a 1-sentence follow-up message for a lead who hasn't replied."""
    try:
        message = await _lf.generate_followup_message(
            lead             = request.lead,
            saas_description = request.saas_description.strip(),
        )
        return JSONResponse({"message": message})
    except Exception as exc:
        logger.error("[Leads] Follow-up generation failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/leads/export-pipeline")
async def leads_export_pipeline(request: ExportPipelineRequest) -> Response:
    """Export the full pipeline as a structured market-validation DOCX report."""
    import tempfile, os as _os
    with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        await asyncio.to_thread(
            _lf.export_pipeline_docx,
            request.stages,
            request.profile_name,
            request.saas_description,
            request.target_customer,
            tmp_path,
        )
        with open(tmp_path, "rb") as fh:
            docx_bytes = fh.read()
    finally:
        try:
            _os.unlink(tmp_path)
        except Exception:
            pass

    safe_name = request.profile_name.replace(" ", "_")[:30]
    return Response(
        content    = docx_bytes,
        media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers    = {"Content-Disposition": f"attachment; filename=\"pipeline_{safe_name}.docx\""},
    )


# ── Subreddit Bulk Extractor endpoints ────────────────────────────────────────

class BulkExtractRequest(BaseModel):
    """
    Request body for POST /bulk-extract/start.

    subreddits  – list of subreddit names WITHOUT the 'r/' prefix.
    comment_sort – sort mode for comments (top / new / controversial / old / qa / confidence).
    comment_limit – top-level comments per post (positive int or 'all').
    post_limit_per_subreddit – safety cap (default 500).
    years_back – how many years back to extract (default 2).
    """
    subreddits:                list[str] = Field(..., min_length=1)
    comment_sort:              str       = Field("top")
    comment_limit:             Any       = Field(25)
    post_limit_per_subreddit:  int       = Field(500, ge=1, le=5000)
    years_back:                float     = Field(2, ge=0.1, le=10)

    @field_validator("subreddits")
    @classmethod
    def strip_subreddits(cls, v: list[str]) -> list[str]:
        cleaned: list[str] = []
        for name in v:
            name = name.strip().lstrip("r/").lstrip("/").strip()
            if name:
                cleaned.append(name)
        if not cleaned:
            raise ValueError("subreddits list must contain at least one non-empty name.")
        return cleaned

    @field_validator("comment_sort")
    @classmethod
    def validate_sort(cls, v: str) -> str:
        valid = {"confidence", "top", "new", "controversial", "old", "qa", "best"}
        if v.lower() not in valid:
            raise ValueError(f"comment_sort must be one of {valid}")
        return v.lower()

    @field_validator("comment_limit")
    @classmethod
    def validate_limit(cls, v: Any) -> Any:
        if str(v).lower() == "all":
            return "all"
        try:
            val = int(v)
            if val < 1:
                raise ValueError()
            return val
        except (ValueError, TypeError):
            raise ValueError("comment_limit must be a positive integer or 'all'")


@app.post("/bulk-extract/start", status_code=202)
async def bulk_extract_start(request: BulkExtractRequest) -> JSONResponse:
    """
    Launch a bulk subreddit extraction job in the background.

    Immediately returns {job_id} — poll GET /bulk-extract/status/{job_id}
    for live progress, then fetch individual files from
    GET /bulk-extract/download/{job_id}/{filename}.
    """
    job_id = str(uuid.uuid4())

    job = _sbe.BulkJob(
        job_id=job_id,
        subreddits=request.subreddits,
    )
    _sbe.get_bulk_jobs()[job_id] = job

    async def _bg() -> None:
        try:
            await _sbe.bulk_extract_subreddits(
                subreddits=request.subreddits,
                comment_sort=request.comment_sort,
                comment_limit=request.comment_limit,
                post_limit_per_subreddit=request.post_limit_per_subreddit,
                years_back=request.years_back,
                job_id=job_id,
                output_dir=OUTPUT_DIR,
            )
        except Exception as exc:
            logger.error("[BulkExtract Job %s] Unhandled error: %s", job_id, exc)
            job.status = _sbe.STATUS_ERROR
            job.error  = str(exc)

    asyncio.create_task(_bg())
    logger.info(
        "[BulkExtract] Job %s started — %d subreddit(s): %s",
        job_id, len(request.subreddits), request.subreddits,
    )
    return JSONResponse({
        "job_id":   job_id,
        "status":   _sbe.STATUS_COLLECTING,
        "message":  (
            f"Bulk extraction job queued for {len(request.subreddits)} subreddit(s). "
            f"Poll GET /bulk-extract/status/{job_id} for live progress."
        ),
        "subreddits": request.subreddits,
    })


@app.get("/bulk-extract/status/{job_id}")
async def bulk_extract_status(job_id: str) -> JSONResponse:
    """
    Returns the live progress state of a bulk extraction job.

    While running::

        {
          "status": "extracting",
          "current_subreddit": "personaltraining",
          "subreddit_index": 1,
          "total_subreddits": 3,
          "posts_done_this_sub": 45,
          "total_posts_this_sub": 500,
          "total_posts_done": 45,
          "total_posts_all_subs": 1200,
          "percent": 3.75,
          "eta_minutes": 87
        }

    When complete, ``docx_files`` lists the absolute server-side paths of all
    generated files; use GET /bulk-extract/download/{job_id}/{filename} to
    retrieve each one.
    """
    job = _sbe.get_bulk_jobs().get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Bulk job '{job_id}' not found.")
    return JSONResponse(content=job.to_status_dict())


@app.get("/bulk-extract/download/{job_id}/{filename}")
async def bulk_extract_download(job_id: str, filename: str) -> FileResponse:
    """
    Streams one of the generated DOCX files produced by a completed bulk-extract
    job as an attachment download.

    ``filename`` must be the basename of one of the paths returned by
    ``GET /bulk-extract/status/{job_id}`` → ``docx_files``.

    Returns 404 if the job or file is not found, 425 if still processing,
    500 if the file has been deleted from disk.
    """
    job = _sbe.get_bulk_jobs().get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Bulk job '{job_id}' not found.")

    if job.status not in (_sbe.STATUS_COMPLETE, _sbe.STATUS_ERROR):
        raise HTTPException(
            status_code=425,
            detail=(
                f"Job is still {job.status}. "
                f"Poll /bulk-extract/status/{job_id} and retry when complete."
            ),
        )

    if job.status == _sbe.STATUS_ERROR:
        raise HTTPException(
            status_code=500,
            detail=job.error or "Bulk extraction job did not complete successfully.",
        )

    # Locate the requested file among the job's outputs
    matched_path: Optional[str] = None
    for path in job.docx_files:
        if os.path.basename(path) == filename:
            matched_path = path
            break

    if matched_path is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"File '{filename}' was not produced by job '{job_id}'. "
                "Check docx_files in the status response for valid filenames."
            ),
        )

    if not os.path.isfile(matched_path):
        raise HTTPException(
            status_code=500,
            detail="Generated file not found on disk. The server may have restarted.",
        )

    logger.info("[BulkExtract] Serving download: job=%s file=%s", job_id, filename)
    return FileResponse(
        path=matched_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=filename,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/bulk-extract", status_code=202)
async def bulk_extract(request: BulkExtractRequest) -> JSONResponse:
    """
    User-facing entry point for the Bulk Archive feature.

    Stricter limits than /bulk-extract/start:
      - max 10 subreddits per job
      - post_limit_per_subreddit max 1000
      - years_back max 5

    Returns: {"job_id": "...", "status": "started", "estimated_posts": null}
    """
    if len(request.subreddits) > 10:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Maximum 10 subreddits per job (got {len(request.subreddits)}). "
                "Archiving more than 10 subreddits in one job could take many hours. "
                "Split into multiple jobs."
            ),
        )
    if request.post_limit_per_subreddit > 1000:
        raise HTTPException(
            status_code=422,
            detail="post_limit_per_subreddit must be ≤ 1000 for the Bulk Archive feature.",
        )
    if request.years_back > 5:
        raise HTTPException(
            status_code=422,
            detail="years_back must be ≤ 5.",
        )

    job_id = str(uuid.uuid4())
    job = _sbe.BulkJob(job_id=job_id, subreddits=request.subreddits)
    _sbe.get_bulk_jobs()[job_id] = job

    async def _bg() -> None:
        try:
            await _sbe.bulk_extract_subreddits(
                subreddits=request.subreddits,
                comment_sort=request.comment_sort,
                comment_limit=request.comment_limit,
                post_limit_per_subreddit=request.post_limit_per_subreddit,
                years_back=request.years_back,
                job_id=job_id,
                output_dir=OUTPUT_DIR,
            )
        except Exception as exc:
            logger.error("[BulkArchive Job %s] Unhandled error: %s", job_id, exc)
            job.status = _sbe.STATUS_ERROR
            job.error  = str(exc)

    asyncio.create_task(_bg())
    logger.info(
        "[BulkArchive] Job %s started — %d subreddit(s): %s",
        job_id, len(request.subreddits), request.subreddits,
    )
    return JSONResponse({
        "job_id":           job_id,
        "status":           "started",
        "estimated_posts":  None,
        "subreddits":       request.subreddits,
    })


@app.get("/bulk-download/{job_id}/{file_index}")
async def bulk_download_by_index(job_id: str, file_index: int) -> FileResponse:
    """
    Downloads one of the generated DOCX files by its zero-based index in the
    ``download_urls`` array returned by GET /bulk-extract/status/{job_id}.

    Returns 404 if the job or file index is not found, 425 if still processing.
    """
    job = _sbe.get_bulk_jobs().get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Bulk job '{job_id}' not found.")

    if job.status not in (_sbe.STATUS_COMPLETE, _sbe.STATUS_ERROR):
        raise HTTPException(
            status_code=425,
            detail=f"Job is still {job.status}. Retry when complete.",
        )

    if not job.docx_files or file_index >= len(job.docx_files):
        raise HTTPException(
            status_code=404,
            detail=(
                f"File index {file_index} is out of range. "
                f"This job produced {len(job.docx_files)} file(s) (0-indexed)."
            ),
        )

    file_path = job.docx_files[file_index]
    if not os.path.isfile(file_path):
        raise HTTPException(
            status_code=500,
            detail="Generated file not found on disk. The server may have restarted.",
        )

    filename = os.path.basename(file_path)
    logger.info(
        "[BulkArchive] Serving file %d of job %s: %s", file_index, job_id, filename
    )
    return FileResponse(
        path=file_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=filename,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── YouTube Comment Archiver ───────────────────────────────────────────────────

# Job status values (reuse existing STATUS_* where possible)
YT_STATUS_QUEUED     = "queued"
YT_STATUS_RUNNING    = "running"
YT_STATUS_GENERATING = "generating"
YT_STATUS_COMPLETE   = "complete"
YT_STATUS_ERROR      = "error"

_yt_jobs: dict[str, dict] = {}   # job_id → state dict


class YoutubeRequest(BaseModel):
    channels:               list[str] = Field(..., min_length=1)
    max_videos_per_channel: int       = Field(25, ge=1, le=50)
    max_comments_per_video: Any       = Field("all")  # int or "all"

    @field_validator("channels")
    @classmethod
    def clean_channels(cls, v: list[str]) -> list[str]:
        cleaned = [c.strip() for c in v if c.strip()]
        if not cleaned:
            raise ValueError("channels list must contain at least one non-empty name.")
        if len(cleaned) > 20:
            raise ValueError("Maximum 20 channels per job.")
        return cleaned

    @field_validator("max_comments_per_video")
    @classmethod
    def validate_max_comments(cls, v: Any) -> Any:
        if str(v).lower() == "all":
            return None   # None = no limit inside extractor
        try:
            val = int(v)
            if val < 1:
                raise ValueError()
            return val
        except (ValueError, TypeError):
            raise ValueError("max_comments_per_video must be a positive integer or 'all'")


async def _run_youtube_job(job_id: str, request: YoutubeRequest) -> None:
    """Background task: extract channels then build DOCX."""
    job = _yt_jobs[job_id]
    channels_data: list[dict] = []
    total = len(request.channels)

    try:
        for idx, channel_name in enumerate(request.channels, 1):
            job["status"]    = YT_STATUS_RUNNING
            job["substatus"] = f"Processing channel {idx}/{total}: {channel_name}…"
            job["channel_index"] = idx
            job["channel_name"]  = channel_name

            def _cb(msg: str) -> None:
                job["substatus"] = msg

            channel_data = await _yt.extract_channel(
                channel_name,
                max_videos=request.max_videos_per_channel,
                max_comments_per_video=request.max_comments_per_video,
                status_cb=_cb,
            )
            channels_data.append(channel_data)
            job["channels_done"] = idx

        # DOCX generation
        job["status"]    = YT_STATUS_GENERATING
        job["substatus"] = "Building DOCX report…"

        def _docx_progress(ch_idx: int, total_ch: int, name: str):
            job["substatus"] = f"Writing channel {ch_idx}/{total_ch}: {name}…"

        file_path: str = await asyncio.to_thread(
            generate_youtube_docx,
            channels_data,
            OUTPUT_DIR,
            _docx_progress,
        )

        job["status"]        = YT_STATUS_COMPLETE
        job["substatus"]     = None
        job["download_path"] = file_path
        logger.info("[YouTube Job %s] ✅ Complete — DOCX: %s", job_id, file_path)

    except Exception as exc:
        logger.error("[YouTube Job %s] Error: %s", job_id, exc)
        job["status"]    = YT_STATUS_ERROR
        job["error"]     = str(exc)
        job["substatus"] = None


@app.post("/youtube/extract", status_code=202)
async def youtube_extract(request: YoutubeRequest) -> JSONResponse:
    """
    Start a YouTube comment extraction job.
    Returns immediately with {job_id}.
    Poll GET /youtube/status/{job_id} for progress.
    """
    api_key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail=(
                "YOUTUBE_API_KEY is not configured on this server. "
                "Add it to your .env file and restart."
            ),
        )

    job_id = str(uuid.uuid4())
    _yt_jobs[job_id] = {
        "job_id":         job_id,
        "status":         YT_STATUS_QUEUED,
        "substatus":      None,
        "error":          None,
        "channels":       request.channels,
        "total_channels": len(request.channels),
        "channels_done":  0,
        "channel_index":  0,
        "channel_name":   "",
        "download_path":  None,
    }
    asyncio.create_task(_run_youtube_job(job_id, request))
    logger.info(
        "[YouTube] Job %s started — %d channel(s): %s",
        job_id, len(request.channels), request.channels,
    )
    return JSONResponse({
        "job_id":    job_id,
        "status":    YT_STATUS_QUEUED,
        "message":   (
            f"YouTube extraction queued for {len(request.channels)} channel(s). "
            f"Poll GET /youtube/status/{job_id} for progress."
        ),
        "channels":  request.channels,
    })


@app.get("/youtube/status/{job_id}")
async def youtube_status(job_id: str) -> JSONResponse:
    """Returns live progress of a YouTube extraction job."""
    job = _yt_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"YouTube job '{job_id}' not found.")

    resp = {
        "job_id":         job["job_id"],
        "status":         job["status"],
        "substatus":      job["substatus"],
        "error":          job["error"],
        "total_channels": job["total_channels"],
        "channels_done":  job["channels_done"],
        "channel_name":   job["channel_name"],
    }
    if job["status"] == YT_STATUS_COMPLETE:
        filename = os.path.basename(job["download_path"])
        resp["download_url"] = f"/youtube/download/{job_id}/{filename}"
    return JSONResponse(resp)


@app.get("/youtube/download/{job_id}/{filename}")
async def youtube_download(job_id: str, filename: str) -> FileResponse:
    """Stream the generated YouTube Comment Archive DOCX."""
    job = _yt_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"YouTube job '{job_id}' not found.")

    if job["status"] not in (YT_STATUS_COMPLETE, YT_STATUS_ERROR):
        raise HTTPException(
            status_code=425,
            detail=f"Job is still {job['status']}. Poll /youtube/status/{job_id} and retry.",
        )
    if job["status"] == YT_STATUS_ERROR:
        raise HTTPException(status_code=500, detail=job["error"] or "Job failed.")

    path = job.get("download_path")
    if not path or not os.path.isfile(path):
        raise HTTPException(status_code=500, detail="File not found on disk.")

    logger.info("[YouTube] Serving download: job=%s file=%s", job_id, filename)
    return FileResponse(
        path=path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=filename,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

