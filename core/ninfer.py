"""NInfer engine: ninfer-serve flag schema, artifact discovery and the shared manifest entry.

ninfer-serve serves exactly one .ninfer artifact per process and has no load/unload API,
so unlike the llama.cpp router it is simply started and stopped. While it runs, the
app advertises it under the "ninfer" key of local-llama.json, which is how other local
tools (an editor plugin, a workflow node) can find it.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Sequence

from . import router
from .flags import (KIND_BOOL, KIND_CHOICE, KIND_FLOAT, KIND_INT, KIND_PATH, KIND_TEXT, UNSET,
                    FlagSpec, split_extra)

CREATE_NO_WINDOW = 0x08000000
SERVER_EXE = "ninfer-serve.exe"
DEFAULT_DIR = ""  # nothing assumed: set on the NInfer page, or through NINFER_DIR
MAX_OUTPUT_TOKENS = 32768

GROUP_ORDER = ["Server", "Context & KV", "Speculative", "Multimodal", "Thinking", "Sampling", "Advanced"]

SPECS: list[FlagSpec] = [
    # ---- Server -------------------------------------------------------------
    FlagSpec("host", "--host", KIND_TEXT, "Host", "Server", default="127.0.0.1",
             placeholder="127.0.0.1", quick=True, always=True,
             help="Bind address. Use 0.0.0.0 to expose on the LAN."),
    FlagSpec("port", "--port", KIND_INT, "Port", "Server", default=8082,
             minimum=1, maximum=65535, quick=True, always=True,
             help="Kept off 8080 (the llama.cpp router) so NInfer and the router can run side "
                  "by side."),
    FlagSpec("model_id", "--model-id", KIND_TEXT, "Model id", "Server",
             placeholder="artifact default", quick=True,
             help="Name reported to API clients. Empty keeps the artifact's own id."),
    FlagSpec("api_key", "--api-key", KIND_TEXT, "API key", "Server",
             placeholder="leave empty for no auth"),
    FlagSpec("cors", "--cors", KIND_BOOL, "CORS", "Server", default=False),
    FlagSpec("webui", "--webui", KIND_BOOL, "Web UI", "Server", default=False,
             help="Downloads the llama.cpp web UI on first use and serves it at /."),
    FlagSpec("max_concurrency", "--max-concurrency", KIND_INT, "Max concurrency", "Server",
             minimum=0, maximum=256),
    FlagSpec("max_pending_requests", "--max-pending-requests", KIND_INT, "Max queued requests", "Server",
             minimum=0, maximum=100_000),
    FlagSpec("pending_timeout_ms", "--pending-timeout-ms", KIND_INT, "Queue timeout (ms)", "Server",
             minimum=0, maximum=2_000_000_000, step=1000),

    # ---- Context & KV -------------------------------------------------------
    FlagSpec("max_context", "--max-context", KIND_INT, "Context size", "Context & KV", default=32768,
             minimum=1024, maximum=1_000_000, step=1024, quick=True, always=True),
    FlagSpec("kv_dtype", "--kv-dtype", KIND_CHOICE, "KV cache type", "Context & KV", default=UNSET,
             choices=(UNSET, "bf16", "int8", "fp8", "nvfp4", "k8v4"), quick=True),
    FlagSpec("default_max_tokens", "--default-max-tokens", KIND_INT, "Default max tokens", "Context & KV",
             minimum=0, maximum=1_000_000, step=1024, quick=True,
             help="Output cap for requests that do not send max_tokens. Server default 8192."),
    FlagSpec("kv_capacity", "--kv-capacity", KIND_TEXT, "KV capacity", "Context & KV",
             placeholder="N or auto"),
    FlagSpec("prefill_chunk", "--prefill-chunk", KIND_INT, "Prefill chunk", "Context & KV",
             minimum=0, maximum=1_000_000),
    FlagSpec("no_prefix_reuse", "--no-prefix-reuse", KIND_BOOL, "Disable prefix reuse", "Context & KV",
             default=False),
    FlagSpec("host_kv_mib", "--host-kv-mib", KIND_INT, "Host KV (MiB)", "Context & KV",
             minimum=0, maximum=1_000_000, advanced=True),
    FlagSpec("device_state_slots", "--device-state-slots", KIND_INT, "Device state slots", "Context & KV",
             minimum=0, maximum=1024, advanced=True),
    FlagSpec("host_state_slots", "--host-state-slots", KIND_INT, "Host state slots", "Context & KV",
             minimum=0, maximum=1024, advanced=True),

    # ---- Speculative --------------------------------------------------------
    FlagSpec("spec", "--spec", KIND_CHOICE, "Speculative mode", "Speculative", default=UNSET,
             choices=(UNSET, "mtp", "dflash", "dflash2"), quick=True,
             help="dflash2 needs companion weights in the artifact (Qwen3.8-27B int / NVFP4)."),
    FlagSpec("draft_tokens", "--draft-tokens", KIND_INT, "Draft tokens", "Speculative",
             minimum=0, maximum=15, quick=True),
    FlagSpec("lm_head_draft", "--lm-head-draft", KIND_BOOL, "LM-head draft", "Speculative",
             default=False, quick=True),

    # ---- Multimodal ---------------------------------------------------------
    FlagSpec("vision", "--vision", KIND_BOOL, "Vision (images + video)", "Multimodal",
             default=False, quick=True),
    FlagSpec("no_cuda_graph", "--no-cuda-graph", KIND_BOOL, "Disable CUDA graphs", "Multimodal",
             default=False, quick=True,
             help="Needed above ~16k context with vision when VRAM is tight."),
    FlagSpec("max_request_mib", "--max-request-mib", KIND_INT, "Max request (MiB)", "Multimodal",
             minimum=0, maximum=65536, help="Server default 384. Inline video needs more."),
    FlagSpec("media_cache_mib", "--media-cache-mib", KIND_INT, "Media cache (MiB)", "Multimodal",
             default=-1, minimum=-1, maximum=65536, help="Server default 1024; 0 disables reuse."),
    FlagSpec("media_live_mib", "--media-live-mib", KIND_INT, "Media live (MiB)", "Multimodal",
             minimum=0, maximum=65536, help="Server default 2048."),
    FlagSpec("media_preprocess_threads", "--media-preprocess-threads", KIND_INT, "Media threads",
             "Multimodal", minimum=0, maximum=64, advanced=True),

    # ---- Thinking -----------------------------------------------------------
    FlagSpec("no_thinking", "--no-thinking", KIND_BOOL, "Disable thinking", "Thinking", default=False,
             quick=True),
    FlagSpec("preserve_thinking", "--preserve-thinking", KIND_BOOL, "Preserve thinking", "Thinking",
             default=False, help="Keeps closed-turn reasoning in later prompts."),
    FlagSpec("default_thinking_budget", "--default-thinking-budget", KIND_INT, "Thinking budget",
             "Thinking", minimum=0, maximum=1_000_000),

    # ---- Sampling -----------------------------------------------------------
    FlagSpec("temperature", "--temperature", KIND_FLOAT, "Temperature", "Sampling",
             minimum=0, maximum=5, step=0.05),
    FlagSpec("top_p", "--top-p", KIND_FLOAT, "Top-P", "Sampling", minimum=0, maximum=1, step=0.01),
    FlagSpec("top_k", "--top-k", KIND_INT, "Top-K", "Sampling", minimum=0, maximum=1000),
    FlagSpec("min_p", "--min-p", KIND_FLOAT, "Min-P", "Sampling", minimum=0, maximum=1, step=0.01),
    FlagSpec("presence_penalty", "--presence-penalty", KIND_FLOAT, "Presence penalty", "Sampling",
             minimum=0, maximum=2, step=0.05),
    FlagSpec("frequency_penalty", "--frequency-penalty", KIND_FLOAT, "Frequency penalty", "Sampling",
             minimum=0, maximum=2, step=0.05),
    FlagSpec("seed", "--seed", KIND_INT, "Seed", "Sampling", default=-1, minimum=-1,
             maximum=2_147_483_647),
    FlagSpec("greedy", "--greedy", KIND_BOOL, "Greedy", "Sampling", default=False),

    # ---- Advanced -----------------------------------------------------------
    FlagSpec("device", "--device", KIND_INT, "CUDA device", "Advanced", default=-1, minimum=-1,
             maximum=16),
    FlagSpec("log_level", "--log-level", KIND_CHOICE, "Log level", "Advanced", default=UNSET,
             choices=(UNSET, "trace", "debug", "info", "warning", "error", "critical", "off")),
    FlagSpec("log_stats_interval_ms", "--log-stats-interval-ms", KIND_INT, "Stats interval (ms)",
             "Advanced", default=-1, minimum=-1, maximum=3_600_000, step=1000),
    FlagSpec("request_log_jsonl", "--request-log-jsonl", KIND_PATH, "Request log (JSONL)", "Advanced",
             file_filter="JSON lines (*.jsonl);;All files (*.*)"),
    FlagSpec("context_cost_presets", "--context-cost-presets", KIND_PATH, "Context cost presets",
             "Advanced", file_filter="All files (*.*)"),
]

SPECS_BY_KEY = {s.key: s for s in SPECS}

# Starting point for an artifact with no saved settings: a 27B-class model on a 32 GB GPU,
# bound to localhost and on port 8082, beside the router rather than on top of it.
RECOMMENDED: dict[str, Any] = {
    "max_context": 150000, "default_max_tokens": 150000, "kv_dtype": "int8",
    "spec": "mtp", "draft_tokens": 3, "lm_head_draft": True, "vision": True,
    "cors": True, "preserve_thinking": True,
    "max_pending_requests": 50, "pending_timeout_ms": 3_000_000,
}


def defaults() -> dict[str, Any]:
    values = {s.key: s.default for s in SPECS if s.default is not None}
    values.update(RECOMMENDED)
    return values


def exe_path(folder: str | Path) -> Path:
    return Path(folder) / SERVER_EXE


def artifacts(folder: str | Path) -> list[Path]:
    """.ninfer files in the install's models folder (and its root, for hand-placed ones)."""
    if not str(folder).strip():
        return []  # no folder chosen yet; "" would otherwise mean the working directory
    base = Path(folder)
    found: list[Path] = []
    for where in (base / "models", base):
        try:
            found.extend(sorted(p for p in where.glob("*.ninfer") if p.is_file()))
        except OSError:
            pass
    return found


