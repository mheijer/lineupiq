from __future__ import annotations

import hmac
import os
import secrets
from hashlib import sha256
from typing import Optional, Tuple
from datetime import datetime, timedelta, timezone


SESSION_SECRET = os.environ.get("LINEUPIQ_SESSION_SECRET", "dev-session-secret-change-me").encode(
    "utf-8"
)
SESSION_TTL_DAYS = 30


def create_session_token(user_id: int) -> str:
    """
    Create a signed session token for the given user.

    Format: user_id:timestamp:nonce:signature
    signature = HMAC-SHA256(SESSION_SECRET, f\"user_id:timestamp:nonce\")
    """
    ts = int(datetime.now(tz=timezone.utc).timestamp())
    nonce = secrets.token_hex(16)
    base = f"{user_id}:{ts}:{nonce}"
    sig = hmac.new(SESSION_SECRET, base.encode("utf-8"), sha256).hexdigest()
    return f"{base}:{sig}"


def parse_session_token(token: str) -> Optional[int]:
    """
    Validate a session token and return the user_id if valid, else None.
    """
    try:
        user_str, ts_str, nonce, sig = token.split(":", 4)
        base = f"{user_str}:{ts_str}:{nonce}"
    except ValueError:
        return None

    expected_sig = hmac.new(SESSION_SECRET, base.encode("utf-8"), sha256).hexdigest()
    if not hmac.compare_digest(expected_sig, sig):
        return None

    try:
        user_id = int(user_str)
        ts = int(ts_str)
    except ValueError:
        return None

    # Expiry check
    created_at = datetime.fromtimestamp(ts, tz=timezone.utc)
    if created_at < datetime.now(tz=timezone.utc) - timedelta(days=SESSION_TTL_DAYS):
        return None

    return user_id


