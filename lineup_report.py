# lineup_report.py
#
# Core lineup logic for LineupIQ.
# - Optimizes starters for a team
# - Suggests bench ↔ starter swaps
# - Suggests FA → starter upgrades
# - Suggests FA → bench upgrades
#
# Used both by the CLI entrypoint AND the FastAPI app (via compute_lineup_state).

from __future__ import annotations

from typing import List, Tuple, Optional, Dict, Any

from models import Player, Slot, Assignment, FreeAgent  # type: ignore[import]
from espn_adapter import (  # type: ignore[import]
    fetch_roster_from_espn,
    fetch_free_agents_from_espn,
)
from config import TEAMS, DEFAULT_TEAM_KEY  # type: ignore[import]

# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

# Players in these statuses (or with 0 proj) are treated as "stash", not droppable.
STASH_STATUSES = {
    "O",
    "OUT",
    "BYE",
    "D",
    "DOUBTFUL",
    "IR",
    "INJURY_RESERVE",
}

# “Healthy enough” to be considered for starting / dropping
HEALTHY_STATUSES = {
    "ACTIVE",
    "NORMAL",
    "QUESTIONABLE",  # you can adjust this if you’d rather stash Q as well
}

FLEX_ELIGIBLE = {"RB", "WR", "TE"}


def _friendly_slot_name_for_starter(p: Player) -> str:
    """
    Human-friendly slot label for current ESPN starters.

    We prefer the *actual* ESPN slot code if available (via espn_slot),
    and normalise FLEX-style codes like 'RB/WR/TE' to the simpler 'FLEX'
    so it's obvious in the UI which starter is occupying the flex slot.
    """
    code = getattr(p, "espn_slot", None) or p.position or ""
    code_up = str(code).upper()
    if code_up in {"RB/WR/TE", "RB/WR", "FLEX"}:
        return "FLEX"
    return code_up

def _slot_allows_position(slot_code: str, position: str) -> bool:
    """
    Return True if a given ESPN lineup slot code can legally hold a player at
    `position`.

    This is stricter than just comparing positions – it encodes the Flex
    rules the ESPN UI enforces:
      - RB/WR/TE/FLEX slots can take any of RB, WR, TE
      - pure RB / WR / TE slots only accept that position
      - DST and K are isolated
      - OP (offensive player) can take any non-DST, non-K position
    """
    slot = (slot_code or "").upper()
    pos = position.upper()

    if slot in {"RB", "RB1", "RB2"}:
        return pos == "RB"
    if slot in {"WR", "WR1", "WR2"}:
        return pos == "WR"
    if slot in {"TE"}:
        return pos == "TE"
    if slot in {"DST", "D/ST"}:
        return pos == "DST"
    if slot in {"K"}:
        return pos == "K"
    if slot in {"FLEX", "RB/WR", "RB/WR/TE"}:
        return pos in FLEX_ELIGIBLE
    if slot in {"OP", "SUPERFLEX"}:
        # Allow any offensive position in generic OP slot.
        return pos in {"QB", "RB", "WR", "TE"}

    # Fallback: if we don't recognise the slot code, require exact match.
    return slot == pos


def _status(p: Player) -> str:
    return (p.status or "").upper()


def _player_to_dict(p: Player) -> Dict[str, Any]:
    """
    Convert a Player dataclass into a JSON-serializable dict suitable for the API.

    We include a few extra fields (like `was_starter`) that the FastAPI layer
    may or may not use, but which are handy for the CLI/UI.
    """
    if p is None:
        return {}

    return {
        "name": p.name,
        "position": p.position,
        "projection": float(p.projection),
        "status": p.status,
        "was_starter": bool(p.is_starter),
        # Optional; may or may not exist on Player depending on adapter
        "nfl_team": getattr(p, "nfl_team", None),
    }


