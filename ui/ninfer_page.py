"""NInfer engine page: install folder, artifact picker, generated flag cards, command preview."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
                               QPushButton, QScrollArea, QVBoxLayout, QWidget)

from core import flags as flagmod
from core import ninfer, runtime, scanner

from .widgets import Card, Combo, FlagValues, build_control, run_async, set_plain_text


class NInferPage(QScrollArea):
    """Everything NInfer-specific. Settings persist per artifact under store["ninfer"]."""

    changed = Signal()
    copy_requested = Signal(str)

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self.supported_cache: dict[str, set[str]] = {}
        self._loading = False
        self.gpus: list = []
        self.server_running = False
        self.values = FlagValues(ninfer.defaults(), self, specs_by_key=ninfer.SPECS_BY_KEY)
        self.values.changed.connect(self._on_value_changed)
        self.setWidgetResizable(True)
        self._build()
        self.reload_artifacts()

    # ------------------------------------------------------------------ settings
    def _settings(self) -> dict:
        return dict(self.store.get("ninfer") or {})

    def _save_settings(self, settings: dict) -> None:
        self.store.set("ninfer", settings)

    def folder(self) -> str:
        return self._settings().get("dir") or ninfer.env_dir()

    def artifact(self) -> Path | None:
        path = self.artifact_combo.currentData()
        return Path(path) if path else None

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 8, 8, 8)
        layout.setSpacing(12)

        install = Card("NInfer", page)
        folder_row = QWidget(install)
        row = QHBoxLayout(folder_row)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.folder_edit = QLineEdit(self.folder(), folder_row)
        self.folder_edit.setPlaceholderText("the folder that holds ninfer-serve.exe")
        self.folder_edit.editingFinished.connect(self._on_folder_edited)
        browse = QPushButton("…", folder_row)
        browse.setProperty("glyph", True)
        browse.setFixedWidth(34)
        browse.setToolTip("Select the folder holding ninfer-serve.exe")
        browse.clicked.connect(self._browse_folder)
        row.addWidget(self.folder_edit, 1)
        row.addWidget(browse)
        install.add_row("Install folder", folder_row, "Folder holding ninfer-serve.exe and models\\.")

        artifact_row = QWidget(install)
        row = QHBoxLayout(artifact_row)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.artifact_combo = Combo(artifact_row)
        self.artifact_combo.currentIndexChanged.connect(self._on_artifact_changed)
        rescan = QPushButton("Rescan", artifact_row)
        rescan.clicked.connect(self.reload_artifacts)
        row.addWidget(self.artifact_combo, 1)
        row.addWidget(rescan)
        install.add_row("Artifact", artifact_row, "The .ninfer model to serve. Settings are saved per artifact.")
        self.artifact_hint = QLabel("", install)
        self.artifact_hint.setObjectName("Hint")
        self.artifact_hint.setWordWrap(True)
        install.add_widget(self.artifact_hint)
        layout.addWidget(install)

        quick = Card("Quick settings", page)
        for spec in ninfer.SPECS:
            if spec.quick:
                quick.add_row(spec.label, build_control(spec, self.values, quick), spec.help)
        restore = QPushButton("Reset to recommended", quick)
        restore.setMaximumWidth(200)
        restore.setToolTip("A starting point for a 27B-class model on a 32 GB GPU: MTP drafting, "
                           "vision, int8 KV cache, port 8082, bound to localhost.")
        restore.clicked.connect(lambda: self.values.replace(ninfer.defaults()))
        quick.add_widget(restore)
        layout.addWidget(quick)

        by_group: dict[str, list[flagmod.FlagSpec]] = {}
        for spec in ninfer.SPECS:
            if not spec.quick:
                by_group.setdefault(spec.group, []).append(spec)
        for group in [g for g in ninfer.GROUP_ORDER if g in by_group]:
            card = Card(group, page)
            for spec in by_group[group]:
                card.add_row(spec.label, build_control(spec, self.values, card), spec.help)
            layout.addWidget(card)

        extras = Card("Extra arguments", page)
        self.extra_args = QLineEdit(extras)
        self.extra_args.setPlaceholderText("anything else to append verbatim")
        self.extra_args.textChanged.connect(lambda _t: self._on_value_changed())
        extras.add_widget(self.extra_args)
        layout.addWidget(extras)

        preview = Card("Command", page)
        self.warnings = QLabel("", preview)
        self.warnings.setWordWrap(True)
        self.warnings.setProperty("role", "warn")
        self.warnings.hide()
        preview.add_widget(self.warnings)
        self.command_view = QPlainTextEdit(preview)
        self.command_view.setObjectName("CommandPreview")
        self.command_view.setReadOnly(True)
        self.command_view.setMinimumHeight(140)
        preview.add_widget(self.command_view)
        hint = QLabel("While it runs, the server is advertised in local-llama.json so other local "
                      "tools can find it instead of starting their own.", preview)
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        preview.add_widget(hint)
        buttons = QWidget(preview)
        row = QHBoxLayout(buttons)
        row.setContentsMargins(0, 0, 0, 0)
        copy_one = QPushButton("Copy command", buttons)
        copy_one.clicked.connect(lambda: self.copy_requested.emit(self.command_text(False)))
        copy_ps = QPushButton("Copy as PowerShell", buttons)
        copy_ps.clicked.connect(lambda: self.copy_requested.emit(self.command_text(True)))
        row.addWidget(copy_one)
        row.addWidget(copy_ps)
        row.addStretch(1)
        preview.add_widget(buttons)
        layout.addWidget(preview)

        layout.addStretch(1)
        self.setWidget(page)

    # ------------------------------------------------------------------ folder / artifacts
    def _browse_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select the NInfer folder", self.folder())
        if path:
            self.folder_edit.setText(path.replace("/", "\\"))
            self._on_folder_edited()

    def _on_folder_edited(self) -> None:
        settings = self._settings()
        settings["dir"] = self.folder_edit.text().strip()
        self._save_settings(settings)
        self.reload_artifacts()

    def reload_artifacts(self) -> None:
        wanted = self._settings().get("artifact", "")
        found = ninfer.artifacts(self.folder())
        self.artifact_combo.blockSignals(True)
        self.artifact_combo.clear()
        for path in found:
            size = scanner.human_size(path.stat().st_size) if path.exists() else ""
            self.artifact_combo.addItem(f"{path.name}  ·  {size}", str(path))
        if not found:
            self.artifact_combo.addItem("No .ninfer artifacts in models\\", "")
        index = self.artifact_combo.findData(wanted)
        self.artifact_combo.setCurrentIndex(index if index >= 0 else 0)
        self.artifact_combo.blockSignals(False)
        self._on_artifact_changed()
        exe = str(ninfer.exe_path(self.folder()))
        if Path(exe).is_file() and exe not in self.supported_cache:
            run_async(lambda e=exe: (e, ninfer.supported_flags(e)), self._on_flags_probed)

    def select_artifact(self, wanted: str) -> bool:
        """Select an artifact by full path or file name, loading its saved settings exactly as
        picking it from the list would. Rescans once, so a newly dropped artifact is found."""
        name = Path(wanted).name.lower()
        for attempt in range(2):
            for index in range(self.artifact_combo.count()):
                data = self.artifact_combo.itemData(index) or ""
                if data and (data.lower() == wanted.lower() or Path(data).name.lower() == name):
                    if index != self.artifact_combo.currentIndex():
                        self.artifact_combo.setCurrentIndex(index)  # fires _on_artifact_changed
                    return True
            if attempt == 0:
                self.reload_artifacts()
        return False

    def _on_flags_probed(self, probed: tuple) -> None:
        exe, result = probed  # the exe that was asked, not the folder selected by now
        self.supported_cache[exe] = result
        self.update_preview()

    def _on_artifact_changed(self) -> None:
        artifact = self.artifact()
        profile = (self._settings().get("profiles") or {}).get(artifact.name if artifact else "", {})
        self._loading = True
        try:
            values = ninfer.defaults()
            saved = dict(profile.get("flags") or {})
            if int(saved.get("port") or 0) == 8080:
                saved["port"] = 8082  # NInfer runs beside the router now, not on its port
            values.update(saved)
            self.values.replace(values)
            self.extra_args.setText(profile.get("extra_args", ""))
        finally:
            self._loading = False
        if artifact:
            settings = self._settings()
            settings["artifact"] = str(artifact)
            self._save_settings(settings)
        self.update_preview()
        self.changed.emit()

    # ------------------------------------------------------------------ command
    def argv(self) -> list[str]:
        artifact = self.artifact()
        if not artifact:
            return []
        return ninfer.build_argv(ninfer.exe_path(self.folder()), artifact,
                                 self.values.as_dict(), self.extra_args.text())

    def health_url(self) -> str:
        return ninfer.base_url(self.values.as_dict())

    def command_text(self, multiline: bool) -> str:
        argv = self.argv()
        if not argv:
            return ""
        return ("& " + flagmod.format_command(argv, multiline=True)) if multiline \
            else flagmod.format_command(argv)

    def _on_value_changed(self, *_args) -> None:
        if not self._loading:
            self.persist()
        self.update_preview()
        self.changed.emit()

    def persist(self) -> None:
        artifact = self.artifact()
        if not artifact:
            return
        settings = self._settings()
        settings.setdefault("profiles", {})[artifact.name] = {
            "flags": self.values.as_dict(), "extra_args": self.extra_args.text()}
        settings["artifact"] = str(artifact)
        self._save_settings(settings)

    def update_preview(self) -> None:
        argv = self.argv()
        set_plain_text(self.command_view, self.command_text(True) if argv
                       else "Pick the NInfer folder and an artifact.")
        problems = []
        exe = ninfer.exe_path(self.folder())
        if not self.folder().strip():
            problems.append("Choose the NInfer install folder above.")
        elif not exe.is_file():
            problems.append(f"{ninfer.SERVER_EXE} not found in {self.folder()}.")
        artifact = self.artifact()
        port = self.values.get("port")
        if port and not self.server_running and runtime.port_in_use(int(port)):
            problems.append(f"Port {int(port)} is already in use.")
        if int(port or 0) == 8080:
            problems.append("Port 8080 is the llama.cpp router's; NInfer cannot share it.")
        if self.values.get("draft_tokens") and not self.values.get("spec"):
            problems.append("Draft tokens are ignored without a speculative mode.")
        available = self.supported_cache.get(str(exe), set())
        unsupported = flagmod.unsupported(self.values.as_dict(), available, ninfer.SPECS)
        if unsupported:
            problems.append("This build does not list: " + ", ".join(s.cli for s in unsupported[:6]))
        if artifact and artifact.exists() and self.gpus:
            if artifact.stat().st_size > self.gpus[0].free_mb * 1024 * 1024:
                problems.append(f"Artifact is {scanner.human_size(artifact.stat().st_size)} but only "
                                f"{self.gpus[0].free_mb / 1024:.1f} GB VRAM is free.")
        self.artifact_hint.setText(str(artifact) if artifact else "")
        self.warnings.setText("  ⚠  " + "\n  ⚠  ".join(problems) if problems else "")
        self.warnings.setVisible(bool(problems))
