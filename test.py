import asyncio
import logging

from backend.google_crawler import crawl_google_for_reddit_urls
from backend.reddit_extractor import extract_multiple_posts, extract_post
import backend.reddit_extractor as re_ext

logging.basicConfig(level=logging.DEBUG)

async def check():
    urls = await crawl_google_for_reddit_urls('Meetings are the productivity killer nobody talks about', max_pages=1, headless=True)
    print(f'Found {len(urls)} urls')
    if urls:
        test_urls = urls[:5]
        for url in test_urls:
            print("---- Testing URL:", url)
            post = await extract_post(url)
            print("Is None?", post is None)

asyncio.run(check())