def _fa_to_dict(fa: FreeAgent) -> Dict[str, Any]:
    """Convert a FreeAgent dataclass into a JSON-serializable dict for the API."""
    if fa is None:
        return {}

    return {
        "name": fa.name,
        "position": fa.position,
        "projection": float(fa.projection),
    }


# ---------------------------------------------------------------------------
# Lineup optimization
# ---------------------------------------------------------------------------


def eligible(player: Player, slot: Slot) -> bool:
    """Return True if a player can fill a given slot."""
    return player.position in slot.eligible_positions


def optimize_lineup(players: List[Player], slots: List[Slot]) -> Tuple[List[Assignment], List[Player]]:
    """
    Simple greedy optimizer:
    - ignores players with stash statuses or 0 projection
    - fills each slot in order with highest-projection eligible player
    """
    usable = [
        p
        for p in players
        if p.projection > 0 and _status(p) not in STASH_STATUSES
    ]

    remaining = usable.copy()
    starters: List[Assignment] = []

    for slot in slots:
        best: Optional[Player] = None
        for p in remaining:
            if eligible(p, slot):
                if best is None or p.projection > best.projection:
                    best = p

        if best is not None:
            starters.append(Assignment(slot, best))
            remaining.remove(best)
        else:
            starters.append(Assignment(slot, None))

    # Post-process starters so that, when possible, the *lowest* projection
    # FLEX-eligible starter (RB/WR/TE) occupies the FLEX slot. This mirrors the
    # way savvy managers use FLEX and makes downstream swap logic easier to
    # reason about.
    slot_name_to_index: Dict[str, int] = {
        a.slot.name: idx for idx, a in enumerate(starters)
    }
    flex_idx = slot_name_to_index.get("FLEX")
    if flex_idx is not None and starters[flex_idx].player is not None:
        # Collect all FLEX-eligible starters across RB/WR/TE/FLEX slots.
        flex_candidates: List[Tuple[int, Player]] = []
        for idx, a in enumerate(starters):
            p = a.player
            if p is None:
                continue
            if p.position in FLEX_ELIGIBLE and a.slot.name in {
                "RB1",
                "RB2",
                "WR1",
                "WR2",
                "TE",
                "FLEX",
            }:
                flex_candidates.append((idx, p))

        if len(flex_candidates) >= 2:
            # Find the lowest-projection FLEX-eligible starter.
            min_idx, min_player = min(
                flex_candidates, key=lambda pair: pair[1].projection
            )

            # If they're not already in FLEX, and the current FLEX player can
            # legally occupy the min player's slot, swap them.
            if min_idx != flex_idx:
                current_flex_player = starters[flex_idx].player
                target_slot = starters[min_idx].slot
                if (
                    current_flex_player is not None
                    and eligible(current_flex_player, target_slot)
                ):
                    starters[flex_idx].player, starters[min_idx].player = (
                        starters[min_idx].player,
                        starters[flex_idx].player,
                    )

    bench = remaining
    return starters, bench


# ---------------------------------------------------------------------------
# 1) Bench ↔ Starter swaps
# ---------------------------------------------------------------------------


