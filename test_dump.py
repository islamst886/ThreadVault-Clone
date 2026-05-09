import asyncio
from playwright.async_api import async_playwright

async def dump():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            locale="en-US"
        )
        page = await context.new_page()
        await page.goto("https://www.google.com/search?q=best+mechanical+keyboards+2024+site%3Areddit.com", wait_until="domcontentloaded")
        
        html = await page.content()
        with open("google_dump.html", "w", encoding="utf-8") as f:
            f.write(html)
        print("Dump complete")

asyncio.run(dump())
