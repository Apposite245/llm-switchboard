"""Process control, binary discovery and machine state (GPU, ports, stray servers)."""
from __future__ import annotations

import json
import re
import socket
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

CREATE_NO_WINDOW = 0x08000000
SERVER_EXE = "llama-server.exe"
NINFER_EXE = "ninfer-serve.exe"

STATE_STOPPED = "stopped"
STATE_STARTING = "starting"
STATE_READY = "ready"
STATE_STOPPING = "stopping"

_READY_RE = re.compile(r"listening on|server is listening|HTTP server listening", re.I)


def _run(args: list[str], timeout: int = 15) -> str:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                              creationflags=CREATE_NO_WINDOW)
        return proc.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""


# ---------------------------------------------------------------- machine state
@dataclass
class GpuInfo:
    index: int
    name: str
    total_mb: int
    used_mb: int

    @property
    def free_mb(self) -> int:
        return max(0, self.total_mb - self.used_mb)


def gpu_info() -> list[GpuInfo]:
    out = _run(["nvidia-smi", "--query-gpu=index,name,memory.total,memory.used",
                "--format=csv,noheader,nounits"], timeout=10)
    gpus = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 4 and parts[0].isdigit():
            gpus.append(GpuInfo(int(parts[0]), parts[1], int(parts[2]), int(parts[3])))
    return gpus


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    bind_host = "" if host in ("0.0.0.0", "") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        try:
            sock.bind((bind_host, port))
            return False
        except OSError:
            return True


@dataclass
class ExternalServer:
    pid: int
    port: int | None
    model: str
    command: str

    @property
    def label(self) -> str:
        name = Path(self.model).name if self.model else "unknown model"
        where = f":{self.port}" if self.port else ""
        return f"PID {self.pid}{where}  ·  {name}"


def running_servers() -> list[ExternalServer]:
    """Every llama-server.exe / ninfer-serve.exe on the machine, including ones we did not start."""
    out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                f"Get-CimInstance Win32_Process -Filter \"Name='{SERVER_EXE}' OR Name='{NINFER_EXE}'\" "
                "| Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"], timeout=20)
    if not out.strip():
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]

    servers = []
    for item in data:
        cmd = item.get("CommandLine") or ""
        port = re.search(r"--port\s+(\d+)", cmd)
        # ninfer-serve takes the artifact positionally.
        model = (re.search(r"(?:--model|-m)\s+(\"[^\"]+\"|\S+)", cmd)
                 or re.search(r"(\"[^\"]+\.ninfer\"|\S+\.ninfer)", cmd))
        servers.append(ExternalServer(
            pid=int(item.get("ProcessId", 0)),
            port=int(port.group(1)) if port else None,
            model=(model.group(1).strip('"') if model else ""),
            command=cmd,
        ))
    return servers


def kill_pid(pid: int, tree: bool = True) -> bool:
    # /T matters for router mode: children are separate llama-server processes that
    # otherwise survive their parent and keep the model's VRAM.
    args = ["taskkill", "/PID", str(pid), "/F"] + (["/T"] if tree else [])
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=15,
                              creationflags=CREATE_NO_WINDOW)
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def find_binaries(search_dirs: list[str] | list[Path]) -> list[Path]:
    """llama-server.exe under each search dir (one level of subfolders)."""
    found: list[Path] = []
    seen = set()
    for raw in search_dirs:
        base = Path(raw)
        if not base.exists():
            continue
        candidates = [base / SERVER_EXE, *base.glob(f"*/{SERVER_EXE}"), *base.glob(f"*/*/{SERVER_EXE}")]
        for path in candidates:
            key = str(path).lower()
            if path.is_file() and key not in seen:
                seen.add(key)
                found.append(path)
    return found


def binary_version(binary: str | Path) -> str:
    out = _run([str(binary), "--version"], timeout=60)
    match = re.search(r"version:\s*(\S+)", out)
    if match:
        return match.group(1)
    match = re.search(r"version:\s*(\S+)", _run(["cmd", "/c", f'"{binary}" --version 2>&1'], timeout=60))
    return match.group(1) if match else ""