def suggest_bench_start_swaps(players: List[Player], slots: List[Slot]) -> List[Tuple[Player, Player, float]]:
    """
    Compare:
      - your *current* starters (is_starter=True)
      - optimal starters (from optimizer)

    Return (bench_player, starter_to_sit, gain) tuples.

    Only:
      - promotes healthy bench players
      - demotes healthy starters
      - uses same-position swaps, except FLEX (RB/WR/TE only).
    """
    base_starters = [p for p in players if p.is_starter]

    optimal_assignments, _ = optimize_lineup(players, slots)
    optimal_starters = [a.player for a in optimal_assignments if a.player is not None]

    promoted = [p for p in optimal_starters if p not in base_starters]
    demoted = [p for p in base_starters if p not in optimal_starters]

    suggestions: List[Tuple[Player, Player, float]] = []

    # Demotable starters = any starter who is *not* part of the optimized
    # lineup and has a non-zero projection. We intentionally allow both
    # healthy and stash/IR statuses here, so if you accidentally left an
    # IR/BYE/OUT player in your starting lineup we will happily suggest
    # benching them in favour of a healthy bench player.
    #
    # As we assign swaps, we remove starters from this list so each starter is
    # only suggested to be benched once. This approximates the "best
    # combination" of swaps instead of multiple suggestions all targeting the
    # same player.
    # NOTE: we intentionally allow IR/BYE/OUT starters here (projection may be
    # 0.0). If you accidentally left an injured player in your lineup, we want
    # to suggest benching them in favour of a healthy bench option.
    demoted_healthy: List[Player] = list(demoted)

    for bench_player in promoted:
        if bench_player is None:
            continue
        # Only promote healthy bench players
        if _status(bench_player) not in HEALTHY_STATUSES or bench_player.projection <= 0:
            continue

        # Only allow demotions where the starter's actual ESPN slot can
        # legally hold the bench player's position. This prevents illegal
        # swaps like putting a WR directly into a pure RB slot; those swaps
        # are only valid when the starter is actually occupying FLEX.
        candidates: List[Player] = []
        for d in demoted_healthy:
            slot_code = getattr(d, "espn_slot", "")
            if _slot_allows_position(slot_code, bench_player.position):
                candidates.append(d)

        if not candidates:
            # No legal single-step swap that respects ESPN's slot rules.
            continue

        starter_to_sit = min(candidates, key=lambda x: x.projection)
        gain = bench_player.projection - starter_to_sit.projection
        if gain > 0.5:  # small threshold to avoid noise
            suggestions.append((bench_player, starter_to_sit, gain))
            # Do not suggest benching the same starter multiple times; once
            # we've assigned this demotion, remove them from the pool so other
            # bench players can look for different opportunities (e.g. FLEX).
            if starter_to_sit in demoted_healthy:
                demoted_healthy.remove(starter_to_sit)

    # Fallback: if there are still demoted starters who are BYE / 0-proj / stash
    # but did not get a suggested swap from the optimized lineup, ensure we
    # still recommend replacing them with the best legal healthy bench option
    # at the *same slot*. This covers simple cases like "BYE RB in RB slot with
    # a healthy RB on the bench", even when the global optimizer prefers to
    # reshuffle other positions (e.g. using a WR in FLEX instead).
    # Track which bench players/starters we've already used in suggestions.
    # Use simple lists instead of sets because Player dataclasses are
    # unhashable by default (eq=True, frozen=False).
    used_bench: List[Player] = [bp for (bp, _sp, _g) in suggestions]
    used_starters: List[Player] = [sp for (_bp, sp, _g) in suggestions]

    for starter in demoted:
        if starter in used_starters:
            continue

        # If the starter is healthy with a real projection, we only want to
        # bench them when the full optimizer found a better promotion; at this
        # point, skip healthy non-stash starters.
        if _status(starter) in HEALTHY_STATUSES and starter.projection > 0:
            continue

        slot_code = getattr(starter, "espn_slot", "")

        candidates: List[Player] = []
        for p in players:
            if p.is_starter:
                continue
            if p in used_bench:
                continue
            if _status(p) not in HEALTHY_STATUSES or p.projection <= 0:
                continue
            if not _slot_allows_position(slot_code, p.position):
                continue
            candidates.append(p)

        if not candidates:
            continue

        best_bench = max(candidates, key=lambda x: x.projection)
        gain = best_bench.projection - starter.projection
        if gain > 0.5:
            suggestions.append((best_bench, starter, gain))
            if best_bench not in used_bench:
                used_bench.append(best_bench)
            if starter not in used_starters:
                used_starters.append(starter)

    suggestions.sort(key=lambda t: t[2], reverse=True)
    return suggestions


# ---------------------------------------------------------------------------
# 2) FA → starter upgrades
# ---------------------------------------------------------------------------


