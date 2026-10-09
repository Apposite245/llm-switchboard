"""Strata engine page: install folder, config picker, port, command preview. Launch only -
the engine settings live in the config Strata's setup wrote."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
                               QPushButton, QScrollArea, QVBoxLayout, QWidget)

from core import flags as flagmod
from core import runtime, strata

from .widgets import Card, Combo, FlagValues, build_control, set_plain_text

PORT_SPEC = flagmod.FlagSpec(
    "port", "--port", flagmod.KIND_INT, "Port", "Server", default=strata.DEFAULT_PORT,
    minimum=1, maximum=65535, always=True,
    help="8097 is what Strata's own run script uses. Keep it off 8080 (router) and 8082 (NInfer).")


class StrataPage(QScrollArea):
    """Settings persist under store["strata"]: folder, chosen config, port."""

    changed = Signal()
    copy_requested = Signal(str)

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self.server_running = False
        saved = self._settings()
        self.values = FlagValues({"port": int(saved.get("port") or strata.DEFAULT_PORT)}, self,
                                 specs_by_key={"port": PORT_SPEC})
        self.values.changed.connect(self._on_port_changed)
        self.setWidgetResizable(True)
        self._build()
        self.reload_configs()

    # ------------------------------------------------------------------ settings
    def _settings(self) -> dict:
        return dict(self.store.get("strata") or {})

    def _save(self, **changes) -> None:
        settings = self._settings()
        settings.update(changes)
        self.store.set("strata", settings)

    def folder(self) -> str:
        return self._settings().get("dir") or strata.env_dir()

    def config(self) -> Path | None:
        path = self.config_combo.currentData()
        return Path(path) if path else None

    def port(self) -> int:
        return int(self.values.get("port") or strata.DEFAULT_PORT)

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 8, 8, 8)
        layout.setSpacing(12)

        card = Card("Strata", page)
        folder_row = QWidget(card)
        row = QHBoxLayout(folder_row)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.folder_edit = QLineEdit(self.folder(), folder_row)
        self.folder_edit.setPlaceholderText("the Strata folder (holds serve\\server.py and .venv)")
        self.folder_edit.editingFinished.connect(self._on_folder_edited)
        browse = QPushButton("…", folder_row)
        browse.setProperty("glyph", True)
        browse.setFixedWidth(34)
        browse.setToolTip("Select the Strata folder (the one holding START-HERE.bat)")
        browse.clicked.connect(self._browse_folder)
        row.addWidget(self.folder_edit, 1)
        row.addWidget(browse)
        card.add_row("Install folder", folder_row, "Folder holding serve\\server.py and .venv\\.")

        config_row = QWidget(card)
        row = QHBoxLayout(config_row)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.config_combo = Combo(config_row)
        self.config_combo.currentIndexChanged.connect(self._on_config_changed)
        rescan = QPushButton("Rescan", config_row)
        rescan.clicked.connect(self.reload_configs)
        row.addWidget(self.config_combo, 1)
        row.addWidget(rescan)
        card.add_row("Config", config_row,
                     "A strata-<model>.json written by Strata's setup. Its engine settings are used as-is.")
        card.add_row(PORT_SPEC.label, build_control(PORT_SPEC, self.values, card), PORT_SPEC.help)
        self.summary = QLabel("", card)
        self.summary.setObjectName("Hint")
        self.summary.setWordWrap(True)
        card.add_widget(self.summary)
        layout.addWidget(card)

        preview = Card("Command", page)
        self.warnings = QLabel("", preview)
        self.warnings.setWordWrap(True)
        self.warnings.setProperty("role", "warn")
        self.warnings.hide()
        preview.add_widget(self.warnings)
        self.command_view = QPlainTextEdit(preview)
        self.command_view.setObjectName("CommandPreview")
        self.command_view.setReadOnly(True)
        self.command_view.setMinimumHeight(110)
        preview.add_widget(self.command_view)
        hint = QLabel("Strata needs nearly the whole GPU and most of system RAM. Loading takes a "
                      "few minutes, longer on the first start.", preview)
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

    # ------------------------------------------------------------------ folder / configs
    def _browse_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select the Strata folder", self.folder())
        if path:
            self.folder_edit.setText(path.replace("/", "\\"))
            self._on_folder_edited()

    def _on_folder_edited(self) -> None:
        self._save(dir=self.folder_edit.text().strip())
        self.reload_configs()

    def reload_configs(self) -> None:
        wanted = self._settings().get("config", "")
        found = strata.configs(self.folder())
        self.config_combo.blockSignals(True)
        self.config_combo.clear()
        for path in found:
            name = strata.read_config(path).get("model_name") or ""
            self.config_combo.addItem(f"{path.name}  ·  {name}" if name else path.name, str(path))
        if not found:
            self.config_combo.addItem("No strata-*.json configs - run Strata's setup first", "")
        index = self.config_combo.findData(wanted)
        self.config_combo.setCurrentIndex(index if index >= 0 else 0)
        self.config_combo.blockSignals(False)
        self._on_config_changed()

    def select_config(self, wanted: str) -> bool:
        """Select a config by full path or file name. Rescans once, for a newly written one."""
        name = Path(wanted).name.lower()
        for attempt in range(2):
            for index in range(self.config_combo.count()):
                data = self.config_combo.itemData(index) or ""
                if data and (data.lower() == wanted.lower() or Path(data).name.lower() == name):
                    self.config_combo.setCurrentIndex(index)
                    return True
            if attempt == 0:
                self.reload_configs()
        return False

    def _on_config_changed(self) -> None:
        config = self.config()
        if config:
            self._save(config=str(config))
        self.update_preview()
        self.changed.emit()

    def _on_port_changed(self, *_args) -> None:
        self._save(port=self.port())
        self.update_preview()
        self.changed.emit()

    # ------------------------------------------------------------------ command
    def argv(self) -> list[str]:
        config = self.config()
        return strata.build_argv(self.folder(), config, self.port()) if config else []

    def health_url(self) -> str:
        return strata.base_url(self.config(), self.port())

    def command_text(self, multiline: bool) -> str:
        argv = self.argv()
        if not argv:
            return ""
        return ("& " + flagmod.format_command(argv, multiline=True)) if multiline \
            else flagmod.format_command(argv)

    def update_preview(self) -> None:
        argv = self.argv()
        set_plain_text(self.command_view, self.command_text(True) if argv
                       else "Pick the Strata folder and a config.")
        config = self.config()
        cfg = strata.read_config(config) if config else {}
        ctx, kv = strata.engine_arg(cfg, "--max-context"), strata.engine_arg(cfg, "--kv")
        bits = [b for b in (
            cfg.get("model_name"),
            f"context {int(ctx):,}" if ctx.isdigit() else "",
            f"KV {kv}" if kv else "",
            "vision" if cfg.get("vision") else "",
        ) if b]
        self.summary.setText("  ·  ".join(bits))

        problems = []
        folder = self.folder()
        if not folder.strip():
            problems.append("Choose the Strata install folder above.")
        else:
            if not strata.python_path(folder).is_file():
                problems.append(f"{strata.VENV_PYTHON} not found in {folder}.")
            if not strata.server_path(folder).is_file():
                problems.append(f"{strata.SERVER_SCRIPT} not found in {folder}.")
        if config and not cfg:
            problems.append(f"{config.name} could not be read as JSON.")
        port = self.port()
        if port in (8080, 8082, 8090):
            owner = {8080: "the llama.cpp router", 8082: "NInfer",
                     8090: "Switchboard's control API"}[port]
            problems.append(f"Port {port} is {owner}'s.")
        elif not self.server_running and runtime.port_in_use(port):
            problems.append(f"Port {port} is already in use - is Strata already running from its own script?")
        self.warnings.setText("  ⚠  " + "\n  ⚠  ".join(problems) if problems else "")
        self.warnings.setVisible(bool(problems))
