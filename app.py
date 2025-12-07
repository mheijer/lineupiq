# app.py
#
# FastAPI wrapper around the LineupIQ engine.
# Exposes:
#   GET /health
#   GET /teams
#   GET /teams/{team_key}/state
#   GET /teams/{team_key}/plan
#   GET /actions/top
#
# Start with:
#   uvicorn app:app --reload

from enum import Enum
from typing import List, Optional, Dict, Any

from fastapi import FastAPI, HTTPException, Depends, Request, Response  # type: ignore[import]
from fastapi.middleware.cors import CORSMiddleware  # type: ignore[import]
from fastapi.responses import FileResponse  # type: ignore[import]
from pydantic import BaseModel  # type: ignore[import]

from config import TEAMS, CURRENT_WEEK, ACTION_MODE  # type: ignore[import]
from lineup_report import run_lineup_for_team  # type: ignore[import]
from espn_actions import apply_actions_for_team  # type: ignore[import]
from auth_db import (  # type: ignore[import]
    init_db,
    create_user,
    verify_user_credentials,
    get_user_by_id,
    set_espn_credentials,
    get_espn_credentials,
    get_managed_team_keys,
    set_managed_team_keys,
)
from auth_security import create_session_token, parse_session_token  # type: ignore[import]


# ---------------------------------------------------------------------------
# Pydantic view models
# ---------------------------------------------------------------------------

class PlayerRole(str, Enum):
    starter = "starter"
    bench = "bench"
    stash = "stash"


class PlayerView(BaseModel):
    name: str
    position: str                 # RB / WR / TE / QB / DST / K
    nfl_team: Optional[str] = None
    slot: Optional[str] = None    # QB, RB1, FLEX, etc.
    role: PlayerRole
    projection: float
    status: str                   # ACTIVE / OUT / DOUBTFUL / IR / BYE / etc.


class LineupView(BaseModel):
    team_key: str
    week: int
    total_projection: float
    starters: List[PlayerView]
    bench: List[PlayerView]
    stash: List[PlayerView]


class ActionType(str, Enum):
    bench_to_start = "bench_to_start"
    fa_for_starter = "fa_for_starter"
    fa_for_bench = "fa_for_bench"


class SuggestedAction(BaseModel):
    id: str                       # e.g. "cant_teach_matchups:fa_for_starter:Jordan Love"
    team_key: str
    week: int
    type: ActionType

    add_name: str
    add_position: str
    add_projection: float

    drop_name: str
    drop_position: str
    drop_projection: float

    gain: float
    can_add_now: bool
    reason: Optional[str] = None  # human-readable explanation


class TeamPlan(BaseModel):
    team_key: str
    week: int
    total_projection_optimized: float
    actions: List[SuggestedAction]


class ApplyActionsRequest(BaseModel):
    """
    Request body for POST /teams/{team_key}/actions/apply.

    For now this is a *dry run* – the server will resolve these IDs against the
    latest suggested plan and return which actions would be applied, but it
    does not yet talk back to ESPN.
    """
    action_ids: List[str]


class ApplyActionsResult(BaseModel):
    team_key: str
    week: int
    applied: List[SuggestedAction]
    unknown_action_ids: List[str]
    # Optional, per-action execution results from espn_actions.apply_actions_for_team
    execution: Optional[List[dict]] = None


class UserView(BaseModel):
    id: int
    email: str


class RegisterRequest(BaseModel):
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


class ESPNLinkRequest(BaseModel):
    espn_s2: str
    espn_swid: str


class ESPNStatus(BaseModel):
    linked: bool


class UserTeam(BaseModel):
    key: str
    platform: str
    league_id: int
    season_year: int
    team_name_keyword: str
    scoring: str
    enabled: bool


class UpdateUserTeamsRequest(BaseModel):
    team_keys: List[str]