# ---------------------------------------------------------------- our server
class ServerProcess(QObject):
    """One server child process (llama-server or ninfer-serve), output streamed as signals."""

    log = Signal(str)
    state_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc: QProcess | None = None
        self._state = STATE_STOPPED
        self.argv: list[str] = []
        self._health_url = ""
        # ninfer-serve's startup log has no fixed "listening" line, so readiness is its /health.
        self._health_timer = QTimer(self)
        self._health_timer.setInterval(1000)
        self._health_timer.timeout.connect(self._poll_health)

    @property
    def state(self) -> str:
        return self._state

    @property
    def running(self) -> bool:
        return self._state in (STATE_STARTING, STATE_READY, STATE_STOPPING)

    @property
    def pid(self) -> int:
        return int(self._proc.processId()) if self._proc else 0

    def _set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            self.state_changed.emit(state)

    def start(self, argv: list[str], health_url: str = "", cwd: str = "") -> None:
        if self.running:
            return
        if self._proc is not None:  # left over from a start that failed
            self._proc.deleteLater()
        self.argv = list(argv)
        self._health_url = health_url
        if health_url:
            self._health_timer.start()
        self._proc = QProcess(self)
        self._proc.setProcessChannelMode(QProcess.MergedChannels)
        self._proc.setWorkingDirectory(cwd or str(Path(argv[0]).parent))
        self._proc.readyReadStandardOutput.connect(self._drain)
        self._proc.finished.connect(self._on_finished)
        self._proc.errorOccurred.connect(self._on_error)
        self._set_state(STATE_STARTING)
        self.log.emit(f"$ {' '.join(argv)}\n")
        self._proc.start(argv[0], argv[1:])

    def stop(self, wait: bool = False) -> None:
        """Stop the server and everything it started. Returns at once unless `wait` is set;
        the state changes to stopped when the process has gone."""
        if not self._proc or not self.running:
            return
        self._set_state(STATE_STOPPING)
        pid = self.pid
        if wait:
            if pid:
                kill_pid(pid, tree=True)
            if self._proc and not self._proc.waitForFinished(5000):
                self._proc.kill()
                self._proc.waitForFinished(3000)
            return
        if pid:
            # taskkill /T takes the whole tree: in router mode the models are separate child
            # processes that would otherwise outlive the router and keep their VRAM.
            try:
                subprocess.Popen(["taskkill", "/PID", str(pid), "/F", "/T"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=CREATE_NO_WINDOW)
            except OSError:
                pass
        QTimer.singleShot(5000, self._kill_if_still_running)

    def _kill_if_still_running(self) -> None:
        if self._proc and self._state == STATE_STOPPING:
            self._proc.kill()

    def _drain(self) -> None:
        if not self._proc:
            return
        text = bytes(self._proc.readAllStandardOutput()).decode("utf-8", errors="replace")
        if not text:
            return
        self.log.emit(text)
        if self._state == STATE_STARTING and _READY_RE.search(text):
            self._set_state(STATE_READY)

    def _poll_health(self) -> None:
        if self._state != STATE_STARTING:
            self._health_timer.stop()
            return
        try:
            with urllib.request.urlopen(f"{self._health_url}/health", timeout=0.4) as response:
                healthy = response.status == 200
        except (urllib.error.URLError, OSError, ValueError):
            healthy = False
        if healthy:
            self._health_timer.stop()
            self._set_state(STATE_READY)

    def _on_error(self, error) -> None:
        if error == QProcess.FailedToStart:
            self._health_timer.stop()
            self.log.emit("\n[switchboard] failed to start - check the binary path.\n")
            self._set_state(STATE_STOPPED)

    def _on_finished(self, code: int, _status) -> None:
        self._health_timer.stop()
        self.log.emit(f"\n[switchboard] {Path(self.argv[0]).name if self.argv else 'server'} exited with code {code}\n")
        proc, self._proc = self._proc, None
        if proc is not None:
            proc.deleteLater()
        self._set_state(STATE_STOPPED)
