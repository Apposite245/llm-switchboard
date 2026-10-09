"""Strata engine: launches Strata's own serve/server.py with a config its setup wrote.

Strata is a separate MoE engine (strata.exe) behind a Python front-end. Its setup writes one
strata-<model>.json per installed model, holding the engine flags; Switchboard only picks a
config and a port and runs the same command the setup's run-*.bat does. Strata's settings
stay Strata's: change them with its own tools.

It needs nearly the whole GPU and most of system RAM, so the window asks before starting it
beside llama.cpp or NInfer.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_DIR = ""  # nothing assumed: set on the Strata page, or through STRATA_DIR
DEFAULT_PORT = 8097  # the setup's run-*.bat port; clear of the router (8080) and NInfer (8082)
SERVER_SCRIPT = Path("serve") / "server.py"
VENV_PYTHON = Path(".venv") / "Scripts" / "python.exe"


def env_dir() -> str:
    return os.environ.get("STRATA_DIR", DEFAULT_DIR)


def python_path(folder: str | Path) -> Path:
    return Path(folder) / VENV_PYTHON


def server_path(folder: str | Path) -> Path:
    return Path(folder) / SERVER_SCRIPT


def configs(folder: str | Path) -> list[Path]:
    if not str(folder).strip():
        return []  # no folder chosen yet; "" would otherwise mean the working directory
    try:
        return sorted(p for p in Path(folder).glob("strata-*.json") if p.is_file())
    except OSError:
        return []


def read_config(path: str | Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))  # Notepad adds a BOM
    except (OSError, ValueError):
        return {}


def engine_arg(cfg: dict, flag: str) -> str:
    args = [str(a) for a in cfg.get("args") or []]
    return args[args.index(flag) + 1] if flag in args[:-1] else ""


def build_argv(folder: str | Path, config: str | Path, port: int) -> list[str]:
    # -u: unbuffered, so the console shows load progress as it happens. -X utf8: a piped
    # stdout on Windows is otherwise cp1252, and the server prints characters it cannot encode.
    return [str(python_path(folder)), "-X", "utf8", "-u", str(server_path(folder)),
            "--engine", "strata", "--config", str(config), "--port", str(int(port))]


def base_url(config: str | Path | None, port: int) -> str:
    host = str(read_config(config).get("host") or "127.0.0.1") if config else "127.0.0.1"
    host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    return f"http://{host}:{int(port)}"


def served_model_id(url: str, timeout: float = 3.0) -> str:
    try:
        with urllib.request.urlopen(f"{url}/v1/models", timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        return str((data.get("data") or [{}])[0].get("id") or "")
    except (urllib.error.URLError, OSError, ValueError, IndexError):
        return ""
