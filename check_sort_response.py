import asyncio, httpx, json
import sys

headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"}

async def main():
    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        # 1. Get a popular post on AskReddit
        res = await client.get('https://www.reddit.com/r/AskReddit/hot.json?limit=1')
        listings = res.json()
        post = listings['data']['children'][0]['data']
        post_id = post['id']
        post_title = post['title']
        post_url = f"https://www.reddit.com/r/AskReddit/comments/{post_id}.json"
        
        print(f"Testing post: {post_title} ({post_url})")
        
        # 2. Results
        results = {}
        
        for sort_mode in ['best', 'confidence', 'top', 'new']:
            res = await client.get(f"{post_url}?sort={sort_mode}&limit=10")
            data = res.json()
            if isinstance(data, list) and len(data) > 1:
                listing_data = data[1]['data']
                actual_sort = listing_data.get('sort')
                comments = listing_data.get('children', [])
                authors = [c['data'].get('author') for c in comments[:5] if c['kind'] == 't1']
                results[sort_mode] = {'actual_sort': actual_sort, 'authors': authors}
            else:
                results[sort_mode] = 'ERROR structure'
        
        with open("sort_research.json", "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)

if __name__ == '__main__':
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())