def build_argv(exe: str | Path, artifact: str | Path, values: dict[str, Any], extra: str = "",
               specs: Sequence[FlagSpec] = SPECS) -> list[str]:
    argv = [str(exe), str(artifact)]
    for spec in specs:
        if spec.key not in values:
            continue
        # --draft-tokens / --lm-head-draft only mean something alongside --spec.
        if spec.key in ("draft_tokens", "lm_head_draft") and not values.get("spec"):
            continue
        argv.extend(spec.emit(values[spec.key]))
    if extra.strip():
        argv.extend(split_extra(extra))
    return argv


def supported_flags(exe: str | Path, timeout: int = 60) -> set[str]:
    """Flags from ninfer-serve's usage line. Slow - never call on the UI thread."""
    try:
        proc = subprocess.run([str(exe), "--help"], capture_output=True, text=True,
                              timeout=timeout, creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return set()
    return set(re.findall(r"--[a-z][a-z0-9-]*", (proc.stdout or "") + (proc.stderr or "")))


def base_url(values: dict[str, Any]) -> str:
    host = str(values.get("host") or "127.0.0.1")
    host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    return f"http://{host}:{int(values.get('port') or 8082)}"


def health(url: str, timeout: float = 0.4) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=timeout) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def served_model_id(values: dict[str, Any], timeout: float = 3.0) -> str:
    """The id the running server answers to (its --model-id, or the artifact's own)."""
    request = urllib.request.Request(f"{base_url(values)}/v1/models")
    if values.get("api_key"):
        request.add_header("Authorization", f"Bearer {values['api_key']}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        return str((data.get("data") or [{}])[0].get("id") or "")
    except (urllib.error.URLError, OSError, ValueError, IndexError):
        return ""


# ------------------------------------------------------------------ manifest entry
def _update_manifest(mutate) -> None:
    path = router.manifest_path()
    # Held across the read and the write: router.publish rewrites the same file from its
    # own worker thread, and both engines can be publishing at once.
    with router.MANIFEST_LOCK:
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {"version": 1, "models": []}
        before = json.dumps(manifest, sort_keys=True)
        mutate(manifest)
        if json.dumps(manifest, sort_keys=True) != before:
            manifest["generated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            router.write_if_changed(path, json.dumps(manifest, indent=2))


def publish_running(values: dict[str, Any], artifact: str | Path, pid: int) -> str:
    """Advertise the running server. Returns the model id it answers to."""
    mid = served_model_id(values) or str(values.get("model_id") or "") or Path(artifact).stem
    context = int(values.get("max_context") or 0)
    output = int(values.get("default_max_tokens") or 8192)
    entry = {
        "id": mid,
        "name": f"{mid} (NInfer)",
        "baseURL": f"{base_url(values)}/v1",
        "apiKey": str(values.get("api_key") or ""),
        "artifact": str(artifact),
        "pid": pid,
        "context": context,
        "output": min(MAX_OUTPUT_TOKENS, output, context // 2 if context else output),
        "modalities": ["text", "image", "video"] if values.get("vision") else ["text"],
        "reasoning": not values.get("no_thinking"),
        "tool_call": True,
    }
    _update_manifest(lambda m: m.__setitem__("ninfer", entry))
    return mid


def clear_running() -> None:
    _update_manifest(lambda m: m.pop("ninfer", None))


def env_dir() -> str:
    return os.environ.get("NINFER_DIR", DEFAULT_DIR)