class WaiverOption(BaseModel):
    add_name: str
    add_position: str
    add_projection: float
    drop_name: str
    drop_position: str
    drop_projection: float
    gain: float
    can_add_now: bool
    reason: Optional[str] = None


class WaiverPlan(BaseModel):
    team_key: str
    week: int
    options: List[WaiverOption]


class AutopilotRequest(BaseModel):
    """
    Request body for POST /teams/{team_key}/autopilot.

    For now this only considers bench_to_start actions and applies those whose
    projected gain meets or exceeds min_gain.
    """
    min_gain: float = 0.5


class AutopilotResult(BaseModel):
    team_key: str
    week: int
    applied: List[SuggestedAction]
    execution: Optional[List[dict]] = None


# ---------------------------------------------------------------------------
# Helper functions to map engine state -> API models
# ---------------------------------------------------------------------------

def _mk_player_view(p: dict, role: PlayerRole, slot: Optional[str] = None) -> PlayerView:
    """
    Safely convert an internal player dict into a PlayerView.
    """
    if p is None:
        # Should not happen, but guard anyway.
        return PlayerView(
            name="UNKNOWN",
            position="UNK",
            nfl_team=None,
            slot=slot,
            role=role,
            projection=0.0,
            status="UNKNOWN",
        )

    return PlayerView(
        name=p.get("name", "UNKNOWN"),
        position=p.get("position", "UNK"),
        nfl_team=p.get("nfl_team"),
        slot=slot,
        role=role,
        projection=float(p.get("projection", 0.0)),
        status=p.get("status", "UNKNOWN"),
    )


def _map_players_for_state(state: dict, team_key: str) -> LineupView:
    """
    Map run_lineup_for_team() output into LineupView for the UI.
    """
    starters: List[PlayerView] = []

    for s in state.get("current_starters", []):
        slot_name = s.get("slot_name")
        p = s.get("player")
        starters.append(
            _mk_player_view(p, role=PlayerRole.starter, slot=slot_name)
        )

    bench_players = [
        _mk_player_view(p, role=PlayerRole.bench)
        for p in state.get("bench", [])
    ]

    stash_players = [
        _mk_player_view(p, role=PlayerRole.stash)
        for p in state.get("stash", [])
    ]

    # total_projection in the LineupView is meant to represent the current
    # ESPN starters total, not the optimized total.
    total = float(state.get("base_total_projection", state.get("total_projection", 0.0)))

    return LineupView(
        team_key=team_key,
        week=CURRENT_WEEK,
        total_projection=total,
        starters=starters,
        bench=bench_players,
        stash=stash_players,
    )


