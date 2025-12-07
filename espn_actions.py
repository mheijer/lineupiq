"""
espn_actions.py
----------------

Abstraction layer for *writing* changes back to ESPN.

Right now this module defaults to a DRY-RUN strategy that returns what
would be done without actually calling ESPN. You can gradually enable
real writes by implementing the HTTP or browser-based paths below.

The rest of the app (FastAPI + UI) should not need to change; they only call
`apply_actions_for_team` with a team config + a list of action dicts.
"""

from typing import Dict, Any, List, Optional
from datetime import datetime, timezone

import requests  # type: ignore[import]

from espn_adapter import _get_league, _find_my_team  # type: ignore[import]
from config import CURRENT_WEEK  # type: ignore[import]


# Static mapping from ESPN lineup slot *codes* (what you see on the Player
# objects, like "RB", "WR", "BE") to the numeric slot ids used by the
# lm-api-writes transactions endpoint. These ids are stable across leagues.
SLOT_CODE_TO_ID: Dict[str, int] = {
    "QB": 0,
    "RB": 2,
    "WR": 4,
    "TE": 6,
    "FLEX": 23,       # RB/WR/TE
    "RB/WR": 23,
    "RB/WR/TE": 23,
    "DST": 16,
    "D/ST": 16,
    "K": 17,
    "BE": 20,         # Bench
    "IR": 21,
    "INJURY_RESERVE": 21,
    "OP": 24,         # Offensive player slot (Superflex / OP)
}


def _is_player_locked(p: Any) -> bool:
    """
    Best-effort check to see if ESPN considers a player locked for lineup
    changes (game started or otherwise not editable).

    We deliberately fail *open* (return False) if we can't determine lock
    status, so we never block legal moves; this is an extra guardrail on top
    of ESPN's own validation.
    """
    # Some espn_api versions expose an explicit flag.
    locked_flag = getattr(p, "lineupLocked", None)
    if isinstance(locked_flag, bool):
        return locked_flag

    # Fallback: inspect the schedule dict for a start datetime this week.
    sched = getattr(p, "schedule", None)
    if isinstance(sched, dict):
        entry = sched.get(CURRENT_WEEK) or sched.get(str(CURRENT_WEEK))
        if isinstance(entry, dict):
            dt = entry.get("date") or entry.get("startDate")
            if isinstance(dt, datetime):
                # Treat kickoff time as the lock point.
                if dt.tzinfo is None:
                    now = datetime.now(tz=timezone.utc)
                    lock_time = dt.replace(tzinfo=timezone.utc)
                else:
                    now = datetime.now(tz=dt.tzinfo)
                    lock_time = dt
                return now >= lock_time

    return False


def _http_session_for_team(team_cfg: Dict[str, Any]) -> requests.Session:
    """
    Build a requests.Session with ESPN auth cookies populated from team_cfg.

    This uses the same espn_s2 / swid values you already store in config.TEAMS.
    """
    s = requests.Session()
    espn_s2 = team_cfg.get("espn_s2")
    swid = team_cfg.get("espn_swid")
    if espn_s2:
        s.cookies.set("espn_s2", espn_s2, domain=".espn.com")
    if swid:
        s.cookies.set("SWID", swid, domain=".espn.com")
    return s


