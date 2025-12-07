# fetch_fp_projections.py
#
# Automatically download FantasyPros weekly projections
# and store them in a week-organized folder under /data.

import requests  # type: ignore[import]
import os
from datetime import datetime

# ================================
# CONFIG
# ================================
from config import SEASON_YEAR, SCORING, CURRENT_WEEK, DATA_ROOT  # type: ignore[import]

POSITIONS = ["qb", "rb", "wr", "te", "k", "dst"]

BASE_URL = "https://www.fantasypros.com/nfl/projections/{pos}.php"

USER_AGENT_HEADER = {
    "User-Agent": "Mozilla/5.0"
}

# ================================
# UTILITIES
# ================================

def detect_current_nfl_week() -> int:
    """
    Rough NFL week estimation based on season start heuristics.
    You can override manually if needed.
    """
    season_start = datetime(SEASON_YEAR, 9, 5)   # approx NFL kickoff window
    today = datetime.now()

    delta_days = (today - season_start).days
    week = max(1, min(18, (delta_days // 7) + 1))

    return week


def ensure_folder(path: str):
    os.makedirs(path, exist_ok=True)


def download_projection_file(pos: str, outfile: str):
    url = BASE_URL.format(pos=pos)
    params = {"scoring": SCORING}

    print(f"Downloading {pos.upper()} projections...")

    resp = requests.get(
        url,
        headers=USER_AGENT_HEADER,
        params=params,
        timeout=15,
    )

    resp.raise_for_status()

    with open(outfile, "wb") as f:
        f.write(resp.content)

    print(f"Saved -> {outfile}")


# ================================
# MAIN RUNNER
# ================================

def main():

    # Use the same notion of "current week" as the rest of the app so that
    # downloads line up with what projections_fantasypros expects.
    week = CURRENT_WEEK

    folder_name = f"fp_week{week}_{SEASON_YEAR}_{SCORING}"
    target_dir = os.path.join(DATA_ROOT, folder_name)

    ensure_folder(target_dir)

    print("\n==========================================")
    print(f"FantasyPros Projections Downloader")
    print(f"Season: {SEASON_YEAR}")
    print(f"Week:   {week}")
    print(f"Scoring:{SCORING}")
    print(f"Output: {target_dir}")
    print("==========================================\n")

    for pos in POSITIONS:
        filename = f"{pos}.xls"
        save_path = os.path.join(target_dir, filename)

        try:
            download_projection_file(pos, save_path)
        except Exception as e:
            print(f"❌ Failed downloading {pos.upper()}: {e}")

    print("\n✅ FantasyPros downloads complete.")


if __name__ == "__main__":
    main()