def suggest_fa_starter_upgrades(
    players: List[Player],
    slots: List[Slot],
    free_agents: List[FreeAgent],
) -> List[Tuple[FreeAgent, Player, float]]:
    """
    For each free agent:
      - Imagine adding them to your roster and re-running the optimizer
      - If they make the optimal starting lineup, see which *healthy* starter they bump
      - Only suggest like-for-like (with FLEX logic)
    """
    base_assignments, _ = optimize_lineup(players, slots)
    base_starters = [a.player for a in base_assignments if a.player is not None]

    upgrades: List[Tuple[FreeAgent, Player, float]] = []

    for fa in free_agents:
        if fa.projection <= 0:
            continue
        # Never suggest FAs who are on BYE (or explicitly marked as stash).
        if _status(Player(fa.name, fa.position, fa.projection, fa.status)) in STASH_STATUSES:
            continue

        fa_player = Player(
            name=fa.name,
            position=fa.position,
            projection=fa.projection,
            status="ACTIVE",
            is_starter=False,
        )

        roster_plus = players + [fa_player]
        new_assignments, _ = optimize_lineup(roster_plus, slots)
        new_starters = [a.player for a in new_assignments if a.player is not None]

        if fa_player not in new_starters:
            continue

        bumped = [p for p in base_starters if p not in new_starters and p is not None]

        # Only bump healthy, non-stash players
        bumped_healthy = [
            p for p in bumped
            if _status(p) in HEALTHY_STATUSES and p.projection > 0
        ]
        if not bumped_healthy:
            continue

        candidates: List[Player] = []

        same_pos = [p for p in bumped_healthy if p.position == fa.position]
        if same_pos:
            candidates = same_pos
        elif fa.position in FLEX_ELIGIBLE:
            flex_candidates = [p for p in bumped_healthy if p.position in FLEX_ELIGIBLE]
            candidates = flex_candidates

        if not candidates:
            continue

        bumped_player = min(candidates, key=lambda x: x.projection)
        gain = fa.projection - bumped_player.projection
        if gain > 0.5:
            upgrades.append((fa, bumped_player, gain))

    upgrades.sort(key=lambda t: t[2], reverse=True)
    return upgrades


# ---------------------------------------------------------------------------
# 3) FA → bench upgrades
# ---------------------------------------------------------------------------


def suggest_fa_bench_upgrades(
    players: List[Player],
    free_agents: List[FreeAgent],
) -> List[Tuple[FreeAgent, Player, float]]:
    """
    Compare free agents against your *bench only*.
    - Bench players that are IR / OUT / BYE / 0-proj are treated as stash, not droppable
    - Like-for-like position, with FLEX RB/WR/TE sharing a pool
    """
    # Droppable bench = non-starters, healthy, non-stash, >0 projection
    droppable_bench = [
        p
        for p in players
        if (not p.is_starter)
        and _status(p) in HEALTHY_STATUSES
        and p.projection > 0
    ]

    upgrades: List[Tuple[FreeAgent, Player, float]] = []

    for fa in free_agents:
        if fa.projection <= 0:
            continue
        # Never suggest FAs who are on BYE (or explicitly marked as stash).
        if _status(Player(fa.name, fa.position, fa.projection, fa.status)) in STASH_STATUSES:
            continue

        # Strict same-position comparison first
        comparable = [p for p in droppable_bench if p.position == fa.position]

        # FLEX logic
        if fa.position in FLEX_ELIGIBLE:
            flex_group = [p for p in droppable_bench if p.position in FLEX_ELIGIBLE]
            if flex_group:
                comparable = flex_group

        if not comparable:
            continue

        worst = min(comparable, key=lambda p: p.projection)
        gain = fa.projection - worst.projection
        if gain > 0.5:
            upgrades.append((fa, worst, gain))

    upgrades.sort(key=lambda t: t[2], reverse=True)
    return upgrades