def _actions_from_state(state: dict, team_key: str) -> TeamPlan:
    """
    Flatten bench swaps + FA upgrades into a unified list of SuggestedAction.
    """
    actions: List[SuggestedAction] = []
    week = CURRENT_WEEK

    # 1) Bench ↔ starter swaps (low risk)
    for swap in state.get("bench_swaps", []):
        bench_p = swap.get("bench_player")
        starter_p = swap.get("starter_player")
        gain = float(swap.get("gain", 0.0))

        if not bench_p or not starter_p:
            continue

        actions.append(
            SuggestedAction(
                id=f"{team_key}:bench_to_start:{bench_p.get('name','UNKNOWN')}",
                team_key=team_key,
                week=week,
                type=ActionType.bench_to_start,
                add_name=bench_p.get("name", "UNKNOWN"),
                add_position=bench_p.get("position", "UNK"),
                add_projection=float(bench_p.get("projection", 0.0)),
                drop_name=starter_p.get("name", "UNKNOWN"),
                drop_position=starter_p.get("position", "UNK"),
                drop_projection=float(starter_p.get("projection", 0.0)),
                gain=gain,
                can_add_now=True,
                reason=f"Bench {starter_p.get('name','UNKNOWN')} for "
                       f"{bench_p.get('name','UNKNOWN')} (+{gain:.1f} pts)",
            )
        )

    # 2) Free agents who would start
    for fa in state.get("fa_starter_upgrades", []):
        fa_p = fa.get("fa")
        bumped = fa.get("bumped")
        gain = float(fa.get("gain", 0.0))
        can_add_now = bool(fa.get("can_add_now", False))

        if not fa_p or not bumped:
            continue

        actions.append(
            SuggestedAction(
                id=f"{team_key}:fa_for_starter:{fa_p.get('name','UNKNOWN')}",
                team_key=team_key,
                week=week,
                type=ActionType.fa_for_starter,
                add_name=fa_p.get("name", "UNKNOWN"),
                add_position=fa_p.get("position", "UNK"),
                add_projection=float(fa_p.get("projection", 0.0)),
                drop_name=bumped.get("name", "UNKNOWN"),
                drop_position=bumped.get("position", "UNK"),
                drop_projection=float(bumped.get("projection", 0.0)),
                gain=gain,
                can_add_now=can_add_now,
                reason=f"Add {fa_p.get('name','UNKNOWN')} to start over "
                       f"{bumped.get('name','UNKNOWN')} (+{gain:.1f} pts)",
            )
        )

    # 3) Free agents who are better bench stashes
    for fa in state.get("fa_bench_upgrades", []):
        fa_p = fa.get("fa")
        drop = fa.get("drop")
        gain = float(fa.get("gain", 0.0))
        can_add_now = bool(fa.get("can_add_now", False))

        if not fa_p or not drop:
            continue

        actions.append(
            SuggestedAction(
                id=f"{team_key}:fa_for_bench:{fa_p.get('name','UNKNOWN')}",
                team_key=team_key,
                week=week,
                type=ActionType.fa_for_bench,
                add_name=fa_p.get("name", "UNKNOWN"),
                add_position=fa_p.get("position", "UNK"),
                add_projection=float(fa_p.get("projection", 0.0)),
                drop_name=drop.get("name", "UNKNOWN"),
                drop_position=drop.get("position", "UNK"),
                drop_projection=float(drop.get("projection", 0.0)),
                gain=gain,
                can_add_now=can_add_now,
                reason=f"Bench upgrade: {fa_p.get('name','UNKNOWN')} > "
                       f"{drop.get('name','UNKNOWN')} (+{gain:.1f} pts)",
            )
        )

    # Sort by biggest gain first
    actions.sort(key=lambda a: a.gain, reverse=True)

    return TeamPlan(
        team_key=team_key,
        week=week,
        total_projection_optimized=float(
            state.get("optimized_total_projection", 0.0)
        ),
        actions=actions,
    )


def _waiver_plan_from_state(state: dict, team_key: str) -> WaiverPlan:
    """
    Build a simple waiver "plan" from the engine state.

    For now we treat any FA → bench upgrade with can_add_now=False as a yellow
    waiver candidate. For each droppable bench player we keep the top few
    upgrade options ordered by gain.
    """
    week = CURRENT_WEEK
    raw_options = state.get("fa_bench_upgrades", [])

    by_drop: dict[str, List[WaiverOption]] = {}

    for fa in raw_options:
        fa_p = fa.get("fa")
        drop = fa.get("drop")
        gain = float(fa.get("gain", 0.0))
        can_add_now = bool(fa.get("can_add_now", False))

        # Only yellow (waiver) free agents for this view.
        if can_add_now:
            continue
        if not fa_p or not drop:
            continue

        option = WaiverOption(
            add_name=fa_p.get("name", "UNKNOWN"),
            add_position=fa_p.get("position", "UNK"),
            add_projection=float(fa_p.get("projection", 0.0)),
            drop_name=drop.get("name", "UNKNOWN"),
            drop_position=drop.get("position", "UNK"),
            drop_projection=float(drop.get("projection", 0.0)),
            gain=gain,
            can_add_now=can_add_now,
            reason=f"Waiver: {fa_p.get('name','UNKNOWN')} > "
                   f"{drop.get('name','UNKNOWN')} (+{gain:.1f} pts)",
        )

        key = option.drop_name
        bucket = by_drop.setdefault(key, [])
        bucket.append(option)

    # For each drop player, keep the top few options.
    MAX_PER_DROP = 3
    options: List[WaiverOption] = []
    for bucket in by_drop.values():
        bucket.sort(key=lambda o: o.gain, reverse=True)
        options.extend(bucket[:MAX_PER_DROP])

    # Sort overall by gain descending.
    options.sort(key=lambda o: o.gain, reverse=True)

    return WaiverPlan(team_key=team_key, week=week, options=options)


