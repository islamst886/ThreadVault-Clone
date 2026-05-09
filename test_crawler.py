import asyncio
from backend.google_crawler import crawl_google_for_reddit_urls
import logging

logging.basicConfig(level=logging.INFO)

async def check():
    urls = await crawl_google_for_reddit_urls("best mechanical keyboards 2024", max_pages=1, headless=True)
    print("Final URLs:", urls)

asyncio.run(check())