def _apply_bench_to_start_http(
    session: requests.Session,
    team_cfg: Dict[str, Any],
    action: Dict[str, Any],
) -> Dict[str, Any]:
    """
    HTTP implementation stub for a bench_to_start action.

    This mirrors the "ROSTER" transaction shape you captured in notes.txt.
    It is written so you can safely iterate:
      - If anything is missing (ids/slots), it returns a clear error and
        does NOT attempt a write.
      - Once you're comfortable, you can trust a successful response as the
        equivalent of clicking the move buttons in the UI.
    """

    add_name = action.get("add_name")
    drop_name = action.get("drop_name")
    if not add_name or not drop_name:
        return {
            "id": action.get("id"),
            "type": action.get("type"),
            "mode": "http",
            "success": False,
            "message": "Missing add_name/drop_name on action; cannot build swap.",
        }

    try:
        league = _get_league(team_cfg)
        my_team = _find_my_team(league, team_cfg)
        roster = list(my_team.roster)
    except Exception as exc:  # pragma: no cover - defensive
        return {
            "id": action.get("id"),
            "type": action.get("type"),
            "mode": "http",
            "success": False,
            "message": f"Failed to load league/team from espn_adapter: {exc}",
        }

    # Find the two players on your ESPN roster by name.
    def _find_player(name: str):
        exact = [p for p in roster if getattr(p, "name", "") == name]
        if exact:
            return exact[0]
        lower = name.lower()
        ci = [p for p in roster if getattr(p, "name", "").lower() == lower]
        return ci[0] if ci else None

    bench_p = _find_player(add_name)
    starter_p = _find_player(drop_name)

    if bench_p is None or starter_p is None:
        return {
            "id": action.get("id"),
            "type": action.get("type"),
            "mode": "http",
            "success": False,
            "message": (
                f"Could not resolve players on ESPN roster: "
                f"add_name={add_name!r}, drop_name={drop_name!r}."
            ),
        }

    # Game-start guardrail: never attempt to move players that ESPN already
    # considers locked for this scoring period.
    if _is_player_locked(bench_p) or _is_player_locked(starter_p):
        locked_names: List[str] = []
        if _is_player_locked(bench_p):
            locked_names.append(getattr(bench_p, "name", "bench player"))
        if _is_player_locked(starter_p):
            locked_names.append(getattr(starter_p, "name", "starter player"))
        who = ", ".join(locked_names) if locked_names else "one or more players"
        return {
            "id": action.get("id"),
            "type": action.get("type"),
            "mode": "http",
            "success": False,
            "message": (
                f"Cannot apply bench_to_start swap: {who} is locked because "
                "their game has already started."
            ),
        }

    # Extract ESPN ids / slot ids. These attribute names come from espn_api's
    # Player object (see your REPL: playerId, lineupSlot, etc.).
    def _player_id(p: Any) -> Optional[int]:
        return getattr(p, "playerId", None) or getattr(p, "id", None)

    def _slot_id(p: Any) -> Optional[int]:
        code = getattr(p, "lineupSlot", None)
        if code is None:
            return None
        return SLOT_CODE_TO_ID.get(str(code))

    bench_player_id = _player_id(bench_p)
    starter_player_id = _player_id(starter_p)
    bench_slot_id = _slot_id(bench_p)
    starter_slot_id = _slot_id(starter_p)

    if (
        bench_player_id is None
        or starter_player_id is None
        or bench_slot_id is None
        or starter_slot_id is None
    ):
        return {
            "id": action.get("id"),
            "type": action.get("type"),
            "mode": "http",
            "success": False,
            "message": (
                "Missing or non-numeric playerId/lineupSlot on ESPN Player "
                "objects; inspect my_team.roster in a REPL to derive the "
                "proper slot-id mapping before enabling HTTP writes."
            ),
        }

    # League / team / scoring context
    team_id = getattr(my_team, "team_id", None) or getattr(my_team, "teamId", None)
    scoring_period = getattr(league, "currentScoringPeriodId", None) or getattr(
        league, "current_week", None
    )
    if scoring_period is None:
        scoring_period = CURRENT_WEEK + 1  # conservative fallback

    # SWID in memberId is expected with braces.
    raw_swid = str(team_cfg.get("espn_swid", "")).strip()
    if not raw_swid:
        member_id = ""
    elif raw_swid.startswith("{") and raw_swid.endswith("}"):
        member_id = raw_swid
    else:
        member_id = "{" + raw_swid.strip("{}") + "}"

    url = (
        "https://lm-api-writes.fantasy.espn.com/apis/v3/games/ffl/"
        f"seasons/{team_cfg['season_year']}/segments/0/"
        f"leagues/{team_cfg['league_id']}/transactions/"
    )

    payload: Dict[str, Any] = {
        "isLeagueManager": False,
        "teamId": team_id,
        "type": "ROSTER",
        "memberId": member_id,
        "scoringPeriodId": int(scoring_period),
        "executionType": "EXECUTE",
        "items": [
            {
                "playerId": int(bench_player_id),
                "type": "LINEUP",
                "fromLineupSlotId": bench_slot_id,
                "toLineupSlotId": starter_slot_id,
            },
            {
                "playerId": int(starter_player_id),
                "type": "LINEUP",
                "fromLineupSlotId": starter_slot_id,
                "toLineupSlotId": bench_slot_id,
            },
        ],
    }

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "x-fantasy-platform": "espn-fantasy-web",
        "x-fantasy-source": "kona",
    }

    try:
        resp = session.post(url, json=payload, headers=headers, timeout=10)
    except Exception as exc:  # pragma: no cover - network/IO
        return {
            "id": action.get("id"),
            "type": action.get("type"),
            "mode": "http",
            "success": False,
            "message": f"HTTP error calling ESPN lineup endpoint: {exc}",
        }

    if not resp.ok:
        return {
            "id": action.get("id"),
            "type": action.get("type"),
            "mode": "http",
            "success": False,
            "message": f"ESPN lineup API returned HTTP {resp.status_code}: {resp.text[:200]}",
        }

    return {
        "id": action.get("id"),
        "type": action.get("type"),
        "mode": "http",
        "success": True,
        "message": (
            f"Successfully submitted bench_to_start transaction for "
            f"{add_name} over {drop_name}."
        ),
    }


