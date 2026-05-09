"""
google_crawler.py
-----------------
Crawls Google Search results to collect Reddit post URLs for a given query.

Strategy:
  - Appends "site:reddit.com" to the user's query so Google returns only Reddit results.
  - Uses Playwright (headless Chromium) to render each results page.
  - Extracts and filters links, keeping only actual Reddit post URLs (/comments/).
  - Clicks "Next" to paginate, up to a configurable max-page limit.
  - Adds a random 2–5s delay between pages to reduce rate-limiting risk.
  - Detects Google CAPTCHA pages and exits gracefully.
"""

import asyncio
import logging
import random
import re
from typing import Optional, Callable
from urllib.parse import quote_plus, urlparse, urlunparse, parse_qs, urlencode

from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeoutError

# ── Logging ────────────────────────────────────────────────────────────────────
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# ── Constants ──────────────────────────────────────────────────────────────────
GOOGLE_SEARCH_URL = "https://www.google.com/search?q={query}&hl=en"
REDDIT_POST_PATTERN = re.compile(
    r"^https?://(www\.)?reddit\.com/r/[^/]+/comments/[^/]+",
    re.IGNORECASE,
)
CAPTCHA_INDICATORS = [
    "sorry/index",           # accounts.google.com/sorry/index
    "recaptcha",
    "unusual traffic",
    "captcha",
]
DEFAULT_MAX_PAGES = 15
DELAY_MIN_SEC = 2
DELAY_MAX_SEC = 5


# ── Pure / unit-testable helpers ───────────────────────────────────────────────

def build_google_query(user_query: str) -> str:
    """
    Appends 'site:reddit.com' to the user's query and URL-encodes the result.

    Args:
        user_query: Raw keyword string from the user.

    Returns:
        URL-encoded query string ready for insertion into a Google Search URL.

    Example:
        >>> build_google_query("best productivity apps")
        'best+productivity+apps+site%3Areddit.com'
    """
    query_lower = user_query.strip().lower()
    if "site:reddit.com" not in query_lower:
        full_query = f"{user_query.strip()} site:reddit.com"
    else:
        full_query = user_query.strip()
    return quote_plus(full_query)


def is_reddit_post_url(url: str) -> bool:
    """
    Returns True only for URLs that are real Reddit post pages (contain /comments/).

    Filters out subreddit homepages, user pages, wiki pages, etc.

    Args:
        url: Candidate URL string.

    Returns:
        True if the URL matches the Reddit post pattern, False otherwise.

    Example:
        >>> is_reddit_post_url("https://www.reddit.com/r/python/comments/abc123/title/")
        True
        >>> is_reddit_post_url("https://www.reddit.com/r/python/")
        False
    """
    return bool(REDDIT_POST_PATTERN.match(url))


def is_captcha_page(page_url: str, page_text: str) -> bool:
    """
    Heuristically detects whether Google has shown a CAPTCHA / bot-detection page.

    Args:
        page_url:  The current page URL.
        page_text: Lowercased visible text content of the page.

    Returns:
        True if a CAPTCHA is detected, False otherwise.
    """
    combined = (page_url + " " + page_text).lower()
    return any(indicator in combined for indicator in CAPTCHA_INDICATORS)


def clean_google_redirect_url(raw_url: str) -> str:
    """
    Google sometimes wraps result links inside a /url?q=... redirect.
    This function unwraps those redirects to return the real destination URL.

    Args:
        raw_url: Raw href from a Google search result anchor.

    Returns:
        The real destination URL, or the original URL if not a redirect.
    """
    if raw_url.startswith("/url?"):
        parsed = urlparse("https://www.google.com" + raw_url)
        qs = parse_qs(parsed.query)
        if "q" in qs:
            return qs["q"][0]
    return raw_url


