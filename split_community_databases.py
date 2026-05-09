import json
import os
from datetime import datetime, timezone

def split_database():
    source_db = "./data/community_database.json"
    if not os.path.exists(source_db):
        print(f"❌ Could not find {source_db}")
        return

    with open(source_db, "r", encoding="utf-8") as f:
        data = json.load(f)

    communities = data.get("communities", [])
    
    tier1 = [] # 10k - 100k
    tier2 = [] # 100k+ - 200k
    tier3 = [] # 200k+ - 500k
    tier4 = [] # 500k+ - 700k
    tier5 = [] # 700k+ - 1M+

    for c in communities:
        subs = c.get("subscribers", 0)
        if 10000 <= subs <= 100000:
            tier1.append(c)
        elif 100000 < subs <= 200000:
            tier2.append(c)
        elif 200000 < subs <= 500000:
            tier3.append(c)
        elif 500000 < subs <= 700000:
            tier4.append(c)
        elif subs > 700000:
            tier5.append(c)

    tiers = [
        ("tier1_10k_100k", tier1),
        ("tier2_100k_200k", tier2),
        ("tier3_200k_500k", tier3),
        ("tier4_500k_700k", tier4),
        ("tier5_700k_1m", tier5)
    ]

    print(f"Total communities in main DB: {len(communities)}")
    print("-" * 40)

    now_str = datetime.now(timezone.utc).isoformat()

    for tier_name, tier_list in tiers:
        filename = f"./data/db_{tier_name}.json"
        
        # Sort by activity score descending, just like the main db
        tier_list.sort(key=lambda x: x.get("activity_score", 0), reverse=True)
        
        output_data = {
            "last_updated": now_str,
            "total_discovered": len(tier_list),
            "total_after_filters": len(tier_list),
            "communities": tier_list
        }
        
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(output_data, f, indent=2)
            
        print(f"Saved {filename} ({len(tier_list)} communities)")

if __name__ == "__main__":
    split_database()
