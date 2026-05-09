"""
ai_analyzer.py
--------------
Sends a Reddit post (title + body + top comments) to the Google Gemini API
for structured AI analysis.

The analysis result is a JSON object with fields:
  category, pain_points, mentioned_products, solution_requests,
  willingness_to_pay, sentiment, summary, key_quote

Usage:
    from ai_analyzer import analyze_post, analyze_posts

    # Single post (mutates the post dict in-place, adds "ai_analysis" key)
    await analyze_post(post_dict)

    # Batch (mutates each post dict in-place, 0.5s delay between calls)
    await analyze_posts(posts_list)

Requires:
    GEMINI_API_KEY environment variable (loaded via python-dotenv).
    pip install google-genai
"""

import asyncio
import json
import logging
import os
import re
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ── Model identifier ──────────────────────────────────────────────────────────
# _MODEL_NAME = "gemini-3.1-flash-lite-preview"
_MODEL_NAME = ""

# ── Delay between successive API calls to stay polite ─────────────────────────
_INTER_CALL_DELAY_SEC = 0.5

# ── System prompt ──────────────────────────────────────────────────────────────
_SYSTEM_PROMPT = (
    "You are a Reddit research analyst. Analyze the given Reddit post and classify it. "
    "Respond ONLY in valid JSON, no markdown."
)

# ── User prompt template ───────────────────────────────────────────────────────
_USER_PROMPT_TEMPLATE = """\
Analyze this Reddit post and return a JSON object with exactly these fields:

{{
    "category": one of exactly these values:
        "Pain Point"          - user expressing a problem or frustration
        "Solution Request"    - user asking for tool/product/service recommendations
        "Money Talk"          - user discussing spending, pricing, or willingness to pay
        "Positive Experience" - user praising a product or service
        "Negative Experience" - user complaining about a specific product or service
        "Hot Discussion"      - controversial or highly debated topic
        "Question"            - general question seeking advice
        "News/Update"         - sharing news or an announcement
        "Other"               - does not fit above categories

    "pain_points": list of strings, each a specific problem or frustration mentioned
        in the post or comments. Empty list if none found.

    "mentioned_products": list of product or company names mentioned in the post
        or comments. Empty list if none.

    "solution_requests": list of strings, each a specific tool or solution the author
        or commenters are asking for. Empty list if none.

    "willingness_to_pay": true if anyone in the post or comments mentions paying money,
        pricing, budget, or cost. false otherwise.

    "sentiment": one of "positive", "negative", "neutral", "mixed"

    "summary": a 2-3 sentence plain English summary of what this post is about and
        what the key insight is for a founder doing market research.

    "key_quote": the single most insightful sentence from the post or comments for a
        founder to read. Pick the most emotionally honest or specific one.
}}

Post Title: {title}
Post Body: {body}
Top Comments: {top_comments}"""


# ── Helpers ────────────────────────────────────────────────────────────────────

def _flatten_comments(comments: list[dict], max_count: int = 5) -> list[str]:
    """
    Flattens the nested comment tree into a plain list of body strings,
    picking the top `max_count` depth-0 comments by score.

    Args:
        comments:  List of comment dicts as produced by reddit_extractor.
        max_count: How many top comments to include in the prompt.

    Returns:
        List of formatted comment strings.
    """
    top_level = [c for c in comments if c.get("depth", 0) == 0]
    top_level.sort(key=lambda c: c.get("score", 0), reverse=True)
    bodies: list[str] = []
    for c in top_level[:max_count]:
        body = c.get("body", "").strip()
        if body and body not in ("[removed]", "[deleted]"):
            author = c.get("author", "unknown")
            score  = c.get("score", 0)
            bodies.append(f"u/{author} (+{score}): {body}")
    return bodies