def normalize_reddit_post_url(url: str) -> str:
    """
    Reduces a Reddit post URL to a canonical key for deduplication purposes.

    Google surfaces the same post under many URL variants:
      - Different title slugs after the post ID
      - Trailing slash present or absent
      - www / old / np / out subdomain variants

    Strategy: keep only the scheme + www.reddit.com + /r/<sub>/comments/<id>/
    Everything after the post ID (title slug, query params, fragments) is dropped.

    Args:
        url: Any Reddit post URL string.

    Returns:
        Canonical URL string used as the dedup key (not used for actual requests).
    """
    try:
        parsed = urlparse(url)
        # Normalise all reddit subdomains to www.reddit.com
        netloc = "www.reddit.com"
        # Split path into non-empty segments
        # Expected: ['r', '<subreddit>', 'comments', '<post_id>', '<optional_slug...>']
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 4 and parts[2].lower() == "comments":
            # Keep only /r/<sub>/comments/<id>/ — drop slug and anything after
            canonical_path = "/" + "/".join(parts[:4]) + "/"
        else:
            canonical_path = parsed.path.rstrip("/") + "/"
        return urlunparse(("https", netloc, canonical_path, "", "", ""))
    except Exception:
        return url  # Fall back to raw URL if parsing fails


def deduplicate(urls: list[str]) -> list[str]:
    """
    Removes duplicate URLs while preserving insertion order.

    Uses the canonical post ID path (via normalize_reddit_post_url) as the
    uniqueness key so that the same post linked with different title slugs,
    subdomains, or trailing-slash variants is treated as one entry.

    Args:
        urls: List of URL strings, potentially containing duplicates.

    Returns:
        Deduplicated list maintaining original order.
    """
    seen: set[str] = set()
    result: list[str] = []
    for url in urls:
        key = normalize_reddit_post_url(url)
        if key not in seen:
            seen.add(key)
            result.append(url)
    return result


# ── Playwright helpers ─────────────────────────────────────────────────────────

async def extract_reddit_urls_from_page(page: Page) -> list[str]:
    """
    Scrapes all anchor hrefs from the current Google results page and filters
    for valid Reddit post URLs.
    
    Extracts only the primary URL from the main organic result blocks (div#search div.g)
    to avoid collecting filler URLs from "People Also Ask" or Sitelinks.

    Args:
        page: The current Playwright Page object.

    Returns:
        List of Reddit post URLs found on this page.
    """
    # 1. Gather all Reddit URLs on the page (for logging/exclusion counting)
    all_raw_hrefs: list[str] = await page.eval_on_selector_all(
        "a[href]",
        "elements => elements.map(el => el.getAttribute('href'))"
    )
    total_page_reddit_urls = 0
    for href in all_raw_hrefs:
        if href and is_reddit_post_url(clean_google_redirect_url(href)):
            total_page_reddit_urls += 1

    # 2. Extract only the primary link from organic result blocks
    # Added .MjjYud fallback because Google modern layouts often omit .g 
    # when grouping "site:" results into Discussion/Forum blocks.
    reddit_urls: list[str] = []
    blocks = await page.query_selector_all("div#search div.g, div#search div.MjjYud")
    
    for block in blocks:
        block_hrefs = await block.eval_on_selector_all(
            "a[href]",
            "elements => elements.map(el => el.getAttribute('href'))"
        )
        for href in block_hrefs:
            if not href:
                continue
            clean = clean_google_redirect_url(href)
            if is_reddit_post_url(clean):
                reddit_urls.append(clean)
                break  # Stop at the first valid Reddit post URL per block

    # 3. Calculate and log exclusions
    excluded_count = max(0, total_page_reddit_urls - len(reddit_urls))
    logger.info(
        "Found %d Reddit post URLs in main results (excluded %d URLs from feature boxes)",
        len(reddit_urls), excluded_count
    )
    
    return reddit_urls


async def click_next_page(page: Page) -> bool:
    """
    Attempts to locate and click Google's "Next" pagination button.

    Args:
        page: The current Playwright Page object.

    Returns:
        True if "Next" was found and clicked, False if no more pages exist.
    """
    # Google's "Next" button sits inside <a id="pnnext"> or a <td class="b"> cell
    selectors = [
        "a#pnnext",                # Classic desktop layout
        "a[aria-label='Next page']",
        "a[aria-label='Next']",
        "td.b a",                  # Older Google layout fallback
    ]

    for selector in selectors:
        next_btn = await page.query_selector(selector)
        if next_btn:
            await next_btn.click()
            logger.info("Clicked 'Next' – navigating to next page...")
            return True

    logger.info("No 'Next' button found – reached the last page.")
    return False


# ── Main crawler entry point ───────────────────────────────────────────────────

