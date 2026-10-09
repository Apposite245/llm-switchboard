"""Localhost control API: lets local tools drive Switchboard's engines through Switchboard.

Why this exists: Switchboard builds each engine's command line from its saved per-model and
per-artifact settings and owns the child process. A tool that launches or kills
ninfer-serve.exe itself has to duplicate those settings and leaves Switchboard's view of the
process stale. Instead, tools ask Switchboard, which stays the only thing that starts servers.

The main client is an MCP server that frees the GPU for ComfyUI: it calls /suspend before a
generation and /resume after it. See CONTROL_API.md for the contract.

Threading: HTTP requests run on server threads. Anything touching Qt objects (QProcess,
widgets) is handed to the GUI thread through ControlBridge and returns immediately; all
waiting - for a process to exit, for VRAM to drain, for a server to become ready - happens
on the HTTP thread, polling state that is safe to read from any thread. Nothing on this path
ever opens a dialog: a modal box raised by an API call would freeze the app unattended.

Binds 127.0.0.1 only, with no authentication, matching the local inference endpoints.
Requests that carry an Origin header (a web page) or an unexpected Host are refused.
"""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Protocol

from PySide6.QtCore import QObject, Qt, Signal, Slot

from . import runtime

DEFAULT_PORT = 8090
ENGINES = ("ninfer", "llama", "strata")
# Engines that cannot run at the same time: Strata takes nearly the whole GPU and most of RAM.
# The window asks before such a start; the API refuses it.
CONFLICTS = {"strata": ("llama", "ninfer"), "llama": ("strata",), "ninfer": ("strata",)}

STOP_TIMEOUT_S = 30
READY_TIMEOUT_S = 300
# Strata loads its experts into pinned RAM and sizes a GPU expert cache before it serves;
# a first start takes several minutes.
STRATA_READY_TIMEOUT_S = 900
VRAM_SETTLE_S = 15
DEFAULT_VRAM_WAIT_S = 60
DEFAULT_MARGIN_MB = 512
MAX_BODY_BYTES = 64 * 1024


class Target(Protocol):
    """What Switchboard window provides. Methods marked GUI run on the GUI thread."""

    def engine_state(self, name: str) -> str: ...               # any thread
    def engine_info(self, name: str) -> dict: ...               # GUI
    def engine_api_key(self, name: str) -> str: ...             # GUI; "" when none is set
    def start_engine(self, name: str, artifact: str | None) -> str | None: ...  # GUI; error text
    def stop_engine(self, name: str) -> None: ...               # GUI
    def log_engine(self, name: str, text: str) -> None: ...     # GUI


