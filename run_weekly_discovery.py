import asyncio
import sys
import os
import json
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "backend"))
from community_discovery import discover_communities  # type: ignore

try:
    from split_community_databases import split_database
except ImportError:
    split_database = None

async def main():
    test_mode = "--test" in sys.argv
    
    os.makedirs("data", exist_ok=True)

    if test_mode:
        # ⚠️  NEVER write to the real database in test mode
        output_json = "data/community_database_TEST.json"
        output_txt  = "data/community_database_TEST_summary.txt"
        print("=" * 60)
        print("TEST MODE — only 10 subreddits")
        print("Output: data/community_database_TEST.json")
        print("The real community_database.json is NOT touched.")
        print("=" * 60)
        test_subreddits = [
            "fitness", "entrepreneur", "SaaS", "webdev",
            "personaltraining", "programming", "marketing",
            "investing", "cooking", "photography"
        ]
    else:
        output_json = "data/community_database.json"
        output_txt  = "data/community_database_summary.txt"
        test_subreddits = None

    print("Starting Community Discovery...")
    result = await discover_communities(
        output_path=output_json,
        test_subreddits=test_subreddits
    )

    # Generate human readable summary
    if os.path.exists(output_json):
        with open(output_json, "r", encoding="utf-8") as f:
            db = json.load(f)

        communities = db.get("communities", [])
        communities.sort(key=lambda x: x.get("activity_score", 0), reverse=True)
        top_50 = communities[:50]

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        with open(output_txt, "w", encoding="utf-8") as f:
            f.write("ThreadVault Community Database\n")
            f.write(f"Generated: {now_str}\n")
            f.write(f"Total communities: {len(communities):,}\n")
            f.write("─────────────────────────────────────────────\n")
            f.write(f"{'Rank':<5} {'Subreddit':<22} {'Members':<10} {'Active':<7} {'Score':<6} {'Category'}\n")
            f.write("─────────────────────────────────────────────\n")

            for i, c in enumerate(top_50, 1):
                f.write(
                    f"{i:<5} "
                    f"r/{c['name']:<20} "
                    f"{c['subscribers']:<10,} "
                    f"{c['active_user_count']:<7,} "
                    f"{c['activity_score']:<6} "
                    f"{c['category']}\n"
                )

    total = result.get('total_after_filters', 0)
    print("=" * 60)

    if test_mode:
        print("TEST RUN COMPLETE")
        print("=" * 60)
        print(f"  Communities fetched: {total}")
        print(f"  Test output saved to: {output_json}")
        print()
        print("✅ Your real community_database.json was NOT modified.")
        print("   To inspect the test results, open:")
        print(f"   data/community_database_TEST.json")
        print()
        print("When you are ready for a real run, use:")
        print("   .\\venv\\Scripts\\python run_weekly_discovery.py")
    else:
        print("DISCOVERY COMPLETE")
        print("=" * 60)
        print(f"  Total communities in database: {total}")
        new_added = result.get('new_communities_added', 0)
        updated = result.get('existing_communities_updated', 0)
        new_subs = result.get('new_subscribers_added', 0)
        
        print(f"  New communities appended   : +{new_added}")
        print(f"  Existing communities updated : {updated}")
        print(f"  New total subscribers added: +{new_subs:,}")
        print(f"  Saved to: {output_json}")
        print()
        print("Next steps — commit this to GitHub:")
        print()
        print(f"  git add {output_json} {output_txt} data/db_tier*.json")
        print('  git commit -m "Weekly community database update"')
        print("  git push")
        print()
        print("The 5 daily tier pipelines on GitHub Actions will")
        print("automatically use these updated databases.")
        
        if split_database:
            print("\nSplitting database into tiers...")
            split_database()

    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(main())