# ---------------------------------------------------------------------------
# Public API: compute_lineup_state(team_key)
# ---------------------------------------------------------------------------


def _default_slots() -> List[Slot]:
    """Standard ESPN lineup for your league."""
    return [
        Slot("QB", ["QB"]),
        Slot("RB1", ["RB"]),
        Slot("RB2", ["RB"]),
        Slot("WR1", ["WR"]),
        Slot("WR2", ["WR"]),
        Slot("TE", ["TE"]),
        Slot("FLEX", ["RB", "WR", "TE"]),
        Slot("DST", ["DST"]),
        Slot("K", ["K"]),
    ]


def run_lineup_for_team(
    team_key: str, team_cfg_override: Optional[dict] = None
) -> Dict[str, Any]:
    """
    Core function used by the FastAPI app.

    Returns a JSON-serializable dictionary describing:
      - current_starters: list of {"slot_name": str, "player": {...}}
      - bench: list of player dicts
      - stash: list of player dicts
      - bench_swaps: list of {"bench_player": {...}, "starter_player": {...}, "gain": float}
      - fa_starter_upgrades: list of {"fa": {...}, "bumped": {...}, "gain": float, "can_add_now": bool}
      - fa_bench_upgrades: list of {"fa": {...}, "drop": {...}, "gain": float, "can_add_now": bool}
    """
    if team_cfg_override is None and team_key not in TEAMS:
        raise KeyError(f"Unknown team key: {team_key}")

    team_cfg = team_cfg_override or TEAMS[team_key]

    roster: List[Player] = fetch_roster_from_espn(team_cfg)
    free_agents: List[FreeAgent] = fetch_free_agents_from_espn(team_cfg, max_players=50)

    slots = _default_slots()

    # Base (ESPN) starters from your current lineup
    base_starters_players: List[Player] = [p for p in roster if p.is_starter]
    base_total_points = sum(p.projection for p in base_starters_players)

    # Optimized lineup (what the engine thinks you *should* start)
    optimized_assignments, bench_after_opt = optimize_lineup(roster, slots)
    optimized_starters_players = [
        a.player for a in optimized_assignments if a.player is not None
    ]
    optimized_total_points = sum(p.projection for p in optimized_starters_players)

    # Stash = non-starters who are injured / bye / 0-proj
    stash_players: List[Player] = []
    for p in roster:
        if p.is_starter:
            continue
        if _status(p) in STASH_STATUSES or p.projection <= 0:
            stash_players.append(p)

    # Bench to display = current bench minus stash
    bench_display = [
        p for p in roster if (not p.is_starter) and p not in stash_players
    ]

    bench_swaps_raw = suggest_bench_start_swaps(roster, slots)
    fa_starter_upgrades_raw = suggest_fa_starter_upgrades(roster, slots, free_agents)
    fa_bench_upgrades_raw = suggest_fa_bench_upgrades(roster, free_agents)

    # Build JSON-friendly structure expected by app.py
    # current_starters = your actual ESPN lineup
    current_starters = [
        {
            "slot_name": _friendly_slot_name_for_starter(p),
            "player": _player_to_dict(p),
        }
        for p in base_starters_players
    ]

    # optimized_starters = what LineupIQ would start for you
    optimized_starters = [
        {
            "slot_name": a.slot.name,
            "player": _player_to_dict(a.player) if a.player is not None else None,
        }
        for a in optimized_assignments
    ]

    bench_players = [_player_to_dict(p) for p in bench_display]
    stash = [_player_to_dict(p) for p in stash_players]

    bench_swaps = [
        {
            "bench_player": _player_to_dict(bp),
            "starter_player": _player_to_dict(sp),
            "gain": float(gain),
        }
        for (bp, sp, gain) in bench_swaps_raw
    ]

    fa_starter_upgrades = [
        {
            "fa": _fa_to_dict(fa),
            "bumped": _player_to_dict(bumped),
            "gain": float(gain),
            "can_add_now": bool(fa.can_add_now),
        }
        for (fa, bumped, gain) in fa_starter_upgrades_raw
    ]

    fa_bench_upgrades = [
        {
            "fa": _fa_to_dict(fa),
            "drop": _player_to_dict(drop),
            "gain": float(gain),
            "can_add_now": bool(fa.can_add_now),
        }
        for (fa, drop, gain) in fa_bench_upgrades_raw
    ]

    state: Dict[str, Any] = {
        "team_key": team_key,
        "team_name": team_cfg.get("team_name_keyword"),
        # For FastAPI:
        "current_starters": current_starters,
        "optimized_starters": optimized_starters,
        "bench": bench_players,
        "stash": stash,
        "bench_swaps": bench_swaps,
        "fa_starter_upgrades": fa_starter_upgrades,
        "fa_bench_upgrades": fa_bench_upgrades,
        "optimized_total_projection": float(optimized_total_points),
        "base_total_projection": float(base_total_points),
        # For CLI convenience (show base lineup total nicely rounded):
        "total_projection": round(base_total_points, 1),
    }

    return state


