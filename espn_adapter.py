# espn_adapter.py
#
# Adapters to pull live data from ESPN and map it into LineupIQ models,
# attaching FantasyPros weekly projections.

from typing import List, Any, Optional

from espn_api.football import League  # type: ignore[import]

from config import (  # type: ignore[import]
    ESPN_LEAGUE_ID,
    ESPN_YEAR,
    ESPN_S2,
    ESPN_SWID,
    TEAMS,
    DEFAULT_TEAM_KEY,
    CURRENT_WEEK,
)
from models import Player, FreeAgent  # type: ignore[import]
from projections_fantasypros import (  # type: ignore[import]
    get_fp_projection_for_espn_player,
)


# ---------- tiny helper so dicts & objects both work ----------

def _cfg_get(cfg: Optional[Any], key: str, default=None):
    """
    Read a config field whether cfg is a dict or a simple object.
    """
    if cfg is None:
        return default
    if isinstance(cfg, dict):
        return cfg.get(key, default)
    return getattr(cfg, key, default)


# ---------- league helper ----------

def _get_league(team_cfg: Optional[Any] = None) -> League:
    """
    Build an ESPN League for a particular team config.
    If team_cfg is None, fall back to the single-team constants.
    """
    league_id = _cfg_get(team_cfg, "league_id", ESPN_LEAGUE_ID)
    year = _cfg_get(team_cfg, "season_year", ESPN_YEAR)
    espn_s2 = _cfg_get(team_cfg, "espn_s2", ESPN_S2)
    swid = _cfg_get(team_cfg, "espn_swid", ESPN_SWID)

    return League(
        league_id=league_id,
        year=year,
        espn_s2=espn_s2,
        swid=swid,
    )


def _find_my_team(league: League, team_cfg: Optional[Any]) -> Any:
    """
    Find the user's team in the league using team_name_keyword.
    """
    name_keyword = _cfg_get(team_cfg, "team_name_keyword")
    if not name_keyword:
        # fallback to default team keyword (single-team mode)
        from config import TEAM_NAME_KEYWORD  # type: ignore[import]

        name_keyword = TEAM_NAME_KEYWORD

    keyword_lower = name_keyword.lower()

    for t in league.teams:
        if keyword_lower in t.team_name.lower():
            return t

    raise RuntimeError(
        f"Could not find team whose name contains '{name_keyword}' in this league."
    )


# ---------- roster + FA fetchers ----------

def fetch_roster_from_espn(team_cfg: Optional[Any] = None) -> List[Player]:
    """
    Return your current ESPN roster as LineupIQ Player objects,
    with projections coming from FantasyPros.
    """
    league = _get_league(team_cfg)
    my_team = _find_my_team(league, team_cfg)

    roster: List[Player] = []

    for p in my_team.roster:
        # Detect bye weeks. In espn_api the schedule is usually a dict keyed
        # by scoring period / week number, so a missing entry for the current
        # week strongly indicates a bye (as with 49ers D/ST above). As a
        # fallback, we also inspect simple opponent-style fields for "BYE".
        sched = getattr(p, "schedule", None)
        is_bye_week = False

        if isinstance(sched, dict):
            # For roster players, schedule will usually contain entries for
            # most weeks; for BYE weeks the current week key is simply
            # missing. For free agents, espn_api often exposes an empty dict
            # for BYE weeks. In both cases "no CURRENT_WEEK key" means BYE.
            if CURRENT_WEEK not in sched and str(CURRENT_WEEK) not in sched:
                is_bye_week = True
        else:
            opp_raw = getattr(p, "opponent", None) or getattr(p, "opp", None)
            opp_str = str(opp_raw or "").upper()
            if "BYE" in opp_str:
                is_bye_week = True

        # Weekly projection from FantasyPros. If the player is on bye, skip
        # the lookup and use 0.0.
        if is_bye_week:
            proj = 0.0
        else:
            proj = get_fp_projection_for_espn_player(p.name, p.position, team_cfg)

        # Raw ESPN status info
        raw_status = getattr(p, "injuryStatus", None) or getattr(p, "status", None)
        slot = (
            getattr(p, "slot_position", None)
            or getattr(p, "slotPosition", None)
            or getattr(p, "lineupSlot", None)
        )

        # Normalise slot to a string for robust comparisons. This code is what
        # you see in the ESPN UI, e.g. "RB", "WR", "RB/WR/TE", "FLEX", "BE".
        # We also stash it on the Player objects so downstream logic (like
        # bench→start swap suggestions) can respect which slots are FLEX and
        # which are strict position slots.
        slot_str = str(slot).upper() if slot is not None else ""

        # Final status:
        #  - BYE if opponent/schedule indicates a bye week
        #  - IR if slot says IR
        #  - else ESPN injuryStatus/status
        #  - else ACTIVE
        if is_bye_week:
            status = "BYE"
        elif slot_str == "IR":
            status = "IR"
        else:
            status = raw_status if raw_status is not None else "ACTIVE"

        # Starter flag: not bench / IR / reserve
        # espn_api can use "BE", "BN", numeric bench codes, or "Bench"
        bench_like_slots = {"BE", "BN", "IR", "RES", "BENCH"}
        is_starter = slot_str not in bench_like_slots

        # Normalize DST
        pos = p.position
        if pos in ("D/ST", "DST"):
            pos = "DST"

        player = Player(
            name=p.name,
            position=pos,
            projection=proj or 0.0,
            status=status,
            is_starter=is_starter,
        )

        # Attach the raw ESPN lineup slot code so the optimizer can reason
        # about which starters are actually occupying FLEX ("RB/WR/TE") versus
        # locked position slots like "RB" or "WR".
        setattr(player, "espn_slot", slot_str)

        roster.append(player)

    return roster


def fetch_free_agents_from_espn(
    team_cfg: Optional[Any] = None, max_players: int = 50
) -> List[FreeAgent]:
    """
    Return a list of free agents with projections.
    max_players limits how many we pull for performance.
    """
    league = _get_league(team_cfg)

    fa_players = league.free_agents(size=max_players)

    free_agents: List[FreeAgent] = []

    for p in fa_players:
        # For free agents, espn_api does not reliably expose schedule / bye-week
        # information (often schedule is just an empty dict and status is None),
        # so we *do not* try to infer BYE here. We instead rely solely on
        # FantasyPros projections and any explicit ESPN status flags.
        raw_status = getattr(p, "injuryStatus", None) or getattr(p, "status", None)
        status = raw_status if raw_status is not None else "ACTIVE"

        proj = get_fp_projection_for_espn_player(p.name, p.position, team_cfg)

        # Availability: treat WA/WAIVER as "cannot add now"
        availability = (getattr(p, "status", "") or "").upper()
        can_add_now = not ("WA" in availability or "WAIVER" in availability)

        pos = p.position
        if pos in ("D/ST", "DST"):
            pos = "DST"

        free_agents.append(
            FreeAgent(
                name=p.name,
                position=pos,
                projection=proj or 0.0,
                can_add_now=can_add_now,
                status=status,
            )
        )

    return free_agents


# Convenience for single-team CLI call, still works:
def fetch_default_roster() -> List[Player]:
    return fetch_roster_from_espn(TEAMS.get(DEFAULT_TEAM_KEY))


def fetch_default_free_agents(max_players: int = 50) -> List[FreeAgent]:
    return fetch_free_agents_from_espn(TEAMS.get(DEFAULT_TEAM_KEY), max_players=max_players)
