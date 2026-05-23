"""
session_store.py — Save and load game sessions to/from disk.

The TF-IDF vectorizer in game.py is not JSON-serializable.
We save raw_story text instead and rebuild the index on load.
"""

import json
import os
from pathlib import Path
from datetime import datetime

SAVES_DIR = Path("saves")
SAVES_DIR.mkdir(exist_ok=True)

# Keys that cannot be JSON-serialized — skip them when saving
NON_SERIALIZABLE_KEYS = {"world_state"}  # world_state may contain arbitrary objects


def save_session(session_id: str, session_data: dict) -> bool:
    """Serialize session to saves/{session_id}.json. Returns True on success."""
    try:
        serializable = {}
        for k, v in session_data.items():
            if k in NON_SERIALIZABLE_KEYS:
                continue
            try:
                json.dumps(v)  # test if serializable
                serializable[k] = v
            except (TypeError, ValueError):
                print(f"  [save] Skipping non-serializable key: {k}")

        serializable["saved_at"] = datetime.utcnow().isoformat()

        path = SAVES_DIR / f"{session_id}.json"
        path.write_text(json.dumps(serializable, indent=2, ensure_ascii=False))
        print(f"  [save] Saved session {session_id[:8]} → {path}")
        return True
    except Exception as e:
        print(f"  [save] ERROR saving session {session_id[:8]}: {e}")
        return False


def load_session(session_id: str) -> dict | None:
    """Load session from saves/{session_id}.json. Returns dict or None."""
    try:
        path = SAVES_DIR / f"{session_id}.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        print(f"  [save] Loaded session {session_id[:8]}")
        return data
    except Exception as e:
        print(f"  [save] ERROR loading session {session_id[:8]}: {e}")
        return None


def list_saves() -> list[dict]:
    """Return list of save metadata dicts, sorted newest first."""
    saves = []
    for f in SAVES_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            char = data.get("character", {})
            saves.append({
                "session_id":     f.stem,
                "character_name": char.get("name", "Unknown"),
                "turn_count":     data.get("turn_count", 0),
                "saved_at":       data.get("saved_at", ""),
                "difficulty":     data.get("difficulty", "normal"),
                "alignment":      data.get("alignment", 0),
            })
        except Exception:
            continue
    return sorted(saves, key=lambda x: x["saved_at"], reverse=True)


def delete_save(session_id: str) -> bool:
    """Delete a save file. Returns True on success."""
    try:
        path = SAVES_DIR / f"{session_id}.json"
        if path.exists():
            path.unlink()
        return True
    except Exception as e:
        print(f"  [save] ERROR deleting save {session_id[:8]}: {e}")
        return False
