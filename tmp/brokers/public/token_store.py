from __future__ import annotations
import os
import json
from typing import Optional, Dict


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SESSION_FILE = os.path.join(PROJECT_ROOT, "config", "session.json")


def read_session() -> Optional[Dict]:
    if not os.path.exists(SESSION_FILE):
        return None
    try:
        with open(SESSION_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return None


def write_session(data: Dict) -> None:
    os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
    with open(SESSION_FILE, "w") as f:
        json.dump(data, f, indent=4)