def _build_user_prompt(post: dict) -> str:
    """Constructs the formatted user prompt for a single post."""
    title = post.get("post", {}).get("title", "")
    body  = post.get("post", {}).get("body", "").strip() or "(no body text)"

    comments = post.get("comments", [])
    comment_lines = _flatten_comments(comments, max_count=5)
    top_comments_text = (
        "\n".join(comment_lines) if comment_lines else "(no comments available)"
    )

    return _USER_PROMPT_TEMPLATE.format(
        title=title,
        body=body,
        top_comments=top_comments_text,
    )


def _extract_json(raw_text: str) -> dict:
    """
    Attempts to parse the model's response as JSON.

    As a safety net, strips any accidental ```json ... ``` fences the model
    might include despite the system prompt instruction.

    Args:
        raw_text: The model's raw text response.

    Returns:
        Parsed dict, or raises ValueError/JSONDecodeError on failure.
    """
    text = raw_text.strip()
    # Strip optional markdown code fences
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


# ── Core async function ────────────────────────────────────────────────────────

async def analyze_post(post: dict) -> None:
    """
    Calls the Gemini API to analyze a single Reddit post.

    Mutates `post` in-place by adding an ``"ai_analysis"`` key:
    - On success:  ``"ai_analysis"`` → parsed dict with all 8 classification fields.
    - On failure:  ``"ai_analysis"`` → ``None`` (never crashes the job).

    Args:
        post: A post dict as returned by ``reddit_extractor.extract_post()``.
    """
    title = post.get("post", {}).get("title", "<untitled>")
    logger.info("[AI] Analysing post: %s", title[:80])

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        logger.warning(
            "[AI] GEMINI_API_KEY not set — skipping analysis for '%s'", title[:60]
        )
        post["ai_analysis"] = None
        return

    try:
        # google.genai is the modern replacement for google.generativeai.
        # Import here so the rest of the app still works if the package is missing.
        from google import genai  # type: ignore
        from google.genai import types  # type: ignore

        client = genai.Client(api_key=api_key)

        user_prompt = _build_user_prompt(post)

        # The SDK's generate_content is synchronous — run in a thread pool
        # so we don't block the asyncio event loop.
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=_MODEL_NAME,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM_PROMPT,
                response_mime_type="application/json",
            ),
        )

        raw_text = response.text
        analysis = _extract_json(raw_text)
        post["ai_analysis"] = analysis
        logger.info(
            "[AI] Analysis complete for: %s | category=%s",
            title[:60],
            analysis.get("category", "?"),
        )

    except json.JSONDecodeError as exc:
        logger.warning("[AI] JSON parse error for '%s': %s", title[:60], exc)
        post["ai_analysis"] = None

    except Exception as exc:
        logger.warning("[AI] API call failed for '%s': %s", title[:60], exc)
        post["ai_analysis"] = None


async def analyze_posts(
    posts: list[dict],
    delay_sec: float = _INTER_CALL_DELAY_SEC,
    status_callback: Optional[Callable[[Optional[str]], None]] = None,
) -> None:
    """
    Runs AI analysis on a list of posts sequentially, with a polite delay
    between each call to avoid overwhelming the Gemini API.

    Mutates each post dict in-place (adds ``"ai_analysis"`` key).
    Never raises — failures per post are silently set to ``None``.

    Args:
        posts:           List of post dicts from ``reddit_extractor``.
        delay_sec:       Seconds to sleep between successive API calls (default 0.5).
        status_callback: Optional callable for live UI substatus updates.
    """
    total = len(posts)
    logger.info("[AI] Starting batch analysis: %d posts", total)

    for i, post in enumerate(posts, start=1):
        title = post.get("post", {}).get("title", "<untitled>")

        if status_callback:
            status_callback(f"AI analysis: post {i} of {total} — {title[:50]}…")

        await analyze_post(post)

        # Polite inter-call delay (skip after the last post)
        if i < total:
            await asyncio.sleep(delay_sec)

    if status_callback:
        status_callback(None)

    logger.info("[AI] Batch analysis complete: %d / %d posts processed.", total, total)
