import asyncio
import json
import os
import random
from datetime import datetime, timezone
import httpx

REDDIT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,image/apng,*/*;"
        "q=0.8,application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
    "DNT": "1",
}

async def warm_up_session(client):
    try:
        await client.get("https://www.reddit.com", timeout=10.0)
        await asyncio.sleep(2.0)
        print("✅ Reddit session established")
    except:
        print("⚠  Could not warm up session, continuing anyway")

CATEGORIES = {
    "fitness_health": ["fitness", "gym", "workout", "running", "weightloss", "diet", "health", "yoga", "crossfit", "bodybuilding", "nutrition"],
    "business_startup": ["entrepreneur", "startup", "business", "smallbusiness", "freelance", "ecommerce", "marketing", "sales"],
    "tech_software": ["programming", "webdev", "coding", "python", "javascript", "devops", "SaaS", "nocode", "software", "tech", "linux", "cybersecurity"],
    "finance_money": ["investing", "personalfinance", "stocks", "crypto", "frugal", "fire", "realestate"],
    "creative_design": ["design", "photography", "art", "illustration", "music", "writing", "filmmaking"],
    "lifestyle": ["travel", "food", "cooking", "fashion", "homeimprovement", "gardening", "pets", "parenting"],
    "gaming": ["gaming", "games", "pcgaming", "rpg", "strategy", "playstation", "xbox", "nintendo"],
    "education_career": ["learnprogramming", "cscareer", "datascience", "machinelearning", "education", "college", "jobs", "careerguidance"],
    "other": []
}

def classify_subreddit(name: str, title: str) -> str:
    text = (name + " " + title).lower()
    for cat, keywords in CATEGORIES.items():
        if any(kw in text for kw in keywords):
            return cat
    return "other"

class CommunityDiscoverer:
    def __init__(self, min_subs=10000, max_subs=10000000, include_nsfw=False):
        self.min_subs = min_subs
        self.max_subs = max_subs
        self.include_nsfw = include_nsfw
        self.subreddits_found = {}  # id -> dict

    async def fetch_page(self, client, url, params):
        for _ in range(3):
            try:
                resp = await client.get(url, params=params, timeout=20.0)
                if resp.status_code == 200:
                    return resp.json().get("data", {})
                elif resp.status_code == 429:
                    print(f"Rate limited by Reddit. Waiting 60 seconds...")
                    await asyncio.sleep(60)
                else:
                    print(f"Status {resp.status_code} for {url}")
                    await asyncio.sleep(2)
            except Exception as e:
                print(f"Error fetching {url}: {e}")
                await asyncio.sleep(2)
        return {}

    def process_children(self, children):
        """Extract only qualifying subreddits and only the fields we need.
        This keeps memory low on 8GB machines even with 5000+ subreddits scanned."""
        count_added = 0
        min_sub_seen = float('inf')
        prev_len = len(self.subreddits_found)

        for c in children:
            data = c.get("data", {})
            sid = data.get("name")
            if not sid:
                continue

            # Safely coerce subscribers - Reddit occasionally returns None
            subs = data.get("subscribers") or 0

            if subs < min_sub_seen:
                min_sub_seen = subs

            # Skip anything outside our range immediately to save memory
            if subs < self.min_subs or subs > self.max_subs:
                continue
            if data.get("subreddit_type") != "public":
                continue
            if data.get("over18", False) and not self.include_nsfw:
                continue

            # Store ONLY the fields needed for filter_and_score, not the full blob
            self.subreddits_found[sid] = {
                "display_name":          data.get("display_name", ""),
                "display_name_prefixed": data.get("display_name_prefixed", ""),
                "title":                 data.get("title", ""),
                "public_description":    data.get("public_description", ""),
                "subscribers":           subs,
                "active_user_count":     data.get("active_user_count") or 0,
                "over18":                data.get("over18", False),
                "subreddit_type":        data.get("subreddit_type", "public"),
                "created_utc":           int(data.get("created_utc") or 0),
            }
            count_added += 1

        new_len = len(self.subreddits_found)
        if (new_len // 100) > (prev_len // 100):
            print(f"Progress: {new_len} qualified subreddits found...")

        return count_added, min_sub_seen


    async def run_discovery(self):
        async with httpx.AsyncClient(
            headers=REDDIT_HEADERS,
            timeout=20.0,
            follow_redirects=True,
            http2=False,
            verify=True,
        ) as client:
            await warm_up_session(client)
            
            print("Starting Source 1: Popular")
            cursor = None
            while True:
                data = await self.fetch_page(client, "https://www.reddit.com/subreddits/popular.json", {"limit": 100, "after": cursor})
                children = data.get("children", [])
                if not children: break
                
                _, min_subs = self.process_children(children)
                cursor = data.get("after")
                await asyncio.sleep(random.uniform(2.0, 3.0))
                
                if not cursor: break
                if min_subs < 8000: break
                
            print("Starting Source 2: New")
            cursor = None
            for _ in range(20):
                data = await self.fetch_page(client, "https://www.reddit.com/subreddits/new.json", {"limit": 100, "after": cursor})
                children = data.get("children", [])
                if not children: break
                
                self.process_children(children)
                cursor = data.get("after")
                await asyncio.sleep(random.uniform(2.0, 3.0))
                if not cursor: break
                
            print("Starting Source 3: Search")
            search_terms = ["help", "advice", "discussion", "community", "support", "general", "official", "enthusiasts", "owners", "users", "professionals", "hobbyists", "beginners", "intermediate", "advanced", "market", "industry", "trade", "business"]
            for term in search_terms:
                data = await self.fetch_page(client, "https://www.reddit.com/subreddits/search.json", {"q": term, "limit": 100, "sort": "relevance"})
                children = data.get("children", [])
                self.process_children(children)
                await asyncio.sleep(random.uniform(2.0, 3.0))

    def filter_and_score(self):
        passed = []
        for sid, data in self.subreddits_found.items():
            # Pre-filtering already done in process_children; just score here
            subs = data.get("subscribers", 0)
            active = data.get("active_user_count", 0) or 0
            
            engagement = (active / subs) * 100 if subs > 0 else 0
            
            score = 0
            if 50000 <= subs <= 500000: score += 20
            
            if active >= 1000: score += 30
            elif active >= 500: score += 20
            elif active >= 100: score += 10
            elif active >= 50: score += 5
            
            if engagement >= 1.0: score += 30
            elif engagement >= 0.5: score += 20
            elif engagement >= 0.2: score += 10
            elif engagement >= 0.1: score += 5
            
            activity_score = min(100, score)
            
            name = data.get("display_name", "")
            title = data.get("title", "")
            cat = classify_subreddit(name, title)
            
            passed.append({
                "name": name,
                "display_name": data.get("display_name_prefixed", f"r/{name}"),
                "title": title,
                "description": data.get("public_description", ""),
                "subscribers": subs,
                "active_user_count": active,
                "engagement_ratio": round(engagement, 3),
                "activity_score": activity_score,
                "category": cat,
                "created_utc": int(data.get("created_utc", 0)),
                "last_seen": datetime.now(timezone.utc).isoformat()
            })
            
        passed.sort(key=lambda x: x["activity_score"], reverse=True)
        return passed

async def discover_communities(
    output_path: str = "./outputs/community_database.json",
    min_subscribers: int = 10000,
    max_subscribers: int = 10000000,
    include_nsfw: bool = False,
    test_subreddits: list = None
) -> dict:
    discoverer = CommunityDiscoverer(min_subscribers, max_subscribers, include_nsfw)
    
    if test_subreddits is not None:
        async with httpx.AsyncClient(
            headers=REDDIT_HEADERS,
            timeout=20.0,
            follow_redirects=True,
            http2=False,
            verify=True,
        ) as client:
            await warm_up_session(client)
            for sub in test_subreddits:
                url = f"https://www.reddit.com/r/{sub}/about.json"
                data = await discoverer.fetch_page(client, url, {})
                if not data:
                    print(f"⚠  r/{sub}: skipped (blocked)")
                    continue
                discoverer.subreddits_found[sub] = data
                await asyncio.sleep(random.uniform(2.0, 3.0))
                    
        total_discovered = len(discoverer.subreddits_found)
        
        # Bypass subscriber filter limits for test subreddits (real data only)
        communities = []
        for sid, data in discoverer.subreddits_found.items():
            subs = data.get("subscribers", 0)
            active = data.get("active_user_count", 0) or 0
            name = data.get("display_name", "")
            title = data.get("title", "")
            cat = classify_subreddit(name, title)
            engagement = (active / subs * 100) if subs else 0
            
            communities.append({
                "name": name,
                "display_name": data.get("display_name_prefixed", f"r/{name}"),
                "title": title,
                "description": data.get("public_description", ""),
                "subscribers": subs,
                "active_user_count": active,
                "engagement_ratio": round(engagement, 3),
                "activity_score": min(100, int(engagement * 10 + (30 if active >= 500 else 10))),
                "category": cat,
                "created_utc": int(data.get("created_utc", 0)),
                "last_seen": datetime.now(timezone.utc).isoformat()
            })
            
        for c in communities:
            print(f"✅ r/{c['name']}: {c['subscribers']:,} members, {c['active_user_count']:,} active, score: {c['activity_score']}/100")
    else:
        await discoverer.run_discovery()
        total_discovered = len(discoverer.subreddits_found)
        communities = discoverer.filter_and_score()
    
    out_dir = os.path.dirname(output_path)
    if out_dir: os.makedirs(out_dir, exist_ok=True)
    
    # Load existing database to append/merge instead of overwrite
    existing_communities = []
    if os.path.exists(output_path):
        try:
            with open(output_path, "r", encoding="utf-8") as f:
                existing_data = json.load(f)
                existing_communities = existing_data.get("communities", [])
        except Exception:
            pass

    # Merge logic: use a dict keyed by 'name' to avoid duplicates
    # New data overwrites old data for the same subreddit
    merged_map = {c["name"]: c for c in existing_communities}
    new_added = 0
    updated = 0
    new_subscribers = 0
    
    for c in communities:
        if c["name"] not in merged_map:
            new_added += 1
            new_subscribers += c.get("subscribers", 0)
        else:
            updated += 1
        merged_map[c["name"]] = c
        
    merged_communities = list(merged_map.values())
    
    # Sort merged list by activity score descending
    merged_communities.sort(key=lambda x: x.get("activity_score", 0), reverse=True)
    
    result = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "total_discovered_this_run": total_discovered,
        "total_after_filters": len(merged_communities),
        "communities": merged_communities
    }
    
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
        
    return {
        "total_discovered": total_discovered,
        "total_after_filters": len(merged_communities),
        "new_communities_added": new_added,
        "existing_communities_updated": updated,
        "new_subscribers_added": new_subscribers,
        "output_path": output_path
    }

async def load_community_database(
    db_path: str = "./outputs/community_database.json"
) -> list[dict]:
    if not os.path.exists(db_path): return []
    try:
        with open(db_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("communities", [])
    except:
        return []

async def get_top_communities(
    db_path: str,
    category: str = None,
    min_activity_score: int = 50,
    limit: int = 200
) -> list[dict]:
    comms = await load_community_database(db_path)
    filtered = []
    for c in comms:
        if c.get("activity_score", 0) < min_activity_score: continue
        if category and c.get("category") != category: continue
        filtered.append(c)
    return filtered[:limit]

if __name__ == "__main__":
    asyncio.run(discover_communities())
