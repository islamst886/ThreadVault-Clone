import asyncio
from backend.reddit_extractor import extract_post

async def main():
    url = 'https://www.reddit.com/r/salesforce/comments/wdii87/meetingd_to_death/'
    post = await extract_post(url)
    print('Post is not None:', post is not None)
    
asyncio.run(main())