# ---------------------------------------------------------------------------
# FastAPI app + routes
# ---------------------------------------------------------------------------

app = FastAPI(title="LineupIQ API")

# Optional: allow local dev frontends (Next.js, iOS simulator via local proxy, etc.)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # you can restrict this later
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _on_startup():
    # Ensure the auth database / tables exist.
    init_db()


def _user_from_dict(row: dict) -> UserView:
    return UserView(id=int(row["id"]), email=row["email"])


def get_current_user(request: Request) -> UserView:
    token = request.cookies.get("lineupiq_session")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user_id = parse_session_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    row = get_user_by_id(user_id)
    if not row:
        raise HTTPException(status_code=401, detail="User not found")
    return _user_from_dict(row)


def _team_cfg_for_user(team_key: str, user: UserView) -> Dict[str, Any]:
    """
    Build a per-request team config for the given user.

    We start from the static TEAMS config for league_id / season_year / scoring,
    but inject this user's ESPN cookies so each user talks to their own ESPN
    account.
    """
    if team_key not in TEAMS:
        raise HTTPException(status_code=404, detail="Unknown team_key")

    base = dict(TEAMS[team_key])
    creds = get_espn_credentials(user.id)
    if not creds:
        raise HTTPException(
            status_code=400,
            detail="ESPN account not linked for this user. Call /auth/espn_link first.",
        )
    espn_s2, espn_swid = creds
    base["espn_s2"] = espn_s2
    base["espn_swid"] = espn_swid
    return base


@app.get("/")
def serve_ui_root():
    """
    Serve the main LineupIQ UI so you can hit the FastAPI root (/) directly
    instead of opening ui.html from the filesystem.

    Keeping the UI and API on the same origin makes future hosting and mobile
    wrappers simpler.
    """
    return FileResponse("ui.html")


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------


@app.post("/auth/register", response_model=UserView)
def register(req: RegisterRequest):
    existing = verify_user_credentials(req.email, req.password)
    # We don't want to leak whether the email exists; instead, try to create
    # and handle uniqueness errors gracefully.
    from sqlite3 import IntegrityError

    try:
        user_id = create_user(req.email, req.password)
    except IntegrityError:
        # Email already exists; for now return 400.
        raise HTTPException(status_code=400, detail="Email already registered")
    row = get_user_by_id(user_id)
    assert row is not None
    return _user_from_dict(row)


