from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

import pandas as pd # type: ignore[import]

from config import CURRENT_WEEK, SCORING, DATA_ROOT, SEASON_YEAR  # type: ignore[import]
from fetch_fp_projections import main as fetch_fp_main  # type: ignore[import]

def week_folder(week: Optional[int] = None,
                scoring: Optional[str] = None) -> str:
    """
    Return the *folder name* where FantasyPros .xls files live for
    a given week / season / scoring, e.g.:

        "fp_week13_2025_half"
    """
    if week is None:
        week = CURRENT_WEEK
    if scoring is None:
        scoring = SCORING

    return f"fp_week{week}_{SEASON_YEAR}_{scoring}"


# In-memory cache: { folder_path_str : { position: { norm_name: fpts } } }
_FP_CACHE: Dict[str, Dict[str, Dict[str, float]]] = {}


def _norm_name(raw: str) -> str:
    """
    Normalise a FantasyPros / ESPN 'Player' cell into a comparable bare name.

    Examples:
      "Jordan Love GB"        -> "jordan love"
      "Jordan Love (GB)"      -> "jordan love"
      "Kenneth Walker III"    -> "kenneth walker"
      "Kenneth Walker III SEA"-> "kenneth walker"

    We:
      - drop anything in parentheses
      - drop a trailing TEAM code (two/three caps like GB, SEA)
      - drop common generational / suffix parts at the end (Jr, Sr, II, III, IV)
    so ESPN and FantasyPros variants line up.
    """
    if not isinstance(raw, str):
        return ""

    # Drop anything in parentheses, e.g. "Jordan Love (GB)"
    if "(" in raw:
        raw = raw.split("(", 1)[0]

    parts = raw.split()

    # If there is a trailing TEAM code (two or three caps) drop it.
    if parts and parts[-1].isupper() and 2 <= len(parts[-1]) <= 3:
        parts = parts[:-1]

    # Drop common generational / name suffixes at the very end.
    SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v", "jr.", "sr."}
    while parts and parts[-1].rstrip(".").lower() in SUFFIXES:
        parts = parts[:-1]

    return " ".join(parts).strip().lower()


def _load_position_file(path: Path, pos: str) -> Dict[str, float]:
    """
    Read one FantasyPros .xls file and return {norm_name: fpts}.

    We handle both simple columns ('Player', 'FPTS') and MultiIndex columns
    like ('Unnamed: 0_level_0', 'Player').
    """
    if not path.exists():
        print(f"[FantasyPros] WARNING: file not found for {pos}: {path}")
        return {}

    # These .xls downloads are actually HTML tables; read_html copes better.
    tables = pd.read_html(path)
    if not tables:
        print(f"[FantasyPros] WARNING: no tables found in {path}")
        return {}

    df = tables[0]

    # Find a column that looks like "Player"
    player_col = None
    for col in df.columns:
        if isinstance(col, tuple):
            if str(col[-1]).strip().lower() == "player":
                player_col = col
                break
        else:
            if str(col).strip().lower() == "player":
                player_col = col
                break

    # Find a column that looks like "FPTS"
    fpts_col = None
    for col in df.columns:
        if isinstance(col, tuple):
            if str(col[-1]).strip().upper() == "FPTS":
                fpts_col = col
                break
        else:
            if str(col).strip().upper() == "FPTS":
                fpts_col = col
                break

    if player_col is None or fpts_col is None:
        print(f"[FantasyPros] WARNING: could not find Player/FPTS columns in {path}")
        return {}

    print(
        f"[FantasyPros] {pos}: loaded {len(df)} rows from {path.name} "
        f"(player_col={player_col}, fpts_col={fpts_col})"
    )

    out: Dict[str, float] = {}
    for _, row in df.iterrows():
        name_raw = row[player_col]
        try:
            fpts_raw = float(row[fpts_col])
        except Exception:
            continue

        name_norm = _norm_name(name_raw)
        if not name_norm:
            continue

        # Keep the max projection if duplicates exist.
        prev = out.get(name_norm)
        if prev is None or fpts_raw > prev:
            out[name_norm] = fpts_raw

    return out


