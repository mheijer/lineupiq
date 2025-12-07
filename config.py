# config.py
from pathlib import Path
from typing import Dict, Any
import os

# ====== FantasyPros / season config ======
SEASON_YEAR: int = 2025
SCORING: str = "half"          # "half", "ppr", etc.
CURRENT_WEEK: int = 14

DATA_ROOT = Path("data")


# How apply_actions_for_team should behave. Keep "dry_run" until you're ready
# to actually talk to ESPN. Later you can change this to "http" or "browser".
ACTION_MODE: str = "http"

# ====== ESPN single-team defaults (for backwards compatibility) ======
ESPN_LEAGUE_ID: int = 43572092          # <- your real league id
ESPN_YEAR: int = SEASON_YEAR

# For safer future hosting, we no longer hard-code real cookies here. In any
# non-local environment you should provide these via environment variables,
# or better yet rely on per-user /auth/espn_link instead of the global values.
ESPN_S2: str = os.environ.get("ESPN_S2", "")
ESPN_SWID: str = os.environ.get("ESPN_SWID", "")
TEAM_NAME_KEYWORD: str = "Can't Teach Matchups"


# ====== Multi-team config ======
TEAMS: Dict[str, Dict[str, Any]] = {
    "cant_teach_matchups": {
        "platform": "espn",
        "league_id": ESPN_LEAGUE_ID,
        "season_year": ESPN_YEAR,
        "team_name_keyword": TEAM_NAME_KEYWORD,
        "espn_s2": ESPN_S2,
        "espn_swid": ESPN_SWID,
        "scoring": SCORING,
    },
    "b00bs_b00bs_b00bs": {
        "platform": "espn",
        "league_id": 973091,
        "season_year": ESPN_YEAR,
        "team_name_keyword": "B00bs B00bs B00bs",
        "espn_s2": ESPN_S2,
        "espn_swid": ESPN_SWID,
        "scoring": SCORING,
    },
    "stinky_steinerts": {
        "platform": "espn",
        "league_id": 232132462,
        "season_year": ESPN_YEAR,
        "team_name_keyword": "Stinky Steinerts",
        "espn_s2": ESPN_S2,
        "espn_swid": ESPN_SWID,
        "scoring": SCORING,
    },
    "huge_b1tches": {
        "platform": "espn",
        "league_id": 482478780,
        "season_year": ESPN_YEAR,
        "team_name_keyword": "Huge B1tches",
        "espn_s2": ESPN_S2,
        "espn_swid": ESPN_SWID,
        "scoring": SCORING,
    },

}

DEFAULT_TEAM_KEY: str = "cant_teach_matchups"
