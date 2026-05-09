import asyncio
from playwright.async_api import async_playwright

async def check():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            locale="en-US"
        )
        page = await context.new_page()
        
        await page.goto("https://www.google.com/search?q=best+mechanical+keyboards+2024+site%3Areddit.com", wait_until="domcontentloaded")
        
        # Check all reddit links
        links = await page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
        reddit_links = [l for l in links if "reddit.com/r/" in l and "/comments/" in l]
        print(f"Total reddit links on page: {len(reddit_links)}")
        
        # Check div#search div.g
        blocks = await page.query_selector_all("div#search div.g")
        print(f"Total div#search div.g blocks: {len(blocks)}")
        
        if blocks:
            for i, b in enumerate(blocks):
                b_links = await b.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
                rl = [l for l in b_links if "reddit.com/r/" in l and "/comments/" in l]
                print(f"Block {i} has {len(rl)} reddit links")
        
        # Check alternative selectors for main search results
        blocks2 = await page.query_selector_all("div#rso > div")
        print(f"Total div#rso > div blocks: {len(blocks2)}")
        if blocks2:
            for i, b in enumerate(blocks2):
                b_links = await b.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
                rl = [l for l in b_links if "reddit.com/r/" in l and "/comments/" in l]
                if rl: print(f"rso Block {i} has {len(rl)} reddit links: {rl[0]}")
        
        print(await page.title())

asyncio.run(check())