def _apply_fa_bench_http(
    session: requests.Session,
    team_cfg: Dict[str, Any],
    action: Dict[str, Any],
) -> Dict[str, Any]:
    """
    HTTP implementation for a fa_for_bench action when can_add_now=True.

    Sends a FREEAGENT transaction with an ADD for the FA and a DROP for the
    specified bench player, mirroring the curl captured in notes.txt.
    """

    add_name = action.get("add_name")
    drop_name = action.get("drop_name")
    can_add_now = bool(action.get("can_add_now", False))

    if not can_add_now:
        return {
            "id": action.get("id"),
            "type": action.get("type"),
            "mode": "http",
            "success": False,
            "message": "Cannot apply fa_for_bench: can_add_now is False (waiver claim).",
        }

    if not add_name or not drop_name:
        return {
            "id": action.get("id"),
            "type": action.get("type"),
            "mode": "http",
            "success": False,
            "message": "Missing add_name/drop_name on action; cannot apply FA move.",
        }

    try:
        league = _get_league(team_cfg)
        my_team = _find_my_team(league, team_cfg)
    except Exception as exc:  # pragma: no cover - defensive
        return {
            "id": action.get("id"),
            "type": action.get("type"),
            "mode": "http",
            "success": False,
            "message": f"Failed to load league/team from espn_adapter: {exc}",
        }

    roster = list(my_team.roster)

    def _find_roster_player(name: str):
        exact = [p for p in roster if getattr(p, "name", "") == name]
        if exact:
            return exact[0]
        lower = name.lower()
        ci = [p for p in roster if getattr(p, "name", "").lower() == lower]
        return ci[0] if ci else None

    drop_p = _find_roster_player(drop_name)
    if drop_p is None:
        return {
            "id": action.get("id"),
            "type": action.get("type"),
            "mode": "http",
            "success": False,
            "message": f"Could not find drop player {drop_name!r} on your roster.",
        }

    # Guardrail: do not attempt to drop a locked player (game already started).
    if _is_player_locked(drop_p):
        return {
            "id": action.get("id"),
            "type": action.get("type"),
            "mode": "http",
            "success": False,
            "message": (
                f"Cannot submit free-agent transaction: drop target "
                f"{getattr(drop_p, 'name', drop_name)!r} is locked because "
                "their game has already started."
            ),
        }

    # Locate the FA in the current free agent pool by name/position.
    try:
        fa_players = league.free_agents(size=200)
    except Exception as exc:  # pragma: no cover - defensive
        return {
            "id": action.get("id"),
            "type": action.get("type"),
            "mode": "http",
            "success": False,
            "message": f"Failed to load free agents from ESPN: {exc}",
        }

    def _find_fa_player(name: str, position: str):
        exact = [
            p
            for p in fa_players
            if getattr(p, "name", "") == name
            and getattr(p, "position", "") == position
        ]
        if exact:
            return exact[0]
        lower = name.lower()
        ci = [
            p
            for p in fa_players
            if getattr(p, "name", "").lower() == lower
            and getattr(p, "position", "") == position
        ]
        return ci[0] if ci else None

    fa_pos = action.get("add_position") or ""
    fa_p = _find_fa_player(add_name, fa_pos)
    if fa_p is None:
        return {
            "id": action.get("id"),
            "type": action.get("type"),
            "mode": "http",
            "success": False,
            "message": (
                f"Could not find free agent {add_name!r} ({fa_pos}) "
                "in ESPN free agent pool."
            ),
        }

    def _player_id(p: Any) -> Optional[int]:
        return getattr(p, "playerId", None) or getattr(p, "id", None)

    fa_player_id = _player_id(fa_p)
    drop_player_id = _player_id(drop_p)

    if fa_player_id is None or drop_player_id is None:
        return {
            "id": action.get("id"),
            "type": action.get("type"),
            "mode": "http",
            "success": False,
            "message": (
                "Missing playerId for FA/drop player; inspect espn_api Player "
                "objects to update espn_actions."
            ),
        }

    team_id = getattr(my_team, "team_id", None) or getattr(my_team, "teamId", None)
    scoring_period = getattr(league, "currentScoringPeriodId", None) or getattr(
        league, "current_week", None
    )
    if scoring_period is None:
        scoring_period = CURRENT_WEEK + 1

    raw_swid = str(team_cfg.get("espn_swid", "")).strip()
    if not raw_swid:
        member_id = ""
    elif raw_swid.startswith("{") and raw_swid.endswith("}"):
        member_id = raw_swid
    else:
        member_id = "{" + raw_swid.strip("{}") + "}"

    url = (
        "https://lm-api-writes.fantasy.espn.com/apis/v3/games/ffl/"
        f"seasons/{team_cfg['season_year']}/segments/0/"
        f"leagues/{team_cfg['league_id']}/transactions/"
    )

    payload: Dict[str, Any] = {
        "isLeagueManager": False,
        "teamId": team_id,
        "type": "FREEAGENT",
        "memberId": member_id,
        "scoringPeriodId": int(scoring_period),
        "executionType": "EXECUTE",
        "items": [
            {
                "playerId": int(fa_player_id),
                "type": "ADD",
                "toTeamId": team_id,
            },
            {
                "playerId": int(drop_player_id),
                "type": "DROP",
                "fromTeamId": team_id,
            },
        ],
    }

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "x-fantasy-platform": "espn-fantasy-web",
        "x-fantasy-source": "kona",
    }

    try:
        resp = session.post(url, json=payload, headers=headers, timeout=10)
    except Exception as exc:  # pragma: no cover - network/IO
        return {
            "id": action.get("id"),
            "type": action.get("type"),
            "mode": "http",
            "success": False,
            "message": f"HTTP error calling ESPN free-agent endpoint: {exc}",
        }

    if not resp.ok:
        return {
            "id": action.get("id"),
            "type": action.get("type"),
            "mode": "http",
            "success": False,
            "message": f"ESPN free-agent API returned HTTP {resp.status_code}: {resp.text[:200]}",
        }

    return {
        "id": action.get("id"),
        "type": action.get("type"),
        "mode": "http",
        "success": True,
        "message": (
            f"Successfully submitted fa_for_bench transaction: "
            f"ADD {add_name} / DROP {drop_name}."
        ),
    }


