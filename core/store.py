"""Settings persistence: scan roots, chosen binary, and per-model flag overrides."""
from __future__ import annotations

import copy
import json
import os
import time
from pathlib import Path
from typing import Any, Callable

APP_NAME = "llm-switchboard"
# What the app was called before it was renamed. Settings made then stay in that folder:
# other tools may already read the manifest from there.
LEGACY_APP_NAME = "llamacpp-manager"

DEFAULTS: dict[str, Any] = {
    "model_roots": [],
    "extra_models": [],
    "binary_dirs": [],
    "binary": "",
    "global_flags": {},
    "per_model": {},
    "extra_args": "",
    "window": {},
}


def config_dir() -> Path:
    base = Path(os.environ.get("APPDATA") or str(Path.home()))
    path = base / APP_NAME
    legacy = base / LEGACY_APP_NAME
    if not path.exists() and legacy.is_dir():
        return legacy
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return config_dir() / "settings.json"


class Store:
    def __init__(self, path: Path | None = None):
        self.path = path or config_path()
        self.data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        # Set when the settings file existed but could not be used; the window shows it once.
        self.load_error = ""
        # Called with a message when a save fails, so the window can say so.
        self.on_save_error: Callable[[str], None] | None = None
        self.load()

    def load(self) -> None:
        try:
            text = self.path.read_text(encoding="utf-8-sig")  # tolerate a BOM from other editors
        except FileNotFoundError:
            return  # first run
        except OSError as error:
            self.load_error = f"Could not read {self.path}: {error}. Starting with default settings."
            return
        try:
            raw = json.loads(text)
            if not isinstance(raw, dict):
                raise ValueError("the file does not hold a JSON object")
        except ValueError as error:
            # The next save would overwrite the file, so keep the unreadable one beside it.
            backup = self.path.with_name(f"{self.path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
            try:
                self.path.replace(backup)
                kept = f"The unreadable file was kept as {backup.name}."
            except OSError:
                kept = "It could not be backed up and will be overwritten."
            self.load_error = (f"{self.path} is damaged ({error}). Starting with default settings. "
                               f"{kept}")
            return
        merged = copy.deepcopy(DEFAULTS)
        merged.update(raw)
        self.data = merged

    def save(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as error:
            if self.on_save_error:
                self.on_save_error(f"Settings could not be saved to {self.path}: {error}")

    # ------------------------------------------------------------- accessors
    def get(self, key: str, fallback: Any = None) -> Any:
        return self.data.get(key, DEFAULTS.get(key, fallback))

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.save()

    def model_profile(self, model_key: str) -> dict:
        return self.data.get("per_model", {}).get(model_key, {})

    def save_model_profile(self, model_key: str, profile: dict) -> None:
        self.data.setdefault("per_model", {})[model_key] = profile
        self.save()

    def add_unique(self, key: str, value: str) -> bool:
        items = list(self.get(key) or [])
        if any(str(i).lower() == str(value).lower() for i in items):
            return False
        items.append(value)
        self.set(key, items)
        return True

    def remove_item(self, key: str, value: str) -> None:
        items = [i for i in (self.get(key) or []) if str(i).lower() != str(value).lower()]
        self.set(key, items)
