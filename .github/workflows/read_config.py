"""
read_config.py
--------------
Helper script for the GitHub Actions workflow (scheduled runs only).

Reads .github/workflows/scheduled_config.yml and writes each config
value to $GITHUB_ENV so subsequent workflow steps can use them as
environment variables.

Called by the workflow as:
    python .github/workflows/read_config.py
"""

import os
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml is not installed. Run: pip install pyyaml")
    sys.exit(1)

CONFIG_PATH = Path(__file__).parent / "scheduled_config.yml"

if not CONFIG_PATH.exists():
    print(f"ERROR: Config file not found: {CONFIG_PATH}")
    print("Create .github/workflows/scheduled_config.yml with a 'config:' block.")
    sys.exit(1)

with CONFIG_PATH.open(encoding="utf-8") as fh:
    raw = yaml.safe_load(fh)

if not raw or "config" not in raw:
    print("ERROR: scheduled_config.yml must have a top-level 'config:' key.")
    sys.exit(1)

config: dict = raw["config"]

REQUIRED = ("subreddits", "post_limit", "years_back", "comment_sort", "comment_limit")
for key in REQUIRED:
    if key not in config:
        print(f"ERROR: Missing key '{key}' in scheduled_config.yml")
        sys.exit(1)

github_env = os.environ.get("GITHUB_ENV")
if not github_env:
    # Local testing — just print what would be exported
    print("GITHUB_ENV not set (local run). Would export:")
    for key in REQUIRED:
        print(f"  {key.upper()}={config[key]}")
    sys.exit(0)

with open(github_env, "a", encoding="utf-8") as env_file:
    env_file.write(f"SUBREDDITS={config['subreddits']}\n")
    env_file.write(f"POST_LIMIT={config['post_limit']}\n")
    env_file.write(f"YEARS_BACK={config['years_back']}\n")
    env_file.write(f"COMMENT_SORT={config['comment_sort']}\n")
    env_file.write(f"COMMENT_LIMIT={config['comment_limit']}\n")

print("Config loaded from scheduled_config.yml")
for key in REQUIRED:
    print(f"  {key}: {config[key]}")
