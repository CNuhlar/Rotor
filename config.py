"""Tiny JSON config persisted per-user under %APPDATA%\\Rotor\\config.json.

We store the selected devices by NAME (not index): indices shift when devices
come and go or across reboots, but a name substring resolves reliably.
"""

import json
import os

APP_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "Rotor")
CONFIG_PATH = os.path.join(APP_DIR, "config.json")


def load():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save(cfg):
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass
