"""
contacts.py — local contact book
Stores a mapping of RNS hash (32 char hex) → user-assigned friendly name.
All RNS operations always use the raw hash. This module is purely UI-layer.

Storage: JSON file at /data/data/com.example.rnshello/files/contacts.json
Format:  {"9e49687e6cf4c6362056f9cbde6820fe": "Alice", ...}
"""

import json
import os
import threading

_CONTACTS_PATH = "/data/data/com.example.rnshello/files/contacts.json"
_lock = threading.Lock()
_contacts: dict = {}   # hash -> name, in-memory cache

def _load():
    """Load contacts from disk into cache. Called once at module import."""
    global _contacts
    try:
        if os.path.exists(_CONTACTS_PATH):
            with open(_CONTACTS_PATH, "r") as f:
                _contacts = json.load(f)
    except Exception as e:
        print(f"contacts: load error {e}")
        _contacts = {}

def _save():
    """Persist current cache to disk. Must be called under _lock."""
    try:
        os.makedirs(os.path.dirname(_CONTACTS_PATH), exist_ok=True)
        with open(_CONTACTS_PATH, "w") as f:
            json.dump(_contacts, f, indent=2)
    except Exception as e:
        print(f"contacts: save error {e}")

def save(hash_hex: str, name: str):
    """Save or update a contact. hash_hex is plain 32-char hex."""
    hash_hex = hash_hex.strip().strip("<>")
    name = name.strip()
    with _lock:
        _contacts[hash_hex] = name
        _save()

def delete(hash_hex: str):
    """Remove a contact by hash."""
    hash_hex = hash_hex.strip().strip("<>")
    with _lock:
        _contacts.pop(hash_hex, None)
        _save()

def get_all() -> list:
    """Return all contacts as a list of dicts: [{hash, name}, ...]"""
    with _lock:
        return [{"hash": h, "name": n} for h, n in _contacts.items()]

def resolve(hash_hex: str, fallback: str = "") -> str:
    """
    Resolve a hash to a friendly name.
    Returns the saved contact name if found, else fallback, else truncated hash.
    RNS layer never calls this — only the UI layer does.
    """
    hash_hex = hash_hex.strip().strip("<>")
    with _lock:
        if hash_hex in _contacts:
            return _contacts[hash_hex]
    if fallback:
        return fallback
    # Last resort: show first 8 + last 4 chars so it's recognisable but short
    if len(hash_hex) >= 12:
        return f"{hash_hex[:8]}…{hash_hex[-4:]}"
    return hash_hex

# Load on import
_load()