class ApiError(Exception):
    def __init__(self, status: int, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.extra = extra


# ------------------------------------------------------------------ GUI-thread bridge
class ControlBridge(QObject):
    """Runs a callable on the GUI thread and hands its result back to the calling thread."""

    _call = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        # Explicit queued connection: the signal is emitted from plain Python threads.
        self._call.connect(self._run, Qt.ConnectionType.QueuedConnection)

    def invoke(self, fn: Callable[[], Any], timeout: float = STOP_TIMEOUT_S) -> Any:
        if threading.current_thread() is threading.main_thread():
            return fn()  # already on the GUI thread; queuing would deadlock
        box: dict[str, Any] = {"done": threading.Event(), "result": None, "error": None}
        self._call.emit((fn, box))
        if not box["done"].wait(timeout):
            raise ApiError(504, "Switchboard did not respond (GUI thread busy)")
        if box["error"] is not None:
            raise box["error"]
        return box["result"]

    @Slot(object)
    def _run(self, item) -> None:
        fn, box = item
        try:
            box["result"] = fn()
        except Exception as error:  # noqa: BLE001 - handed back to the HTTP thread
            box["error"] = error
        finally:
            box["done"].set()


# ------------------------------------------------------------------ helpers
def _gpu_used_mb() -> int | None:
    gpus = runtime.gpu_info()
    return gpus[0].used_mb if gpus else None


def _gpu_free_mb() -> int | None:
    gpus = runtime.gpu_info()
    return gpus[0].free_mb if gpus else None


def settled_used_mb(timeout: float = VRAM_SETTLE_S) -> int | None:
    """GPU memory in use once it has stopped falling after a process exit."""
    deadline = time.monotonic() + timeout
    last = _gpu_used_mb()
    while last is not None and time.monotonic() < deadline:
        time.sleep(0.5)
        now = _gpu_used_mb()
        if now is None:
            return last
        if now >= last - 16:  # no meaningful drop since the last read
            return now
        last = now
    return last


def _served_model_id(url: str, api_key: str = "") -> str:
    request = urllib.request.Request(f"{url}/v1/models")
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            data = json.loads(response.read().decode("utf-8"))
        return str((data.get("data") or [{}])[0].get("id") or "")
    except (urllib.error.URLError, OSError, ValueError, IndexError):
        return ""


# ------------------------------------------------------------------ operations
class Controller:
    """The operations behind the endpoints. Runs on HTTP threads; one mutation at a time."""

    def __init__(self, target: Target, bridge: ControlBridge):
        self.target = target
        self.bridge = bridge
        self._lock = threading.Lock()
        # engine -> {"artifact": str | None, "footprint_mb": int | None}
        self.suspended: dict[str, dict] = {}

    # -------------------------------------------------------------- queries
    def status(self) -> dict:
        info = self.bridge.invoke(lambda: {n: self.target.engine_info(n) for n in ENGINES})
        gpus = runtime.gpu_info()
        return {
            "engines": info,
            "gpu": ({"name": gpus[0].name, "total_mb": gpus[0].total_mb,
                     "used_mb": gpus[0].used_mb, "free_mb": gpus[0].free_mb} if gpus else None),
            "suspended": dict(self.suspended),
        }

    def _running(self, name: str) -> bool:
        return self.target.engine_state(name) != runtime.STATE_STOPPED

    def _log(self, name: str, text: str) -> None:
        self.bridge.invoke(lambda: self.target.log_engine(name, f"\n[switchboard] {text}\n"))

    # -------------------------------------------------------------- stop / start
    def _stop_and_wait(self, name: str) -> int | None:
        """Stop an engine and return the VRAM it released, in MB (None if unmeasurable)."""
        before = _gpu_used_mb()
        self.bridge.invoke(lambda: self.target.stop_engine(name))
        deadline = time.monotonic() + STOP_TIMEOUT_S
        while self._running(name):
            if time.monotonic() > deadline:
                raise ApiError(504, f"{name} did not stop within {STOP_TIMEOUT_S} s")
            time.sleep(0.2)
        after = settled_used_mb()
        if before is None or after is None:
            return None
        return max(0, before - after)

    def _wait_ready(self, name: str, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            state = self.target.engine_state(name)
            if state == runtime.STATE_STOPPED:
                raise ApiError(500, f"{name} exited during startup - see its console tab")
            if state == runtime.STATE_READY:
                break
            if time.monotonic() > deadline:
                raise ApiError(504, f"{name} not ready after {int(timeout)} s")
            time.sleep(0.5)
        info = self.bridge.invoke(lambda: self.target.engine_info(name))
        if name in ("ninfer", "strata"):
            # /health can answer a moment before the model id is published; a client that
            # sends a request on the strength of /health alone could still get a 404.
            # Without the engine's key a protected server answers 401 and this would wait
            # out the whole timeout. The key is fetched separately so /status never shows it.
            api_key = self.bridge.invoke(lambda: self.target.engine_api_key(name))
            while not (model_id := _served_model_id(info["url"], api_key)):
                if time.monotonic() > deadline:
                    raise ApiError(504, f"{name} is healthy but /v1/models lists no model")
                time.sleep(0.5)
            info["model_id"] = model_id
        return info

    def start(self, name: str, artifact: str | None = None, wait: bool = True,
              timeout: float | None = None) -> dict:
        with self._lock:
            return self._start(name, artifact, wait, timeout)

    def _start(self, name: str, artifact: str | None, wait: bool, timeout: float | None) -> dict:
        state = self.target.engine_state(name)
        if state in (runtime.STATE_STARTING, runtime.STATE_STOPPING):
            raise ApiError(409, f"{name} is {state}; try again shortly")
        if state == runtime.STATE_READY:
            info = self.bridge.invoke(lambda: self.target.engine_info(name))
            running = info.get("artifact")
            if artifact and running and not _same_artifact(artifact, running):
                raise ApiError(409, f"{name} is already serving {running}; stop it first")
            return {"started": False, "already_running": True, **info}
        running = [n for n in CONFLICTS[name] if self._running(n)]
        if running:
            raise ApiError(409, f"{' and '.join(running)} running; {name} cannot share the GPU with it. "
                                f"Stop it first (POST /suspend or /<engine>/stop)", running=running)
        error = self.bridge.invoke(lambda: self.target.start_engine(name, artifact))
        if error:
            raise ApiError(400, error)
        self._log(name, "started via control API")
        if not wait:
            return {"started": True, "state": self.target.engine_state(name)}
        if timeout is None:
            timeout = STRATA_READY_TIMEOUT_S if name == "strata" else READY_TIMEOUT_S
        return {"started": True, **self._wait_ready(name, timeout)}

    def stop(self, name: str) -> dict:
        with self._lock:
            if not self._running(name):
                return {"stopped": False, "already_stopped": True}
            freed = self._stop_and_wait(name)
            self._log(name, "stopped via control API")
            return {"stopped": True, "freed_mb": freed}

    # -------------------------------------------------------------- suspend / resume
    def suspend(self, engines: list[str] | None = None) -> dict:
        with self._lock:
            names = [n for n in (engines or ENGINES) if self._running(n)]
            for name in names:
                info = self.bridge.invoke(lambda n=name: self.target.engine_info(n))
                freed = self._stop_and_wait(name)
                self.suspended[name] = {"artifact": info.get("artifact"), "footprint_mb": freed}
                self._log(name, f"suspended via control API (released {freed} MB)"
                          if freed is not None else "suspended via control API")
            return {"suspended": names, "records": self.suspended,
                    "free_mb": _gpu_free_mb()}

    def resume(self, force: bool = False, vram_wait_s: float = DEFAULT_VRAM_WAIT_S,
               margin_mb: int = DEFAULT_MARGIN_MB, timeout: float | None = None) -> dict:
        with self._lock:
            resumed = {}
            for name in [n for n in ENGINES if n in self.suspended]:
                record = self.suspended[name]
                if self._running(name):
                    self.suspended.pop(name)
                    resumed[name] = {"already_running": True}
                    continue
                footprint = record.get("footprint_mb")
                if footprint and not force:
                    needed = footprint + margin_mb
                    deadline = time.monotonic() + vram_wait_s
                    free = _gpu_free_mb()
                    while free is not None and free < needed and time.monotonic() < deadline:
                        time.sleep(1)
                        free = _gpu_free_mb()
                    if free is not None and free < needed:
                        # Starting anyway would not fail on Windows - the driver would
                        # silently spill to system memory and decode would run at half
                        # speed. Refuse, and keep the record so the call can be retried.
                        raise ApiError(409, f"not enough free VRAM to resume {name}",
                                       free_mb=free, needed_mb=needed, resumed=resumed)
                resumed[name] = self._start(name, record.get("artifact"), True, timeout)
                self.suspended.pop(name)
                self._log(name, "resumed via control API")
            return {"resumed": resumed, "free_mb": _gpu_free_mb()}


def _same_artifact(a: str, b: str) -> bool:
    from pathlib import Path
    return Path(a).name.lower() == Path(b).name.lower()


# ------------------------------------------------------------------ HTTP layer
class _Handler(BaseHTTPRequestHandler):
    controller: Controller  # set on the subclass created per server

    def log_message(self, *_args) -> None:  # keep stderr quiet in the windowed exe
        pass

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _check_origin(self) -> None:
        """Refuse requests that come from a web page rather than a local tool.

        The API has no password, so without this any site open in a browser could stop or
        start the engines. Browsers attach an Origin header to every cross-site POST, and
        local tools do not send one. The Host check stops a site that points its own domain
        name at 127.0.0.1 (DNS rebinding).
        """
        if self.headers.get("Origin"):
            raise ApiError(403, "requests from web pages are not accepted")
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        if host not in ("127.0.0.1", "localhost", "::1", ""):
            raise ApiError(403, f"unexpected Host header {host!r}; use 127.0.0.1")

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as error:
            raise ApiError(400, "Content-Length is not a number") from error
        if length < 0 or length > MAX_BODY_BYTES:
            raise ApiError(400, f"body must be between 0 and {MAX_BODY_BYTES} bytes")
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as error:
            raise ApiError(400, f"body is not JSON: {error}") from error
        if not isinstance(data, dict):
            raise ApiError(400, "body must be a JSON object")
        return data

    def _dispatch(self, method: str) -> None:
        c = self.controller
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            self._check_origin()
            body = self._body() if method == "POST" else {}
            if method == "GET" and path in ("/", "/status"):
                result = c.status()
            elif method == "POST" and path == "/suspend":
                engines = body.get("engines")
                if engines is not None:
                    if not isinstance(engines, list) or any(e not in ENGINES for e in engines):
                        raise ApiError(400, f"engines must be a list drawn from {list(ENGINES)}")
                result = c.suspend(engines)
            elif method == "POST" and path == "/resume":
                try:
                    vram_wait_s = float(body.get("vram_wait_s", DEFAULT_VRAM_WAIT_S))
                    margin_mb = int(body.get("margin_mb", DEFAULT_MARGIN_MB))
                except (TypeError, ValueError) as error:
                    raise ApiError(400, "vram_wait_s and margin_mb must be numbers") from error
                result = c.resume(force=bool(body.get("force", False)),
                                  vram_wait_s=vram_wait_s, margin_mb=margin_mb)
            elif method == "POST" and path in ("/ninfer/start", "/llama/start", "/strata/start"):
                name = path.split("/")[1]
                # Strata's equivalent of an artifact is the strata-*.json config to run.
                artifact = {"ninfer": body.get("artifact"), "strata": body.get("config")}.get(name)
                result = c.start(name, artifact, wait=bool(body.get("wait", True)))
            elif method == "POST" and path in ("/ninfer/stop", "/llama/stop", "/strata/stop"):
                result = c.stop(path.split("/")[1])
            else:
                raise ApiError(404, f"no route for {method} {path}")
            self._send(200, {"ok": True, **result})
        except ApiError as error:
            self._send(error.status, {"ok": False, "error": str(error), **error.extra})
        except Exception as error:  # noqa: BLE001 - report, never kill the server thread
            self._send(500, {"ok": False, "error": f"{type(error).__name__}: {error}"})

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")


class _ExclusiveServer(ThreadingHTTPServer):
    # HTTPServer sets SO_REUSEADDR, which on Windows lets a SECOND socket bind a port that is
    # already in use - two Switchboard instances would both "own" the API and requests would land
    # on either. Refuse that, and ask Windows for exclusive use of the address.
    allow_reuse_address = False
    allow_reuse_port = False
    daemon_threads = True

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class ControlServer:
    """Owns the HTTP server thread. start() returns False if the port is taken."""

    def __init__(self, controller: Controller, port: int = DEFAULT_PORT):
        self.controller = controller
        self.port = port
        self._httpd: ThreadingHTTPServer | None = None
        self.error = ""

    @property
    def running(self) -> bool:
        return self._httpd is not None

    def start(self) -> bool:
        """Bind and serve. Safe to call repeatedly; Switchboard retries on failure."""
        if self._httpd is not None:
            return True
        handler = type("Handler", (_Handler,), {"controller": self.controller})
        try:
            self._httpd = _ExclusiveServer(("127.0.0.1", self.port), handler)
        except OSError as error:
            # Port busy - another Switchboard, or something transient. The caller retries.
            self.error = str(error)
            return False
        self.error = ""
        threading.Thread(target=self._httpd.serve_forever, name="control-api", daemon=True).start()
        return True

    def shutdown(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
