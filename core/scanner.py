"""Model discovery for a HuggingFace-style tree: <root>/<owner>/<repo>/*.gguf

Companion-file detection lives in COMPANION_RULES - add a rule there to teach the
scanner about a new kind of sidecar file.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import gguf

ROLE_MODEL = "model"
ROLE_MMPROJ = "mmproj"
ROLE_MTP = "mtp"
ROLE_DRAFT = "draft"

# Ordered: first rule whose pattern matches the file stem wins. ROLE_MODEL is the fallback.
COMPANION_RULES: list[tuple[str, re.Pattern]] = [
    (ROLE_MMPROJ, re.compile(r"mmproj|mm-proj|clip[-_.]?vision|projector", re.I)),
    (ROLE_MTP, re.compile(r"(?:^|[^a-z0-9])(?:mtp|nextn|next-n|eagle3)(?:[^a-z0-9]|$)", re.I)),
    (ROLE_DRAFT, re.compile(r"(?:^|[^a-z0-9])draft(?:[^a-z0-9]|$)", re.I)),
]

SHARD_RE = re.compile(r"^(?P<stem>.+)-(?P<index>\d{5})-of-(?P<total>\d{5})$")
QUANT_RE = re.compile(
    r"(IQ\d+_[A-Z]+(?:_[A-Z]+)?|Q\d+_\d+|Q\d+_[KS](?:_[SML])?|MXFP4(?:_MOE)?|BF16|F16|F32|F8_E4M3)",
    re.I,
)
MAX_DEPTH = 5


@dataclass
class GgufFile:
    path: Path
    role: str
    size: int
    shards: int = 1
    label: str = ""

    @property
    def name(self) -> str:
        return self.path.name


@dataclass
class ModelRepo:
    folder: Path
    owner: str
    name: str
    root: Path | None = None
    quants: list[GgufFile] = field(default_factory=list)
    companions: dict[str, list[GgufFile]] = field(default_factory=dict)
    pinned: bool = False  # added individually rather than found under a root

    @property
    def key(self) -> str:
        return str(self.folder if not self.pinned else self.quants[0].path).lower()

    @property
    def display(self) -> str:
        return self.name

    @property
    def subtitle(self) -> str:
        bits = [self.owner] if self.owner else []
        bits.append(human_size(sum(q.size for q in self.quants) if len(self.quants) == 1
                               else max(q.size for q in self.quants)))
        return "  ·  ".join(b for b in bits if b)

    def companion(self, role: str) -> list[GgufFile]:
        return self.companions.get(role, [])

    def best_companion(self, role: str, quant: GgufFile | None) -> GgufFile | None:
        """Pick the sidecar that best matches a quant - longest shared filename prefix wins."""
        options = self.companion(role)
        if not options:
            return None
        if quant is None or len(options) == 1:
            return options[0]
        target = quant.path.stem.lower()
        return max(options, key=lambda c: (_shared_prefix(target, c.path.stem.lower()), c.size))


def _shared_prefix(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def human_size(n: int) -> str:
    if n <= 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.2f} {unit}"
        n /= 1024.0
    return f"{n:.2f} TB"


def classify(stem: str) -> str:
    for role, pattern in COMPANION_RULES:
        if pattern.search(stem):
            return role
    return ROLE_MODEL


def quant_label(stem: str) -> str:
    matches = QUANT_RE.findall(stem)
    return matches[-1].upper() if matches else ""


def _collect_gguf(folder: Path) -> list[GgufFile]:
    """All .gguf in one folder, with shard sets collapsed into a single entry."""
    shard_groups: dict[str, list[tuple[int, Path, int]]] = {}
    singles: list[GgufFile] = []

    try:
        entries = list(os.scandir(folder))
    except OSError:
        return []

    for entry in entries:
        if not entry.name.lower().endswith(".gguf"):
            continue
        path = Path(entry.path)
        try:
            # Broken reparse points raise here rather than reporting False.
            if not entry.is_file():
                continue
            size = entry.stat().st_size
        except OSError:
            continue
        shard = SHARD_RE.match(path.stem)
        if shard:
            shard_groups.setdefault(shard.group("stem"), []).append(
                (int(shard.group("index")), path, size)
            )
        else:
            singles.append(GgufFile(path=path, role=classify(path.stem), size=size,
                                    label=quant_label(path.stem)))

    for stem, parts in shard_groups.items():
        parts.sort()
        first = parts[0][1]
        singles.append(GgufFile(
            path=first,
            role=classify(stem),
            size=sum(p[2] for p in parts),
            shards=len(parts),
            label=quant_label(stem),
        ))

    return singles


def _repo_identity(folder: Path, root: Path | None) -> tuple[str, str]:
    if root is None:
        return "", folder.name
    try:
        parts = folder.relative_to(root).parts
    except ValueError:
        return "", folder.name
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    if len(parts) == 1:
        return "", parts[0]
    return "", root.name


def _walk(root: Path, max_depth: int = MAX_DEPTH):
    """Yield directories under root, depth-limited, skipping dot/system folders."""
    # The root is used as given, not resolved: resolving a junction, symlink or mapped drive
    # would yield folders that are no longer "under" the root the caller knows, losing each
    # model's owner and showing paths the user never typed.
    stack = [(root, 0)]
    while stack:
        folder, depth = stack.pop()
        yield folder
        if depth >= max_depth:
            continue
        try:
            for entry in os.scandir(folder):
                if entry.is_dir(follow_symlinks=False) and not entry.name.startswith((".", "$")):
                    stack.append((Path(entry.path), depth + 1))
        except OSError:
            continue


def scan_root(root: str | Path) -> list[ModelRepo]:
    root = Path(root)
    if not root.is_dir():
        return []
    repos: list[ModelRepo] = []
    for folder in _walk(root):
        files = _collect_gguf(folder)
        if not files:
            continue
        quants = [f for f in files if f.role == ROLE_MODEL]
        if not quants:
            # A folder holding nothing but "draft" files: either a model whose name merely
            # contains the word, or a small draft model on its own. Both can be served, so
            # list them. Lone projectors and MTP heads cannot, and stay hidden.
            for f in files:
                if f.role == ROLE_DRAFT:
                    f.role = ROLE_MODEL
            quants = [f for f in files if f.role == ROLE_MODEL]
        if not quants:
            continue
        companions: dict[str, list[GgufFile]] = {}
        for f in files:
            if f.role != ROLE_MODEL:
                companions.setdefault(f.role, []).append(f)
        owner, name = _repo_identity(folder, root)
        quants.sort(key=lambda f: f.path.name.lower())
        repos.append(ModelRepo(folder=folder, owner=owner, name=name, root=root,
                               quants=quants, companions=companions))
    return repos


def scan(roots: list[str] | list[Path], extra_files: list[str] | list[Path] | None = None) -> list[ModelRepo]:
    found: dict[str, ModelRepo] = {}
    for root in roots:
        for repo in scan_root(root):
            found[repo.key] = repo

    for raw in (extra_files or []):
        path = Path(raw)
        if not path.is_file():
            continue
        siblings = _collect_gguf(path.parent)
        companions: dict[str, list[GgufFile]] = {}
        for f in siblings:
            if f.role != ROLE_MODEL:
                companions.setdefault(f.role, []).append(f)
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        quant = GgufFile(path=path, role=ROLE_MODEL, size=size, label=quant_label(path.stem))
        repo = ModelRepo(folder=path.parent, owner="", name=path.stem, root=None,
                         quants=[quant], companions=companions, pinned=True)
        found[repo.key] = repo

    # By the name the list shows (the owner is only its subtitle), then owner to break ties.
    return sorted(found.values(), key=lambda r: (r.name.lower(), r.owner.lower()))


def describe(quant: GgufFile) -> dict:
    """GGUF header summary for one file. Safe to call off the UI thread."""
    return gguf.summarize(gguf.read_metadata(quant.path))