# ---------------------------------------------------------------------------
# CLI entrypoint (still handy while developing)
# ---------------------------------------------------------------------------


def print_lineup_report(team_key: str) -> None:
    """Pretty-print the same info that run_lineup_for_team returns."""
    state = run_lineup_for_team(team_key)

    print(
        f"\n========= LINEUPIQ — OPTIMAL STARTERS (THIS WEEK) "
        f"[{state['team_name']}] ========="
    )
    # Use optimized_starters for this report so it always reflects the engine's
    # recommended lineup, even though the API /state endpoint shows ESPN
    # "current" starters.
    for s in state["optimized_starters"]:
        slot = s.get("slot_name", "UNK")
        p = s.get("player")
        if not p or p.get("name") is None:
            print(f"{slot:5} -> [EMPTY]")
        else:
            flag = "START" if p.get("was_starter") else "BENCH→START"
            print(
                f"{slot:5} -> {p.get('name','UNKNOWN'):<22} "
                f"{p.get('position','UNK'):3}  "
                f"PROJ {p.get('projection',0.0):5.1f}  "
                f"[{p.get('status','UNKNOWN')}]  ({flag})"
            )
    print(
        f"\nTOTAL EXPECTED POINTS (optimized): "
        f"{state['optimized_total_projection']:.1f}"
    )
    print(
        f"TOTAL CURRENT LINEUP POINTS (ESPN starters): "
        f"{state['total_projection']:.1f}\n"
    )

    print("============ CURRENT BENCH (ELIGIBLE THIS WEEK) ============")
    for p in state["bench"]:
        print(
            f"{p.get('name','UNKNOWN'):<22} {p.get('position','UNK'):3}  "
            f"PROJ {p.get('projection',0.0):5.1f}  "
            f"[{p.get('status','UNKNOWN')}]"
        )

    print("\n============ STASH (IR / OUT / BYE / 0-PROJ) ============")
    if not state["stash"]:
        print("No obvious stash players.")
    else:
        for p in state["stash"]:
            print(
                f"{p.get('name','UNKNOWN'):<22} {p.get('position','UNK'):3}  "
                f"PROJ {p.get('projection',0.0):5.1f}  "
                f"[{p.get('status','UNKNOWN')}]"
            )

    print("\n========== SUGGESTED BENCH ↔ STARTER SWAPS (LOW RISK) =====")
    if not state["bench_swaps"]:
        print("No bench players clearly out-project your current starters.")
    else:
        for s in state["bench_swaps"]:
            print(
                f"START {s['bench_player'].get('name','UNKNOWN'):<18} "
                f"({s['bench_player'].get('position','UNK')}) "
                f"PROJ {s['bench_player'].get('projection',0.0):5.1f} > "
                f"BENCH {s['starter_player'].get('name','UNKNOWN'):<18} "
                f"PROJ {s['starter_player'].get('projection',0.0):5.1f}  "
                f"(+{s.get('gain',0.0):.1f} pts)"
            )

    print("\n========== FREE AGENTS WHO WOULD START FOR YOU ============")
    imm = [u for u in state["fa_starter_upgrades"] if u.get("can_add_now")]
    wai = [u for u in state["fa_starter_upgrades"] if not u.get("can_add_now")]

    print("-- Immediate adds (green +) --")
    if not imm:
        print("No free agents clearly improve your starting lineup right now.")
    else:
        for u in imm:
            print(
                f"ADD {u['fa'].get('name','UNKNOWN'):<18} "
                f"({u['fa'].get('position','UNK')}) "
                f"PROJ {u['fa'].get('projection',0.0):5.1f}  "
                f"> BUMPS {u['bumped'].get('name','UNKNOWN'):<18} "
                f"PROJ {u['bumped'].get('projection',0.0):5.1f}  "
                f"(+{u.get('gain',0.0):.1f} pts)"
            )

    print("\n-- Waiver claims (yellow +) --")
    if not wai:
        print("No obvious waiver-wire upgrades to your starting lineup.")
    else:
        for u in wai:
            print(
                f"CLAIM {u['fa'].get('name','UNKNOWN'):<18} "
                f"({u['fa'].get('position','UNK')}) "
                f"PROJ {u['fa'].get('projection',0.0):5.1f}  "
                f"> WOULD BUMP {u['bumped'].get('name','UNKNOWN'):<18} "
                f"PROJ {u['bumped'].get('projection',0.0):5.1f}  "
                f"(+{u.get('gain',0.0):.1f} pts)"
            )

    print("\n========== FREE AGENTS WHO ARE BETTER STASHES =============")
    imm_bench = [u for u in state["fa_bench_upgrades"] if u.get("can_add_now")]
    wai_bench = [u for u in state["fa_bench_upgrades"] if not u.get("can_add_now")]

    print("-- Immediate bench upgrades (green +) --")
    if not imm_bench:
        print("No clear bench upgrades among immediate adds.")
    else:
        for u in imm_bench:
            print(
                f"ADD {u['fa'].get('name','UNKNOWN'):<18} "
                f"({u['fa'].get('position','UNK')}) "
                f"PROJ {u['fa'].get('projection',0.0):5.1f}  "
                f"> DROP {u['drop'].get('name','UNKNOWN'):<18} "
                f"PROJ {u['drop'].get('projection',0.0):5.1f}  "
                f"(+{u.get('gain',0.0):.1f} pts)"
            )

    print("\n-- Waiver bench upgrades (yellow +) --")
    if not wai_bench:
        print("No obvious waiver-wire bench upgrades.")
    else:
        for u in wai_bench:
            print(
                f"CLAIM {u['fa'].get('name','UNKNOWN'):<18} "
                f"({u['fa'].get('position','UNK')}) "
                f"PROJ {u['fa'].get('projection',0.0):5.1f}  "
                f"> WOULD DROP {u['drop'].get('name','UNKNOWN'):<18} "
                f"PROJ {u['drop'].get('projection',0.0):5.1f}  "
                f"(+{u.get('gain',0.0):.1f} pts)"
            )

    print("\n============================================================\n")


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(
        description="LineupIQ: optimize lineups for one of your fantasy teams."
    )
    parser.add_argument(
        "--team",
        "-t",
        default=DEFAULT_TEAM_KEY,
        help=f"Team key from config.TEAMS (default: {DEFAULT_TEAM_KEY})",
    )
    parser.add_argument(
        "--json-out",
        help="If set, write the lineup state as JSON to this file instead of pretty-printing.",
    )

    args = parser.parse_args()
    state = run_lineup_for_team(args.team)

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(state, f, indent=2)
        print(f"Wrote lineup state to {args.json_out}")
    else:
        print_lineup_report(args.team)