def apply_actions_for_team(
    team_cfg: Dict[str, Any],
    actions: List[Dict[str, Any]],
    mode: str = "dry_run",
) -> List[Dict[str, Any]]:
    """
    Apply a list of suggested actions for one team.

    Parameters
    ----------
    team_cfg:
        One of the entries from config.TEAMS, containing ESPN league / auth
        info (league_id, season_year, espn_s2, espn_swid, etc.).
    actions:
        List of action dictionaries, typically derived from SuggestedAction
        models. Each action should at least have:
          - id
          - type ("bench_to_start" | "fa_for_starter" | "fa_for_bench")
          - add_name, add_position
          - drop_name, drop_position
          - gain, can_add_now
    mode:
        Execution mode. For now we only support "dry_run". In the future you
        can add:
          - "http"    -> use HTTP + cookies to call ESPN's endpoints
          - "browser" -> drive a real browser via Playwright/Selenium

    Returns
    -------
    List[Dict[str, Any]]:
        Per-action execution result:
          - id          : action id
          - type        : action type
          - mode        : execution mode used
          - success     : bool
          - message     : human-readable summary
    """

    results: List[Dict[str, Any]] = []

    http_session: requests.Session | None = None

    for a in actions:
        action_id = a.get("id", "")
        action_type = a.get("type", "")
        add_name = a.get("add_name", "UNKNOWN")
        drop_name = a.get("drop_name", "UNKNOWN")
        gain = a.get("gain", 0.0)

        if mode == "dry_run":
            msg = (
                f"[DRY RUN] Would apply {action_type} for team "
                f"{team_cfg.get('team_name_keyword')!r}: "
                f"ADD {add_name} / DROP {drop_name} (+{gain:.1f} pts)."
            )
            results.append(
                {
                    "id": action_id,
                    "type": action_type,
                    "mode": mode,
                    "success": False,
                    "message": msg,
                }
            )
        elif mode == "http":
            # Lazily create a shared session for this batch.
            if http_session is None:
                http_session = _http_session_for_team(team_cfg)

            if action_type == "bench_to_start":
                result = _apply_bench_to_start_http(http_session, team_cfg, a)
            elif action_type == "fa_for_bench":
                result = _apply_fa_bench_http(http_session, team_cfg, a)
            elif action_type == "fa_for_starter":
                # For now, treat FA->starter as "add to bench, drop" and allow
                # the optimizer / UI to promote them on the next run. This is
                # safer than trying to manipulate lineup slots in the same
                # transaction.
                result = _apply_fa_bench_http(http_session, team_cfg, a)
            else:
                result = {
                    "id": action_id,
                    "type": action_type,
                    "mode": mode,
                    "success": False,
                    "message": (
                        "HTTP execution not implemented for this action type."
                    ),
                }

            results.append(result)
        else:
            # Placeholder for future "browser" mode or other strategies.
            results.append(
                {
                    "id": action_id,
                    "type": action_type,
                    "mode": mode,
                    "success": False,
                    "message": "Execution mode not implemented yet.",
                }
            )

    return results


