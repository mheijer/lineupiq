from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional, Tuple, Dict, Any
import hashlib
import os
import secrets
from datetime import datetime

# Allow the auth DB path to be overridden for hosted environments (e.g. Render
# persistent disk). Locally we still default to ./data/lineupiq_auth.db.
DB_PATH = Path(os.environ.get("LINEUPIQ_DB_PATH", "data/lineupiq_auth.db"))


def _get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = _get_conn()
    cur = conn.cursor()

    # Basic user table: one ESPN account per LineupIQ user for now.
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )

    # ESPN credentials per user, stored encrypted at rest.
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS espn_credentials (
            user_id INTEGER PRIMARY KEY,
            espn_s2 TEXT NOT NULL,
            espn_swid TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )

    # Managed teams per user: which config.TEAMS entries this user cares about.
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS managed_teams (
            user_id INTEGER NOT NULL,
            team_key TEXT NOT NULL,
            PRIMARY KEY (user_id, team_key),
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )

    conn.commit()
    conn.close()


def _hash_password(password: str) -> str:
    """Hash a password using PBKDF2-HMAC-SHA256 with a random salt."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return salt.hex() + ":" + dk.hex()


def _verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt_hex, hash_hex = stored_hash.split(":", 1)
    except ValueError:
        return False
    salt = bytes.fromhex(salt_hex)
    expected = bytes.fromhex(hash_hex)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return secrets.compare_digest(dk, expected)


def create_user(email: str, password: str) -> int:
    conn = _get_conn()
    cur = conn.cursor()
    now = datetime.utcnow().isoformat()
    pw_hash = _hash_password(password)
    cur.execute(
        "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
        (email.lower(), pw_hash, now),
    )
    conn.commit()
    user_id = cur.lastrowid
    conn.close()
    return int(user_id)


def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE email = ?", (email.lower(),))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def verify_user_credentials(email: str, password: str) -> Optional[Dict[str, Any]]:
    user = get_user_by_email(email)
    if not user:
        return None
    if not _verify_password(password, user["password_hash"]):
        return None
    return user


def _get_crypto_key() -> Optional[bytes]:
    """
    Load the symmetric key used to encrypt ESPN cookies.

    For real deployments you should set LINEUPIQ_ESPN_KEY to a random 32-byte
    value (in hex). If it's missing, we fall back to storing credentials
    in plain text, which is fine for local dev but not recommended for
    shared hosting.
    """
    key_hex = os.environ.get("LINEUPIQ_ESPN_KEY")
    if not key_hex:
        return None
    try:
        return bytes.fromhex(key_hex)
    except ValueError:
        return None


def _encrypt(value: str) -> str:
    key = _get_crypto_key()
    data = value.encode("utf-8")
    if not key:
        # Dev fallback: store raw.
        return data.hex()
    # Simple XOR with key for now; callers should still rely on OS-level
    # protection for the DB file. In the future we can swap this for a
    # stronger scheme (e.g. Fernet) without changing callers.
    enc = bytes(b ^ key[i % len(key)] for i, b in enumerate(data))
    return enc.hex()


def _decrypt(value_hex: str) -> str:
    key = _get_crypto_key()
    data = bytes.fromhex(value_hex)
    if not key:
        return data.decode("utf-8")
    dec = bytes(b ^ key[i % len(key)] for i, b in enumerate(data))
    return dec.decode("utf-8")


def set_espn_credentials(user_id: int, espn_s2: str, espn_swid: str) -> None:
    conn = _get_conn()
    cur = conn.cursor()
    now = datetime.utcnow().isoformat()
    enc_s2 = _encrypt(espn_s2)
    enc_swid = _encrypt(espn_swid)
    cur.execute(
        """
        INSERT INTO espn_credentials (user_id, espn_s2, espn_swid, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            espn_s2=excluded.espn_s2,
            espn_swid=excluded.espn_swid,
            updated_at=excluded.updated_at
        """,
        (user_id, enc_s2, enc_swid, now),
    )
    conn.commit()
    conn.close()


def get_espn_credentials(user_id: int) -> Optional[Tuple[str, str]]:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT espn_s2, espn_swid FROM espn_credentials WHERE user_id = ?",
        (user_id,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return _decrypt(row["espn_s2"]), _decrypt(row["espn_swid"])


def get_managed_team_keys(user_id: int) -> list[str]:
    """
    Return the list of team_keys this user has explicitly enabled.

    If the user has never configured teams, this will return an empty list;
    callers can decide whether to treat that as "all TEAMS" or "none".
    """
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT team_key FROM managed_teams WHERE user_id = ? ORDER BY team_key",
        (user_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return [r["team_key"] for r in rows]


def set_managed_team_keys(user_id: int, team_keys: list[str]) -> None:
    """
    Replace the set of managed team_keys for this user with the given list.
    """
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM managed_teams WHERE user_id = ?", (user_id,))
    for key in team_keys:
        cur.execute(
            "INSERT INTO managed_teams (user_id, team_key) VALUES (?, ?)",
            (user_id, key),
        )
    conn.commit()
    conn.close()