@app.post("/auth/login", response_model=UserView)
def login(req: LoginRequest, response: Response):
    user = verify_user_credentials(req.email, req.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_session_token(int(user["id"]))
    # HttpOnly cookie; in production you should also set secure=True and a
    # proper domain.
    response.set_cookie(
        key="lineupiq_session",
        value=token,
        httponly=True,
        samesite="lax",
    )
    return _user_from_dict(user)


@app.post("/auth/logout")
def logout(response: Response):
    response.delete_cookie("lineupiq_session")
    return {"status": "ok"}


@app.get("/auth/me", response_model=UserView)
def me(user: UserView = Depends(get_current_user)):
    return user


@app.post("/auth/espn_link")
def link_espn(req: ESPNLinkRequest, user: UserView = Depends(get_current_user)):
    """
    Store ESPN cookies (espn_s2, swid) for the current user.

    For now this expects the user to paste values manually; in a future
    iteration we can capture them via a browser login flow.
    """
    set_espn_credentials(user.id, req.espn_s2, req.espn_swid)
    return {"status": "ok"}


@app.get("/auth/espn_status", response_model=ESPNStatus)
def espn_status(user: UserView = Depends(get_current_user)):
    """
    Simple status endpoint so the UI can tell whether ESPN cookies are linked.
    """
    creds = get_espn_credentials(user.id)
    return ESPNStatus(linked=creds is not None)


@app.get("/me/teams", response_model=List[UserTeam])
def get_user_teams(user: UserView = Depends(get_current_user)):
    """
    Return all configured teams with an 'enabled' flag for this user.

    If the user has never configured teams, all TEAMS are treated as enabled.
    """
    managed = set(get_managed_team_keys(user.id))
    # If no explicit configuration, treat all as enabled.
    use_all = not managed

    teams: List[UserTeam] = []
    for key, cfg in TEAMS.items():
        enabled = use_all or key in managed
        teams.append(
            UserTeam(
                key=key,
                platform=cfg["platform"],
                league_id=cfg["league_id"],
                season_year=cfg["season_year"],
                team_name_keyword=cfg["team_name_keyword"],
                scoring=cfg["scoring"],
                enabled=enabled,
            )
        )
    return teams


@app.post("/me/teams", response_model=List[UserTeam])
def update_user_teams(
    req: UpdateUserTeamsRequest, user: UserView = Depends(get_current_user)
):
    """
    Set which TEAMS entries this user wants LineupIQ to manage.
    """
    # Filter to known team keys only.
    cleaned = [key for key in req.team_keys if key in TEAMS]
    set_managed_team_keys(user.id, cleaned)
    return get_user_teams(user)


@app.get("/health")
def health():
    return {"status": "ok", "week": CURRENT_WEEK}


@app.get("/teams")
def list_teams(user: UserView = Depends(get_current_user)):
    """
    List of all configured teams. Drives the 'all my teams' view.
    """
    managed = set(get_managed_team_keys(user.id))
    results = []
    for key, cfg in TEAMS.items():
        # If the user has explicitly configured teams, only show those.
        if managed and key not in managed:
            continue
        results.append(
            {
                "key": key,
                "platform": cfg["platform"],
                "league_id": cfg["league_id"],
                "season_year": cfg["season_year"],
                "team_name_keyword": cfg["team_name_keyword"],
                "scoring": cfg["scoring"],
            }
        )
    return results


@app.get("/teams/{team_key}/state", response_model=LineupView)
def get_team_state(team_key: str, user: UserView = Depends(get_current_user)):
    """
    Current starters / bench / stash for one team (with optimized total).
    """
    team_cfg = _team_cfg_for_user(team_key, user)
    state = run_lineup_for_team(team_key, team_cfg_override=team_cfg)
    return _map_players_for_state(state, team_key)


@app.get("/teams/{team_key}/plan", response_model=TeamPlan)
def get_team_plan(team_key: str, user: UserView = Depends(get_current_user)):
    """
    Canonical list of suggested actions for this team.
    """
    team_cfg = _team_cfg_for_user(team_key, user)
    state = run_lineup_for_team(team_key, team_cfg_override=team_cfg)
    return _actions_from_state(state, team_key)


@app.get("/teams/{team_key}/waiver_plan", response_model=WaiverPlan)
def get_waiver_plan(team_key: str, user: UserView = Depends(get_current_user)):
    """
    Waiver (yellow FA) recommendation plan for a team.

    This is intentionally read-only and only surfaces suggestions for
    can_add_now=False free agents; you still place the actual claims in ESPN.
    """
    team_cfg = _team_cfg_for_user(team_key, user)
    state = run_lineup_for_team(team_key, team_cfg_override=team_cfg)
    return _waiver_plan_from_state(state, team_key)


@app.post("/teams/{team_key}/actions/apply", response_model=ApplyActionsResult)
def apply_actions_dry_run(
    team_key: str,
    req: ApplyActionsRequest,
    mode: Optional[str] = None,
    user: UserView = Depends(get_current_user),
):
    """
    Dry-run endpoint for applying suggested actions.

    - Recomputes the latest plan for the team.
    - Resolves the requested action_ids against that plan.
    - Returns the matching SuggestedAction objects and any unknown IDs.

    IMPORTANT: This does *not* mutate your ESPN lineup yet. It's only here
    to wire up the UI flow and inspect what would be done.
    """
    team_cfg = _team_cfg_for_user(team_key, user)
    state = run_lineup_for_team(team_key, team_cfg_override=team_cfg)
    plan = _actions_from_state(state, team_key)

    by_id = {a.id: a for a in plan.actions}

    applied: List[SuggestedAction] = []
    unknown: List[str] = []

    for action_id in req.action_ids:
        action = by_id.get(action_id)
        if action is not None:
            applied.append(action)
        else:
            unknown.append(action_id)

    # Abstracted execution layer. We pass simple dicts so espn_actions stays
    # decoupled from FastAPI / Pydantic types.

    # Allow per-request override of execution mode via ?mode=dry_run|http.
    exec_mode = (mode or ACTION_MODE).lower()
    if exec_mode not in ("dry_run", "http"):
        exec_mode = ACTION_MODE

    execution = apply_actions_for_team(
        team_cfg=team_cfg,
        actions=[a.dict() for a in applied],
        mode=exec_mode,
    )

    return ApplyActionsResult(
        team_key=team_key,
        week=CURRENT_WEEK,
        applied=applied,
        unknown_action_ids=unknown,
        execution=execution,
    )


@app.post("/teams/{team_key}/autopilot", response_model=AutopilotResult)
def autopilot_swaps(
    team_key: str,
    req: AutopilotRequest,
    mode: Optional[str] = None,
    user: UserView = Depends(get_current_user),
):
    """
    Autopilot endpoint: apply a set of bench_to_start swaps for this team.

    - Recomputes the latest plan for the team.
    - Filters to bench_to_start actions with gain >= min_gain.
    - Applies those actions via espn_actions.apply_actions_for_team.
    """
    team_cfg = _team_cfg_for_user(team_key, user)
    state = run_lineup_for_team(team_key, team_cfg_override=team_cfg)
    plan = _actions_from_state(state, team_key)

    # Only Bench → Start actions, filtered by gain.
    candidates: List[SuggestedAction] = [
        a for a in plan.actions
        if a.type == ActionType.bench_to_start and a.gain >= req.min_gain
    ]

    if not candidates:
        return AutopilotResult(
            team_key=team_key,
            week=CURRENT_WEEK,
            applied=[],
            execution=[],
        )

    exec_mode = (mode or ACTION_MODE).lower()
    if exec_mode not in ("dry_run", "http"):
        exec_mode = ACTION_MODE

    execution = apply_actions_for_team(
        team_cfg=team_cfg,
        actions=[a.dict() for a in candidates],
        mode=exec_mode,
    )

    return AutopilotResult(
        team_key=team_key,
        week=CURRENT_WEEK,
        applied=candidates,
        execution=execution,
    )


@app.get("/actions/top", response_model=List[SuggestedAction])
def get_top_actions(limit: int = 20, user: UserView = Depends(get_current_user)):
    """
    Cross-team 'top actions this week' view.
    """
    all_actions: List[SuggestedAction] = []

    for team_key in TEAMS.keys():
        team_cfg = _team_cfg_for_user(team_key, user)
        state = run_lineup_for_team(team_key, team_cfg_override=team_cfg)
        plan = _actions_from_state(state, team_key)
        all_actions.extend(plan.actions)

    all_actions.sort(key=lambda a: a.gain, reverse=True)
    return all_actions[:limit]