async def crawl_multiple_google_queries(
    user_queries: list[str],
    max_pages: int = DEFAULT_MAX_PAGES,
    headless: bool = True,
    status_cb: Optional[Callable[[int, str], None]] = None,
) -> list[str]:
    """
    Main crawler function optimized for bulk querying.

    Performs a Google Search restricted to reddit.com for a list of queries,
    using a single browser context so CAPTCHA solves and cookies persist across all queries.
    """
    collected_urls: list[str] = []

    logger.info("Starting Google crawl for %d queries", len(user_queries))
    logger.info("Max pages: %d | Headless: %s", max_pages, headless)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()

        for idx, user_query in enumerate(user_queries, 1):
            if status_cb:
                status_cb(idx, user_query)
                
            encoded_query = build_google_query(user_query)
            start_url = GOOGLE_SEARCH_URL.format(query=encoded_query)

            logger.info("Opening: %s", start_url)
            await page.goto(start_url, wait_until="domcontentloaded", timeout=30_000)

            for page_num in range(1, max_pages + 1):
                logger.info("── Scraping Query %d/%d - page %d / %d ──", idx, len(user_queries), page_num, max_pages)

                # ── CAPTCHA check ──────────────────────────────────────────────────
                current_url = page.url
                page_text = await page.inner_text("body") if await page.query_selector("body") else ""
                if is_captcha_page(current_url, page_text):
                    if not headless:
                        logger.warning(
                            "CAPTCHA detected! Since headless=False, waiting 180 seconds for you to manually solve it in the browser window..."
                        )
                        try:
                            # Wait for Google search results to appear (meaning the user solved it)
                            await page.wait_for_selector("div#search", timeout=180_000)
                            logger.info("CAPTCHA solved! Resuming crawl...")
                        except PlaywrightTimeoutError:
                            logger.error("CAPTCHA not solved within 180s. Aborting.")
                            break
                    else:
                        logger.warning("CAPTCHA detected. Stopping crawl early to avoid ban.")
                        break

                # ── Wait for results to load ───────────────────────────────────────
                try:
                    await page.wait_for_selector("a[href]", timeout=10_000)
                except PlaywrightTimeoutError:
                    logger.warning("Timed out waiting for results on page %d. Stopping.", page_num)
                    break

                # ── Extract URLs from this page ────────────────────────────────────
                page_urls = await extract_reddit_urls_from_page(page)
                collected_urls.extend(page_urls)

                # ── Stop if we have reached the last requested page ────────────────
                if page_num >= max_pages:
                    logger.info("Reached max page limit (%d). Stopping.", max_pages)
                    break

                # ── Try to go to the next page ─────────────────────────────────────
                has_next = await click_next_page(page)
                if not has_next:
                    break  # No more pages

                # ── Polite random delay to reduce rate-limiting risk ───────────────
                delay = random.uniform(DELAY_MIN_SEC, DELAY_MAX_SEC)
                logger.info("Waiting %.2f seconds before next page...", delay)
                await asyncio.sleep(delay)

                # ── Wait for next page to fully load ──────────────────────────────
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=15_000)
                except PlaywrightTimeoutError:
                    logger.warning("Page %d failed to load after navigation. Stopping.", page_num + 1)
                    break

            # Polite delay before processing the next search query
            if idx < len(user_queries):
                inter_query_delay = random.uniform(3.0, 6.0)
                logger.info("Waiting %.2f seconds before executing next search query...", inter_query_delay)
                await asyncio.sleep(inter_query_delay)

        await browser.close()

    result = deduplicate(collected_urls)
    logger.info("Crawl complete. Total unique Reddit post URLs found: %d", len(result))
    return result


def crawl_multiple_google_queries_sync(*args, **kwargs) -> list[str]:
    """
    Synchronous wrapper for crawl_multiple_google_queries.
    Ensures that a fresh WindowsProactorEventLoop is used in the background thread.
    This prevents NotImplementedError when Uvicorn runs under a SelectorEventLoop.
    """
    import sys
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    return asyncio.run(crawl_multiple_google_queries(*args, **kwargs))


# ── CLI convenience runner ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    query = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "best productivity apps"
    urls = crawl_multiple_google_queries_sync([query], max_pages=5, headless=False)
    print("\n=== Collected Reddit Post URLs ===")
    for u in urls:
        print(u)
    print(f"\nTotal: {len(urls)} URLs")