def _ensure_loaded(scoring: Optional[str] = None) -> Dict[str, Dict[str, float]]:
    """
    Ensure projections are loaded for the current week / scoring.

    Returns: { position: {norm_name: fpts} }
    """
    if scoring is None:
        scoring = SCORING

    folder_name = week_folder(CURRENT_WEEK, scoring)
    folder_path = Path(DATA_ROOT) / folder_name
    key = str(folder_path.resolve())

    if key in _FP_CACHE:
        return _FP_CACHE[key]

    # If the expected week/scoring folder is empty, automatically download
    # the latest FantasyPros projections for this week.
    if not folder_path.exists() or not any(folder_path.iterdir()):
        print(
            f"[FantasyPros] No projection files found for {folder_name}; "
            f"invoking fetch_fp_projections.main() to download them..."
        )
        try:
            fetch_fp_main()
        except Exception as exc:  # pragma: no cover - defensive
            print(f"[FantasyPros] ERROR downloading projections: {exc}")

    print(f"[FantasyPros] Using folder layout: {folder_path}")
    folder_path.mkdir(parents=True, exist_ok=True)

    files = {
        "QB": "qb.xls",
        "RB": "rb.xls",
        "WR": "wr.xls",
        "TE": "te.xls",
        "K": "k.xls",
        "DST": "dst.xls",
    }

    pos_maps: Dict[str, Dict[str, float]] = {}
    total_rows = 0

    for pos, filename in files.items():
        path = folder_path / filename
        pos_map = _load_position_file(path, pos)
        pos_maps[pos] = pos_map
        total_rows += len(pos_map)

    _FP_CACHE[key] = pos_maps
    print(
        f"[FantasyPros] Loaded {total_rows} projection entries "
        f"for week {CURRENT_WEEK} (scoring={scoring})."
    )

    return pos_maps


def get_fp_projection_for_espn_player(
    espn_player_name: str,
    position: str,
    team_cfg: Optional[dict] = None,
) -> Optional[float]:
    """
    Lookup a projection for an ESPN player using FantasyPros tables.

    espn_player_name: name from ESPN API (e.g. 'Jordan Love')
    position: 'QB', 'RB', 'WR', 'TE', 'K', 'DST'
    team_cfg: a team dict from config.TEAMS; we currently only care about
              the 'scoring' field if present.
    """
    scoring = None
    if isinstance(team_cfg, dict):
        scoring = team_cfg.get("scoring")

    pos_maps = _ensure_loaded(scoring=scoring)

    # Some positions in ESPN are like 'D/ST'; normalise to 'DST'.
    if position in ("D/ST", "DST", "DEF"):
        position = "DST"

    # Normalise the name in the same way we normalised FP names.
    norm = _norm_name(espn_player_name)

    pos_map = pos_maps.get(position.upper())
    if not pos_map:
        return None

    # First try the straightforward lookup.
    value = pos_map.get(norm)
    if value is not None:
        return value

    # DST naming between ESPN and FantasyPros can be quite different
    # (e.g. "Seahawks D/ST" vs "Seattle Seahawks"). As a fallback, for DST
    # we do a fuzzy-ish match that strips non-letters and generic defense
    # suffixes and then looks for overlapping team names.
    if position.upper() == "DST":
        def _squash_team(s: str) -> str:
            only_letters = "".join(ch.lower() for ch in str(s) if ch.isalpha())
            for token in ("dst", "defense", "def"):
                only_letters = only_letters.replace(token, "")
            return only_letters

        target = _squash_team(espn_player_name)
        if not target:
            return None

        for key, fpts in pos_map.items():
            k = _squash_team(key)
            if not k:
                continue
            if k in target or target in k:
                return fpts

    return None
