# models.py

from dataclasses import dataclass
from typing import List, Optional

@dataclass
class Player:
    name: str
    position: str            # QB, RB, WR, TE, DST, K
    projection: float        # this week's projected points
    status: str = "ACTIVE"   # ACTIVE, Q, O, BYE, IR, D
    is_starter: bool = False # your current ESPN lineup flag

@dataclass
class Slot:
    name: str
    eligible_positions: List[str]

@dataclass
class Assignment:
    slot: Slot
    player: Optional[Player]

@dataclass
class FreeAgent:
    name: str
    position: str
    projection: float
    can_add_now: bool        # True = green plus; False = waiver (yellow +)
    status: str = "ACTIVE"   # ACTIVE, BYE, O, etc.
