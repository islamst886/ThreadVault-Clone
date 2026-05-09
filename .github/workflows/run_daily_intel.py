import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "backend"))
init = Path(__file__).parent.parent.parent / "backend" / "__init__.py"
if not init.exists():
    init.touch()

from daily_intelligence import run_daily_intelligence  # type: ignore

SEP = "=" * 55

async def main():
    lookback    = int(os.environ.get("LOOKBACK_HOURS", "24"))
    threshold   = float(os.environ.get("VIRAL_THRESHOLD", "3"))
    test_mode   = os.environ.get("TEST_MODE", "false").lower() == "true"
    test_with_ai = os.environ.get("TEST_WITH_AI", "false").lower() == "true"
    db_path     = os.environ.get("DB_PATH")
    tracking_path = os.environ.get("TRACKING_PATH")
    tier_name   = os.environ.get("TIER_NAME", "")

    print(SEP)
    if tier_name:
        print(f"TIER      : {tier_name}")
    if test_mode:
        print("MODE      : TEST  (3 subreddits, 96-hour window)")
        print(f"AI        : {'ENABLED' if test_with_ai else 'DISABLED'}")
    else:
        print("MODE      : PRODUCTION")
        print(f"Lookback  : {lookback} hours")
        print(f"Threshold : {threshold}x baseline")
    print(SEP)

    if test_mode:
        report_path = await run_daily_intelligence(
            watchlist_path=None,
            output_dir="./outputs",
            lookback_hours=96,
            viral_threshold=2.0,
            test_mode=True,
            test_subreddits=["personaltraining", "entrepreneur", "SaaS"],
            skip_ai=not test_with_ai,
            db_path=db_path,
            tracking_path=tracking_path,
            tier_name=tier_name
        )
    else:
        report_path = await run_daily_intelligence(
            watchlist_path=".github/workflows/watchlist.yml",
            output_dir="./outputs",
            lookback_hours=lookback,
            viral_threshold=threshold,
            db_path=db_path,
            tracking_path=tracking_path,
            tier_name=tier_name
        )

    size_kb = round(os.path.getsize(report_path) / 1024)
    print(SEP)
    print(f"✅ Report generated: {report_path} ({size_kb} KB)")
    print(SEP)

asyncio.run(main())
