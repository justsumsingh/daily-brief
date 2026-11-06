"""
Shared helpers: private config + encrypted state.

Nothing location-specific lives in this public repo. Your towns, keywords and
sources come from one of:
  1. the BRIEF_CONFIG environment variable (a GitHub Secret holding config.json's text), or
  2. a local config.json file (git-ignored, for testing on your own computer).

Saved state (seen items, 911 calls) is committed only as encrypted *.enc files,
using the STATE_KEY secret. Without the key they're unreadable noise.
"""
import json
import os
import sys

from cryptography.fernet import Fernet, InvalidToken

IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"


def load_config() -> dict:
    raw = os.environ.get("BRIEF_CONFIG", "").strip()
    if not raw and os.path.exists("config.json"):
        with open("config.json", encoding="utf-8") as f:
            raw = f.read()
    if not raw:
        sys.exit("No config found. Add the BRIEF_CONFIG secret (or a local config.json).")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        sys.exit("BRIEF_CONFIG is not valid JSON — paste the whole config.json file into the secret.")


def _fernet():
    key = os.environ.get("STATE_KEY", "").strip()
    if not key:
        if IN_ACTIONS:
            sys.exit("Missing STATE_KEY secret.")
        return None  # local testing: plain JSON files
    return Fernet(key.encode())


def load_state(name: str, default: dict) -> dict:
    f = _fernet()
    path = f"{name}.enc" if f else name
    if not os.path.exists(path):
        return default
    with open(path, "rb") as fh:
        data = fh.read()
    try:
        return json.loads(f.decrypt(data) if f else data)
    except (InvalidToken, json.JSONDecodeError):
        sys.exit(f"Could not read {path} — STATE_KEY may have changed. Delete the file to start fresh.")


def save_state(name: str, data: dict):
    f = _fernet()
    blob = json.dumps(data, sort_keys=True).encode()
    if f:
        with open(f"{name}.enc", "wb") as fh:
            fh.write(f.encrypt(blob))
    else:
        with open(name, "wb") as fh:
            fh.write(blob)


def log(public: str, private: str = ""):
    """Public GitHub logs get only the generic message; local runs get the detail too."""
    print(public if IN_ACTIONS or not private else f"{public} {private}")
