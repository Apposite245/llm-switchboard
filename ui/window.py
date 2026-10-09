"""Main window: model library, generated flag panels, command preview, process control."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSortFilterProxyModel, Qt, QTimer
from PySide6.QtGui import QAction, QGuiApplication, QStandardItem, QStandardItemModel, QTextCursor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QFileDialog, QFrame,
                               QHBoxLayout, QLabel, QLineEdit, QListView, QMainWindow, QMenu,
                               QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSplitter, QStackedWidget,
                               QTabWidget, QVBoxLayout, QWidget)

from core import control
from core import flags as flagmod
from core import ninfer, profiles, router, runtime, scanner, strata
from core.store import Store

MODE_SINGLE = "single"
MODE_ROUTER = "router"
MODE_NINFER = "ninfer"
MODE_STRATA = "strata"
# Pages with their own launch view and no GGUF library, in launch_stack order after llama.cpp's.
PAGE_MODES = (MODE_NINFER, MODE_STRATA)
RESCAN_INTERVAL_MS = 30_000
CONSOLE_TAB = 2
PROCESSES_TAB = 3

SCOPE_MODEL = "model"
SCOPE_ALL = "all"
SCOPE_HINT = ("This model: changes are saved as overrides for the selected model only.\n"
              "All models: changes set the global defaults, which every model inherits "
              "unless it overrides that setting.")

from .ninfer_page import NInferPage
from .strata_page import StrataPage
from .widgets import (ROLE_BADGES, ROLE_KEY, ROLE_SUB, Card, Combo, FlagValues, ModelDelegate,
                      build_control, run_async, set_plain_text)


class MainWindow(QMainWindow):
    def __init__(self, store: Store):
        super().__init__()
        self.store = store
        self.repos: list[scanner.ModelRepo] = []
        self.repo_by_key: dict[str, scanner.ModelRepo] = {}
        self.current_repo: scanner.ModelRepo | None = None
        self.current_meta: dict = {}
        self.supported_cache: dict[str, set[str]] = {}
        self.external: list[runtime.ExternalServer] = []
        self._loading_profile = False
        self._library_signature: tuple = ()
        self._scan_in_flight = False
        # models.ini sections as of the router's last start/reload.
        self._applied_sections: dict[str, str] = {}
        self._reloading_sections: dict[str, str] = {}
        self._published_models = 0
        # What the running ninfer-serve was started with, for the manifest entry.
        self._ninfer_launch: tuple[dict, str] | None = None
        # Host, port and key the running llama.cpp server was started with. While it runs,
        # its address must not follow whichever model happens to be selected.
        self._llama_launch_values: dict | None = None
        self._gpu_poll_in_flight = False
        self._process_poll_in_flight = False
        self._ninfer_model_id = ""
        # A crash can leave the previous session's NInfer advertised in local-llama.json.
        ninfer.clear_running()

        self.store.on_save_error = self._on_save_error
        self.values = FlagValues(profiles.global_defaults(self.store), self)
        self._scope = self.store.get("edit_scope") or SCOPE_MODEL
        self._scope_combos: list = []
        # flag key -> [(label, reset button, control)], one entry per place the flag is shown
        self._flag_rows: dict[str, list] = {}

        # Two independent engines: llama-server (single/router) and ninfer-serve. They run
        # on different ports and can be up at the same time.
        self.server = runtime.ServerProcess(self)
        self.server.log.connect(self._append_log)
        self.server.state_changed.connect(self._on_server_state)
        self.ninfer_server = runtime.ServerProcess(self)
        self.ninfer_server.log.connect(self._append_ninfer_log)
        self.ninfer_server.state_changed.connect(self._on_ninfer_state)
        # Strata wants the GPU and most of RAM to itself, so starting it beside either of the
        # others (or them beside it) asks first.
        self.strata_server = runtime.ServerProcess(self)
        self.strata_server.log.connect(lambda text: self._append_to(self.strata_console, text))
        self.strata_server.state_changed.connect(self._on_strata_state)
        self._strata_config = ""  # what the running Strata was started with
        self._strata_model_id = ""
        # Which llama.cpp engine the Start button launches; the NInfer view does not change it.
        saved_mode = self.store.get("mode") or MODE_SINGLE
        self._llama_mode_value = self.store.get("llama_mode") or (
            saved_mode if saved_mode in (MODE_SINGLE, MODE_ROUTER) else MODE_SINGLE)

        self.setWindowTitle("LLM Switchboard")
        self.resize(1360, 880)
        self.setMinimumSize(1060, 700)

        self._build_ui()
        self._restore_geometry()

        self.values.changed.connect(self._on_value_changed)

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(500)
        self._save_timer.timeout.connect(self._persist)

        self._machine_timer = QTimer(self)
        self._machine_timer.setInterval(6000)
        self._machine_timer.timeout.connect(self._refresh_machine)
        self._machine_timer.start()

        # Picks up models dropped into a root folder without pressing Rescan.
        self._rescan_timer = QTimer(self)
        self._rescan_timer.setInterval(RESCAN_INTERVAL_MS)
        self._rescan_timer.timeout.connect(lambda: self._rescan(quiet=True))
        self._rescan_timer.start()

        self._reload_binaries()
        self._rescan()
        self._refresh_buttons()
        self._apply_mode_layout()
        self._refresh_machine()

        # Local tools (an MCP server, a script) drive the engines through this rather than
        # launching or killing the server executables behind Switchboard's back.
        self.control_bridge = control.ControlBridge(self)
        self.control = control.ControlServer(
            control.Controller(self, self.control_bridge),
            int(self.store.get("control_port") or control.DEFAULT_PORT))
        # A failed bind retries, so a port that was only briefly busy heals itself instead of
        # leaving the session without an API. The state is shown permanently in the engine
        # line: a one-off status-bar message gets overwritten within seconds.
        self._control_retry = QTimer(self)
        self._control_retry.setInterval(5000)
        self._control_retry.timeout.connect(self._start_control)
        self._control_logged_error = ""
        self._start_control()

    # ------------------------------------------------------------------ chrome
    def _build_ui(self) -> None:
        root = QWidget(self)
        root.setObjectName("Root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_header())

        splitter = QSplitter(Qt.Horizontal, root)
        splitter.setHandleWidth(8)
        self.library_panel = self._build_library()
        splitter.addWidget(self.library_panel)
        splitter.addWidget(self._build_tabs())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([360, 1000])

        body = QWidget(root)
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(14, 12, 14, 12)
        body_layout.addWidget(splitter)
        outer.addWidget(body, 1)

        self.status_label = QLabel("", self)
        self.statusBar().addWidget(self.status_label)
        # Engine state gets its own slot: scan and copy messages share status_label and
        # would otherwise wipe out "Router running / NInfer running".
        self.engine_label = QLabel("stopped", self)
        self.statusBar().addPermanentWidget(self.engine_label)
        self.gpu_label = QLabel("", self)
        self.statusBar().addPermanentWidget(self.gpu_label)

    def _build_header(self) -> QWidget:
        header = QFrame(self)
        header.setObjectName("Header")
        header.setFixedHeight(66)
        row = QHBoxLayout(header)
        row.setContentsMargins(18, 10, 18, 10)
        row.setSpacing(12)

        titles = QVBoxLayout()
        titles.setSpacing(1)
        title = QLabel("LLM Switchboard", header)
        title.setObjectName("HeaderTitle")
        self.header_sub = QLabel("no model selected", header)
        self.header_sub.setObjectName("HeaderSub")
        titles.addWidget(title)
        titles.addWidget(self.header_sub)
        row.addLayout(titles)
        row.addStretch(1)

        mode_label = QLabel("Mode", header)
        mode_label.setObjectName("SectionLabel")
        row.addWidget(mode_label)
        self.mode_combo = Combo(header)
        self.mode_combo.addItem("Single model", MODE_SINGLE)
        self.mode_combo.addItem("Router · all models", MODE_ROUTER)
        self.mode_combo.addItem("NInfer", MODE_NINFER)
        self.mode_combo.addItem("Strata", MODE_STRATA)
        self.mode_combo.setToolTip(
            "Router serves every library model on one port and loads whichever a client asks for.\n"
            "NInfer serves one .ninfer artifact with ninfer-serve.\n"
            "Strata runs one of Strata's own configs with its MoE engine.")
        index = self.mode_combo.findData(self.store.get("mode") or MODE_SINGLE)
        self.mode_combo.setCurrentIndex(max(0, index))
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        row.addWidget(self.mode_combo)

        binary_label = QLabel("Binary", header)
        binary_label.setObjectName("SectionLabel")
        row.addWidget(binary_label)
        self.binary_combo = Combo(header)
        self.binary_combo.setMinimumWidth(300)
        self.binary_combo.currentIndexChanged.connect(self._on_binary_changed)
        row.addWidget(self.binary_combo)

        add_binary = QPushButton("＋", header)
        add_binary.setProperty("glyph", True)
        add_binary.setFixedWidth(34)
        add_binary.setToolTip("Add a llama-server.exe or a folder of builds")
        add_binary.clicked.connect(self._add_binary)
        row.addWidget(add_binary)
        # llama.cpp-only; NInfer picks its exe from the install folder on its own page.
        self._llama_header_widgets = (binary_label, self.binary_combo, add_binary)

        self.start_button = QPushButton(self._start_label(), header)
        self.start_button.setProperty("accent", True)
        self.start_button.setMinimumWidth(130)
        self.start_button.clicked.connect(self._toggle_server)
        row.addWidget(self.start_button)

        # Its own button, always visible: NInfer runs beside the router, not instead of it.
        self.ninfer_button = QPushButton("Start NInfer", header)
        self.ninfer_button.setProperty("accent", True)
        self.ninfer_button.setMinimumWidth(130)
        self.ninfer_button.setToolTip("ninfer-serve runs on its own port, alongside llama.cpp.")
        self.ninfer_button.clicked.connect(self._toggle_ninfer)
        row.addWidget(self.ninfer_button)

        self.strata_button = QPushButton("Start Strata", header)
        self.strata_button.setProperty("accent", True)
        self.strata_button.setMinimumWidth(130)
        self.strata_button.setToolTip("Strata's engine with the config chosen on the Strata page.")
        self.strata_button.clicked.connect(self._toggle_strata)
        row.addWidget(self.strata_button)
        return header

    def _build_library(self) -> QWidget:
        panel = QFrame(self)
        panel.setObjectName("Panel")
        panel.setMinimumWidth(300)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        self.search = QLineEdit(panel)
        self.search.setPlaceholderText("Search models…")
        self.search.setClearButtonEnabled(True)
        layout.addWidget(self.search)

        # Shown until both first-run steps are done: an empty list says nothing about
        # what to do next, and the small ＋ in the header is easy to miss.
        self.setup_guide = QFrame(panel)
        self.setup_guide.setObjectName("Card")
        guide = QVBoxLayout(self.setup_guide)
        guide.setContentsMargins(12, 12, 12, 12)
        guide.setSpacing(8)
        guide_title = QLabel("GET STARTED", self.setup_guide)
        guide_title.setObjectName("CardTitle")
        guide.addWidget(guide_title)
        self.setup_binary = QPushButton("Choose llama-server.exe…", self.setup_guide)
        self.setup_binary.setProperty("accent", True)
        self.setup_binary.clicked.connect(self._add_binary)
        guide.addWidget(self.setup_binary)
        self.setup_models = QPushButton("Add your models folder…", self.setup_guide)
        self.setup_models.setProperty("accent", True)
        self.setup_models.clicked.connect(self._add_root)
        guide.addWidget(self.setup_models)
        guide_hint = QLabel("Models are found in the folder's subfolders, laid out like Hugging Face "
                            "downloads: owner\\repo\\model.gguf. A single file can be added with "
                            "Add model… below.", self.setup_guide)
        guide_hint.setObjectName("Hint")
        guide_hint.setWordWrap(True)
        guide.addWidget(guide_hint)
        layout.addWidget(self.setup_guide)

        self.model_source = QStandardItemModel(self)
        self.model_proxy = QSortFilterProxyModel(self)
        self.model_proxy.setSourceModel(self.model_source)
        self.model_proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.model_proxy.setFilterRole(Qt.DisplayRole)
        self.search.textChanged.connect(self._apply_filter)

        self.model_list = QListView(panel)
        self.model_list.setModel(self.model_proxy)
        self.model_list.setItemDelegate(ModelDelegate(self))
        self.model_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.model_list.setMouseTracking(True)
        self.model_list.setUniformItemSizes(True)
        self.model_list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.model_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.model_list.customContextMenuRequested.connect(self._model_context_menu)
        # currentChanged rather than clicked, so keyboard navigation selects too.
        self.model_list.selectionModel().currentChanged.connect(self._on_current_changed)
        layout.addWidget(self.model_list, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        roots_button = QPushButton("Folders", panel)
        roots_button.clicked.connect(self._folders_menu)
        add_model = QPushButton("Add model…", panel)
        add_model.clicked.connect(self._add_model_file)
        rescan = QPushButton("Rescan", panel)
        rescan.clicked.connect(self._rescan)
        for button in (roots_button, add_model, rescan):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        return panel

    def _build_tabs(self) -> QWidget:
        self.tabs = QTabWidget(self)
        self.launch_stack = QStackedWidget(self)
        self.launch_stack.addWidget(self._build_launch_tab())
        self.ninfer_page = NInferPage(self.store, self)
        self.ninfer_page.changed.connect(self._on_ninfer_changed)
        self.ninfer_page.copy_requested.connect(self._copy)
        self.launch_stack.addWidget(self.ninfer_page)
        self.strata_page = StrataPage(self.store, self)
        self.strata_page.changed.connect(self._on_strata_changed)
        self.strata_page.copy_requested.connect(self._copy)
        self.launch_stack.addWidget(self.strata_page)
        self.tabs.addTab(self.launch_stack, "Launch")
        self.tabs.addTab(self._build_flags_tab(), "Flags")
        self.tabs.addTab(self._build_console_tab(), "Console")
        self.tabs.addTab(self._build_processes_tab(), "Processes")
        self.tabs.currentChanged.connect(
            lambda index: index == PROCESSES_TAB and self._refresh_machine(force=True))
        return self.tabs

    # ------------------------------------------------------------------ launch tab
    def _build_launch_tab(self) -> QWidget:
        scroll = QScrollArea(self)
        self.launch_scroll = scroll
        scroll.setWidgetResizable(True)
        page = QWidget(scroll)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 8, 8, 8)
        layout.setSpacing(12)

        self.model_card = Card("Selected model", page)
        self.model_name = QLabel("Nothing selected", self.model_card)
        self.model_name.setProperty("role", "value")
        self.model_path = QLabel("", self.model_card)
        self.model_path.setObjectName("Hint")
        self.model_path.setWordWrap(True)
        self.model_meta = QLabel("", self.model_card)
        self.model_meta.setObjectName("Hint")
        self.model_card.add_widget(self.model_name)
        self.model_card.add_widget(self.model_path)
        self.model_card.add_widget(self.model_meta)

        self.quant_combo = Combo(self.model_card)
        self.quant_combo.currentIndexChanged.connect(self._on_quant_changed)
        self.model_card.add_row("Quant", self.quant_combo, "Which GGUF in this repo folder to serve.")

        self.detect_label = QLabel("", self.model_card)
        self.detect_label.setObjectName("Hint")
        self.detect_label.setWordWrap(True)
        self.model_card.add_widget(self.detect_label)
        layout.addWidget(self.model_card)

        quick = Card("Quick settings", page)
        quick.add_row("Changes apply to", self._make_scope_combo(quick), SCOPE_HINT)
        for spec in flagmod.SPECS:
            if spec.quick:
                self._add_flag_row(quick, spec)
        layout.addWidget(quick)

        extras = Card("Extra arguments", page)
        self.extra_args = QLineEdit(extras)
        self.extra_args.setPlaceholderText("anything else to append verbatim, e.g. --no-webui")
        self.extra_args.setText(self.store.get("extra_args") or "")
        self.extra_args.textChanged.connect(lambda _t: self._on_value_changed("", None))
        extras.add_widget(self.extra_args)
        layout.addWidget(extras)

        preview = Card("Command", page)
        self.warnings = QLabel("", preview)
        self.warnings.setWordWrap(True)
        self.warnings.setProperty("role", "warn")
        self.warnings.hide()
        preview.add_widget(self.warnings)

        self.publish_row = QWidget(preview)
        publish_layout = QHBoxLayout(self.publish_row)
        publish_layout.setContentsMargins(0, 0, 0, 0)
        self.publish_label = QLabel("", self.publish_row)
        self.publish_label.setProperty("role", "warn")
        self.publish_label.setWordWrap(True)
        publish_button = QPushButton("Apply now", self.publish_row)
        publish_button.clicked.connect(self._apply_router_changes)
        publish_layout.addWidget(self.publish_label, 1)
        publish_layout.addWidget(publish_button)
        self.publish_row.hide()
        preview.add_widget(self.publish_row)

        self.command_view = QPlainTextEdit(preview)
        self.command_view.setObjectName("CommandPreview")
        self.command_view.setReadOnly(True)
        self.command_view.setMinimumHeight(140)
        preview.add_widget(self.command_view)

        self.router_hint = QLabel("", preview)
        self.router_hint.setObjectName("Hint")
        self.router_hint.setWordWrap(True)
        self.router_hint.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.router_hint.hide()
        preview.add_widget(self.router_hint)

        buttons = QWidget(preview)
        row = QHBoxLayout(buttons)
        row.setContentsMargins(0, 0, 0, 0)
        copy_one = QPushButton("Copy command", buttons)
        copy_one.clicked.connect(lambda: self._copy(self._command_text(False)))
        copy_ps = QPushButton("Copy as PowerShell", buttons)
        copy_ps.clicked.connect(lambda: self._copy(self._command_text(True)))
        row.addWidget(copy_one)
        row.addWidget(copy_ps)
        row.addStretch(1)
        preview.add_widget(buttons)
        layout.addWidget(preview)

        layout.addStretch(1)
        scroll.setWidget(page)
        return scroll

    def _build_flags_tab(self) -> QWidget:
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        page = QWidget(scroll)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 8, 8, 8)
        layout.setSpacing(12)

        scope = Card("Editing", page)
        scope.add_row("Changes apply to", self._make_scope_combo(scope), SCOPE_HINT)
        self.scope_hint = QLabel("", scope)
        self.scope_hint.setObjectName("Hint")
        self.scope_hint.setWordWrap(True)
        scope.add_widget(self.scope_hint)
        layout.addWidget(scope)

        by_group: dict[str, list[flagmod.FlagSpec]] = {}
        for spec in flagmod.SPECS:
            by_group.setdefault(spec.group, []).append(spec)

        ordered = [g for g in flagmod.GROUP_ORDER if g in by_group]
        ordered += [g for g in by_group if g not in flagmod.GROUP_ORDER]
        for group in ordered:
            card = Card(group, page)
            for spec in by_group[group]:
                self._add_flag_row(card, spec)
            layout.addWidget(card)

        layout.addStretch(1)
        scroll.setWidget(page)
        return scroll

    # ------------------------------------------------------------------ edit scope
    def _make_scope_combo(self, parent) -> Combo:
        """One per place flags are edited; all copies stay in sync."""
        combo = Combo(parent)
        combo.addItem("This model", SCOPE_MODEL)
        combo.addItem("All models", SCOPE_ALL)
        combo.setCurrentIndex(max(0, combo.findData(self._scope)))
        combo.currentIndexChanged.connect(lambda _i, c=combo: self._set_scope(c.currentData()))
        self._scope_combos.append(combo)
        return combo

    def _add_flag_row(self, card: Card, spec: flagmod.FlagSpec) -> None:
        holder = QWidget(card)
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        control = build_control(spec, self.values, holder)
        row.addWidget(control, 1)
        reset = QPushButton("↺", holder)
        reset.setObjectName("ResetOverride")
        reset.setFixedWidth(30)
        reset.setToolTip("This model overrides the global value. Click to go back to the global one.")
        reset.clicked.connect(lambda _c=False, k=spec.key: self._reset_override(k))
        reset.hide()
        row.addWidget(reset)
        flag_names = " / ".join(t for tokens in (spec.emit_map or {}).values() for t in tokens) or spec.cli
        label = card.add_row(spec.label, holder, spec.help, flag_names)
        self._flag_rows.setdefault(spec.key, []).append((label, reset, control))

    def _set_scope(self, scope: str) -> None:
        if scope == self._scope:
            return
        # Anything still waiting in the debounce belongs to the old scope.
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._persist()
        self._scope = scope
        self.store.set("edit_scope", scope)
        for combo in self._scope_combos:
            combo.blockSignals(True)
            combo.setCurrentIndex(max(0, combo.findData(scope)))
            combo.blockSignals(False)
        self._apply_profile()

    def _overridden_keys(self) -> set[str]:
        repo, quant = self.current_repo, self._current_quant()
        if not repo or not quant:
            return set()
        if self._scope == SCOPE_MODEL:
            # Live, so the marker appears the moment a value departs from the global one.
            return set(profiles.diff(self.values.as_dict(), profiles.base_values(self.store, repo, quant)))
        return set(profiles.overrides(self.store, repo))

    def _refresh_override_markers(self) -> None:
        overridden = self._overridden_keys()
        all_mode = self._scope == SCOPE_ALL
        for key, rows in self._flag_rows.items():
            is_over = key in overridden
            per_model_only = key in profiles.MODEL_SCOPED_KEYS
            for label, reset, widget in rows:
                if label.property("overridden") != is_over:
                    label.setProperty("overridden", is_over)
                    label.style().unpolish(label)
                    label.style().polish(label)
                reset.setVisible(is_over and not per_model_only)
                # A projector or alias belongs to one model; there is no global value to edit.
                widget.setEnabled(not (all_mode and per_model_only))
        if hasattr(self, "scope_hint"):
            name = self.current_repo.name if self.current_repo else "the selected model"
            if all_mode:
                text = ("Editing the global defaults every model inherits. Highlighted settings are "
                        f"overridden by {name}, so changing them here will not affect it.")
            else:
                text = (f"Editing {name} only. Highlighted settings differ from the global defaults; "
                        "↺ puts one back.")
            self.scope_hint.setText(text)

    def _reset_override(self, key: str) -> None:
        repo = self.current_repo
        if not repo:
            return
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._persist()
        profile = dict(self.store.model_profile(repo.key))
        current = profiles.overrides(self.store, repo)
        current.pop(key, None)
        profile["overrides"] = current
        profile.pop("flags", None)
        self.store.save_model_profile(repo.key, profile)
        self._apply_profile()
        self._sync_router()

    def _effective(self) -> tuple[dict, str]:
        """What the selected model will actually run with, whichever scope is being edited."""
        if self._scope == SCOPE_MODEL:
            return self.values.as_dict(), self.extra_args.text()
        repo, quant = self.current_repo, self._current_quant()
        if repo and quant:
            return profiles.resolve(self.store, repo, quant)
        return profiles.global_defaults(self.store), profiles.global_extra(self.store)

    def _build_console_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 8, 8, 8)
        layout.setSpacing(10)

        bar = QWidget(page)
        row = QHBoxLayout(bar)
        row.setContentsMargins(0, 0, 0, 0)
        self.autoscroll = QCheckBox("Auto-scroll", bar)
        self.autoscroll.setChecked(True)
        clear = QPushButton("Clear", bar)
        clear.clicked.connect(lambda: self._active_console().clear())
        copy_log = QPushButton("Copy log", bar)
        copy_log.clicked.connect(lambda: self._copy(self._active_console().toPlainText()))
        row.addWidget(self.autoscroll)
        row.addStretch(1)
        row.addWidget(copy_log)
        row.addWidget(clear)
        layout.addWidget(bar)

        # One pane per engine, so two running servers do not interleave into one log.
        self.console_tabs = QTabWidget(page)
        self.console = self._make_console(page)
        self.ninfer_console = self._make_console(page)
        self.strata_console = self._make_console(page)
        self.console_tabs.addTab(self.console, "llama.cpp")
        self.console_tabs.addTab(self.ninfer_console, "NInfer")
        self.console_tabs.addTab(self.strata_console, "Strata")
        layout.addWidget(self.console_tabs, 1)
        return page

    def _make_console(self, parent) -> QPlainTextEdit:
        view = QPlainTextEdit(parent)
        view.setObjectName("Console")
        view.setReadOnly(True)
        view.setMaximumBlockCount(6000)
        return view

    def _active_console(self) -> QPlainTextEdit:
        return self.console_tabs.currentWidget()

    def _show_console(self, console: QPlainTextEdit) -> None:
        self.tabs.setCurrentIndex(CONSOLE_TAB)
        self.console_tabs.setCurrentWidget(console)

    def _build_processes_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 8, 8, 8)
        layout.setSpacing(12)

        self.proc_card = Card("Running llama-server processes", page)
        self.proc_empty = QLabel("Nothing running.", self.proc_card)
        self.proc_empty.setObjectName("Hint")
        self.proc_card.add_widget(self.proc_empty)
        layout.addWidget(self.proc_card)

        refresh = QPushButton("Refresh", page)
        refresh.clicked.connect(lambda: self._refresh_machine(force=True))
        refresh.setMaximumWidth(120)
        layout.addWidget(refresh)
        layout.addStretch(1)
        return page

    # ------------------------------------------------------------------ binaries
    def _reload_binaries(self) -> None:
        dirs = list(self.store.get("binary_dirs") or [])
        found = runtime.find_binaries(dirs)
        saved = self.store.get("binary") or ""
        if saved and Path(saved).is_file() and not any(str(p).lower() == saved.lower() for p in found):
            found.insert(0, Path(saved))

        self.binary_combo.blockSignals(True)
        self.binary_combo.clear()
        for path in found:
            self.binary_combo.addItem(f"{path.parent.name}  ·  {path.name}", str(path))
        if not found:
            self.binary_combo.addItem("No llama-server.exe found — use ＋", "")
        index = self.binary_combo.findData(saved)
        self.binary_combo.setCurrentIndex(index if index >= 0 else 0)
        self.binary_combo.blockSignals(False)
        self._on_binary_changed()

    def _current_binary(self) -> str:
        return self.binary_combo.currentData() or ""

    def _refresh_setup_guide(self) -> None:
        has_binary = bool(self._current_binary())
        has_models = roots_configured(self.store)
        self.setup_binary.setVisible(not has_binary)
        self.setup_models.setVisible(not has_models)
        self.setup_guide.setVisible(not (has_binary and has_models))

    def _on_binary_changed(self) -> None:
        binary = self._current_binary()
        self.store.set("binary", binary)
        self._refresh_setup_guide()
        if binary and binary not in self.supported_cache:
            run_async(lambda b=binary: (b, flagmod.supported_flags(b)), self._on_flags_probed)
        self._update_preview()

    def _on_flags_probed(self, probed: tuple) -> None:
        binary, result = probed  # the binary that was asked, not the one selected by now
        self.supported_cache[binary] = result
        self._update_preview()

    def _add_binary(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select llama-server.exe", "",
                                              "llama-server (llama-server.exe);;Executables (*.exe)")
        if not path:
            return
        path = path.replace("/", "\\")
        self.store.add_unique("binary_dirs", str(Path(path).parent))
        self.store.set("binary", path)
        self._reload_binaries()

    # ------------------------------------------------------------------ library
    def _folders_menu(self) -> None:
        menu = QMenu(self)
        add = QAction("Add root folder…", menu)
        add.triggered.connect(self._add_root)
        menu.addAction(add)
        roots = list(self.store.get("model_roots") or [])
        pinned = list(self.store.get("extra_models") or [])
        if roots or pinned:
            menu.addSeparator()
        for root in roots:
            action = QAction(f"Remove root:  {root}", menu)
            action.triggered.connect(lambda _c=False, r=root: self._remove_root(r))
            menu.addAction(action)
        for item in pinned:
            action = QAction(f"Remove model:  {Path(item).name}", menu)
            action.triggered.connect(lambda _c=False, m=item: self._remove_pinned(m))
            menu.addAction(action)
        menu.exec(self.cursor().pos())

    def _add_root(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select your models root folder")
        if path:
            self.store.add_unique("model_roots", path.replace("/", "\\"))
            self._rescan()

    def _remove_root(self, root: str) -> None:
        self.store.remove_item("model_roots", root)
        self._rescan()

    def _remove_pinned(self, path: str) -> None:
        self.store.remove_item("extra_models", path)
        self._rescan()

    def _add_model_file(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Add model files", "", "GGUF models (*.gguf)")
        for path in paths:
            self.store.add_unique("extra_models", path.replace("/", "\\"))
        if paths:
            self._rescan()

    def _model_context_menu(self, point) -> None:
        index = self.model_list.indexAt(point)
        if not index.isValid():
            return
        repo = self.repo_by_key.get(index.data(ROLE_KEY))
        if not repo:
            return
        menu = QMenu(self)
        reveal = QAction("Open containing folder", menu)
        reveal.triggered.connect(lambda: self._reveal(repo.folder))
        menu.addAction(reveal)
        if repo.pinned:
            remove = QAction("Remove from library", menu)
            remove.triggered.connect(lambda: self._remove_pinned(str(repo.quants[0].path)))
            menu.addAction(remove)
        menu.exec(self.model_list.viewport().mapToGlobal(point))

    def _reveal(self, folder: Path) -> None:
        import subprocess
        subprocess.Popen(["explorer", str(folder)], creationflags=runtime.CREATE_NO_WINDOW)

    def _rescan(self, quiet: bool = False) -> None:
        if self._scan_in_flight:
            return
        self._scan_in_flight = True
        roots = list(self.store.get("model_roots") or [])
        extras = list(self.store.get("extra_models") or [])
        self._scan_quiet = quiet
        if not quiet:
            self.status_label.setText("Scanning…")
        # Must be a bound method: a lambda has no receiver object, so Qt would run it
        # on the worker thread and touch widgets from there.
        run_async(scanner.scan, self._on_scan_done, self._on_scan_failed, roots, extras)

    def _on_scan_done(self, repos: list) -> None:
        self._on_models_loaded(repos, self._scan_quiet)

    def _on_scan_failed(self, message: str) -> None:
        self._scan_in_flight = False
        self.status_label.setText(f"Scan failed: {message}")

    @staticmethod
    def _signature(repos: list) -> tuple:
        # Paths only: sizes would change every tick while a download is still writing.
        return tuple(
            (r.key, tuple(str(q.path) for q in r.quants),
             tuple(sorted(str(c.path) for group in r.companions.values() for c in group)))
            for r in repos)

    def _on_models_loaded(self, repos: list, quiet: bool = False) -> None:
        self._scan_in_flight = False
        signature = self._signature(repos)
        if quiet and signature == self._library_signature:
            return
        added = len(signature) - len(self._library_signature)
        first_scan = not self._library_signature
        self._library_signature = signature
        self.repos = repos
        self.repo_by_key = {r.key: r for r in repos}
        profiles.migrate_legacy(self.store, repos)
        previous = self.current_repo.key if self.current_repo else None

        self.model_source.clear()
        for repo in repos:
            item = QStandardItem(repo.display)
            item.setEditable(False)
            item.setData(repo.key, ROLE_KEY)
            item.setData(repo.subtitle, ROLE_SUB)
            item.setData(self._badges(repo), ROLE_BADGES)
            self.model_source.appendRow(item)

        self._refresh_setup_guide()
        if not roots_configured(self.store):
            self.status_label.setText("Add your models root folder to get started  →  Folders")
        else:
            self.status_label.setText(f"{len(repos)} model folder(s)")

        if previous and previous in self.repo_by_key:
            self._select_key(previous)
        elif repos:
            self._select_key(repos[0].key)
        else:
            # Save any edit to the model that just vanished, then show the global defaults:
            # with no model selected, whatever is in the form is saved as the globals.
            self._flush_pending()
            self.current_repo = None
            self.current_meta = {}
            self._apply_profile()
            self._refresh_model_card()

        if quiet and not first_scan:
            change = f"{added:+d} model folder(s)" if added else "model files changed"
            self.status_label.setText(f"Library updated: {change}")
        else:
            # Populating controls makes the scroll area chase focus; put it back at the top.
            QTimer.singleShot(0, lambda: self.launch_scroll.verticalScrollBar().setValue(0))
        self._sync_router()

    def _badges(self, repo: scanner.ModelRepo) -> list[str]:
        badges = []
        if repo.companion(scanner.ROLE_MMPROJ):
            badges.append("VISION")
        if repo.companion(scanner.ROLE_MTP):
            badges.append("MTP")
        if repo.companion(scanner.ROLE_DRAFT):
            badges.append("DRAFT")
        shards = max((q.shards for q in repo.quants), default=1)
        if shards > 1:
            badges.append(f"SPLIT {shards}")
        return badges

    def _apply_filter(self, text: str) -> None:
        self.model_proxy.setFilterFixedString(text)

    def _select_key(self, key: str) -> None:
        for row in range(self.model_proxy.rowCount()):
            index = self.model_proxy.index(row, 0)
            if index.data(ROLE_KEY) == key:
                self.model_list.setCurrentIndex(index)
                # setCurrentIndex is a no-op when the index is already current,
                # so load explicitly rather than relying on the signal.
                if self.repo_by_key[key] is not self.current_repo:
                    self._load_repo(self.repo_by_key[key])
                return

    def _on_current_changed(self, current, _previous) -> None:
        if not current.isValid():
            return
        repo = self.repo_by_key.get(current.data(ROLE_KEY))
        if repo and repo is not self.current_repo:
            self._load_repo(repo)

    # ------------------------------------------------------------------ selection
    def _load_repo(self, repo: scanner.ModelRepo) -> None:
        self._flush_pending()  # an edit still in the debounce belongs to the previous model
        self.current_repo = repo
        profile = self.store.model_profile(repo.key)

        self.quant_combo.blockSignals(True)
        self.quant_combo.clear()
        for quant in repo.quants:
            label = quant.label or quant.path.stem
            suffix = f"  ·  {scanner.human_size(quant.size)}"
            if quant.shards > 1:
                suffix += f"  ·  {quant.shards} shards"
            self.quant_combo.addItem(label + suffix, str(quant.path))
        wanted = profile.get("quant", "")
        index = self.quant_combo.findData(wanted)
        self.quant_combo.setCurrentIndex(index if index >= 0 else 0)
        self.quant_combo.blockSignals(False)
        self.quant_combo.setEnabled(len(repo.quants) > 1)

        self._apply_profile(profile)
        self._refresh_model_card()

    def _current_quant(self) -> scanner.GgufFile | None:
        if not self.current_repo:
            return None
        path = self.quant_combo.currentData()
        for quant in self.current_repo.quants:
            if str(quant.path) == path:
                return quant
        return self.current_repo.quants[0] if self.current_repo.quants else None

    def _on_quant_changed(self) -> None:
        if self.current_repo and not self._loading_profile:
            self._apply_profile(self.store.model_profile(self.current_repo.key))
            self._refresh_model_card()
            self._save_timer.start()  # remember the chosen quant, not only on the next flag edit

    def _apply_profile(self, _profile: dict | None = None) -> None:
        self._loading_profile = True
        try:
            repo, quant = self.current_repo, self._current_quant()
            if self._scope == SCOPE_ALL or not (repo and quant):
                # The global defaults themselves. Per-model-only fields still show this
                # model's value (read-only) so the form never looks half empty.
                shown = profiles.global_defaults(self.store)
                extra = profiles.global_extra(self.store)
                if repo and quant:
                    effective, _ = profiles.resolve(self.store, repo, quant)
                    shown.update({k: effective.get(k) for k in profiles.MODEL_SCOPED_KEYS})
            else:
                shown, extra = profiles.resolve(self.store, repo, quant)
            self.values.replace(shown)
            self.extra_args.blockSignals(True)
            self.extra_args.setText(extra)
            self.extra_args.blockSignals(False)
        finally:
            self._loading_profile = False
        self._refresh_override_markers()
        self._update_preview()

    def _flush_pending(self) -> None:
        """Save an edit still waiting in the debounce before the form changes underneath it."""
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._persist()

    def _refresh_model_card(self) -> None:
        repo, quant = self.current_repo, self._current_quant()
        if not repo or not quant:
            self.model_name.setText("Nothing selected")
            self.model_path.setText("")
            self.model_meta.setText("")
            self.detect_label.setText("")
            self.header_sub.setText("no model selected")
            return

        owner = f"{repo.owner} / " if repo.owner else ""
        self.model_name.setText(f"{owner}{repo.name}")
        self.model_path.setText(str(quant.path))
        self.header_sub.setText(f"{repo.name}  ·  {quant.label or 'gguf'}")
        self.model_meta.setText("reading GGUF header…")

        notes = []
        for role, label in ((scanner.ROLE_MMPROJ, "vision projector"),
                            (scanner.ROLE_MTP, "MTP weights"),
                            (scanner.ROLE_DRAFT, "draft model")):
            best = repo.best_companion(role, quant)
            if best:
                notes.append(f"detected {label}: {best.path.name}")
        self.detect_label.setText("  •  ".join(notes) if notes else "no companion files detected")

        self.current_meta = {}
        run_async(lambda q=quant: (str(q.path), scanner.describe(q)), self._on_meta_ready)
        self._update_preview()

    def _on_meta_ready(self, described: tuple) -> None:
        path, meta = described
        quant = self._current_quant()
        if not quant or str(quant.path) != path:
            return  # the selection moved on while the header was being read
        self.current_meta = meta or {}
        if not meta:
            self.model_meta.setText("GGUF header unreadable")
            self._update_preview()
            return
        bits = []
        if meta.get("arch"):
            bits.append(meta["arch"])
        if meta.get("size_label"):
            bits.append(meta["size_label"])
        if meta.get("file_type"):
            bits.append(meta["file_type"])
        if meta.get("n_layer"):
            bits.append(f"{meta['n_layer']} layers")
        if meta.get("n_expert"):
            bits.append(f"{meta['n_expert']} experts")
        if meta.get("n_ctx_train"):
            bits.append(f"trained ctx {meta['n_ctx_train']:,}")
        self.model_meta.setText("  ·  ".join(bits))
        self._update_preview()

    # ------------------------------------------------------------------ command
    def _argv(self) -> list[str]:
        """The command the visible page would run (NInfer's, Strata's, or llama.cpp's)."""
        if self._mode() == MODE_NINFER:
            return self.ninfer_page.argv()
        if self._mode() == MODE_STRATA:
            return self.strata_page.argv()
        return self._llama_argv()

    def _llama_argv(self) -> list[str]:
        binary = self._current_binary()
        if not binary:
            return []
        if self._llama_mode() == MODE_ROUTER:
            return router.argv(binary, self._server_values())
        quant = self._current_quant()
        if not quant:
            return []
        values, extra = self._effective()
        return flagmod.build_argv(binary, quant.path, values, extra)

    def _server_values(self) -> dict:
        """The values that hold the llama.cpp server's host, port and API key.

        A running server keeps what it was started with. Otherwise the router takes them
        from the global settings - it is one process for every model, so a per-model port
        would make its address depend on which model is selected - and a single server
        takes them from the selected model."""
        if self.server.running and self._llama_launch_values is not None:
            return self._llama_launch_values
        if self._llama_mode() == MODE_ROUTER:
            return profiles.global_defaults(self.store)
        return self._effective()[0]

    def _command_text(self, multiline: bool) -> str:
        argv = self._argv()
        if not argv:
            return ""
        if not multiline:
            return flagmod.format_command(argv)
        return "& " + flagmod.format_command(argv, multiline=True)

    def _update_preview(self) -> None:
        if self._mode() == MODE_NINFER:
            self.ninfer_page.gpus = getattr(self, "_gpus", [])
            self.ninfer_page.server_running = self.ninfer_server.running
            self.ninfer_page.update_preview()
            return
        if self._mode() == MODE_STRATA:
            self.strata_page.server_running = self.strata_server.running
            self.strata_page.update_preview()
            return
        argv = self._argv()
        set_plain_text(self.command_view,
                       self._command_text(True) if argv else "Select a model and a llama-server binary.")

        in_router = self._llama_mode() == MODE_ROUTER
        self.router_hint.setVisible(in_router)
        if in_router:
            self.router_hint.setText(
                f"Router serves {self._published_models} model(s) from {router.ini_path()}. "
                "Settings above are saved as this model's preset; host, port and API key are "
                "the router's own and come from the All models settings. Other tools can read "
                f"the model list from {router.manifest_path()}.")

        problems = []
        if not self._current_binary():
            problems.append("No llama-server.exe selected.")
        quant = self._current_quant()
        if quant and not quant.path.exists():
            problems.append("Model file is missing from disk.")

        effective = self._effective()[0]
        port = self._server_values().get("port")
        if port and not self.server.running and runtime.port_in_use(int(port)):
            problems.append(f"Port {int(port)} is already in use.")

        trained = self.current_meta.get("n_ctx_train")
        ctx = effective.get("ctx_size") or 0
        if trained and ctx and ctx > trained:
            problems.append(f"Context {int(ctx):,} exceeds the model's trained {trained:,}.")

        if effective.get("cache_type_v") not in ("", None, "f16", "f32", "bf16") \
                and effective.get("flash_attn") == "off":
            problems.append("A quantised V cache needs flash attention enabled.")

        binary = self._current_binary()
        available = self.supported_cache.get(binary, set())
        unsupported = flagmod.unsupported(effective, available)
        if unsupported:
            names = ", ".join(spec.cli for spec in unsupported[:6])
            problems.append(f"This build does not list: {names}")

        if in_router:
            dropped = router.ignored_extra(self._effective()[1])
            if dropped:
                problems.append("Router mode only passes --long flags from Extra arguments; "
                                f"ignored: {', '.join(dropped[:6])}. Use the long form.")

        if quant:
            gpus = getattr(self, "_gpus", [])
            if gpus and quant.size > gpus[0].free_mb * 1024 * 1024:
                problems.append(
                    f"Model is {scanner.human_size(quant.size)} but only "
                    f"{gpus[0].free_mb / 1024:.1f} GB VRAM is free.")

        self.warnings.setText("  ⚠  " + "\n  ⚠  ".join(problems) if problems else "")
        self.warnings.setVisible(bool(problems))

    def _copy(self, text: str) -> None:
        if text:
            QGuiApplication.clipboard().setText(text)
            self.status_label.setText("Copied to clipboard")

    # ------------------------------------------------------------------ persistence
    def _on_value_changed(self, *_args) -> None:
        if not self._loading_profile:
            self._save_timer.start()
            self._refresh_override_markers()
        self._update_preview()

    def _persist(self) -> None:
        repo, quant = self.current_repo, self._current_quant()
        values, extra = self.values.as_dict(), self.extra_args.text()
        profile = dict(self.store.model_profile(repo.key)) if repo else {}

        if self._scope == SCOPE_ALL or not (repo and quant):
            self.store.set("global_flags", {k: v for k, v in values.items()
                                            if k not in profiles.MODEL_SCOPED_KEYS})
            self.store.set("extra_args", extra)
        else:
            # Only what differs from what this model would inherit is stored, so later
            # global changes still reach every setting it has not deliberately overridden.
            profile["overrides"] = profiles.diff(values, profiles.base_values(self.store, repo, quant))
            profile.pop("flags", None)
            if extra != profiles.global_extra(self.store):
                profile["extra_args"] = extra
            else:
                profile.pop("extra_args", None)

        if repo:
            profile["quant"] = self.quant_combo.currentData() or ""
            if "overrides" not in profile:
                profile["overrides"] = profiles.overrides(self.store, repo)
                profile.pop("flags", None)
            self.store.save_model_profile(repo.key, profile)
        self._refresh_override_markers()
        self._sync_router()

    # ------------------------------------------------------------------ router mode
    def _mode(self) -> str:
        return self.mode_combo.currentData() or MODE_SINGLE

    def _llama_mode(self) -> str:
        """Single or router - what the llama.cpp Start button launches. Viewing the NInfer
        page does not change it, so the router stays startable from there."""
        return self._llama_mode_value

    def _on_mode_changed(self) -> None:
        mode = self._mode()
        self.store.set("mode", mode)
        if mode in (MODE_SINGLE, MODE_ROUTER):
            self._llama_mode_value = mode
            self.store.set("llama_mode", mode)
        self._apply_mode_layout()
        self._sync_router()
        self._update_preview()
        self._refresh_buttons()

    def _apply_mode_layout(self) -> None:
        """NInfer and Strata have their own pages and no GGUF library, so the llama.cpp parts
        step aside."""
        mode = self._mode()
        own_page = mode in PAGE_MODES
        self.library_panel.setVisible(not own_page)
        self.start_button.setVisible(not own_page or self.server.running)
        for widget in self._llama_header_widgets:
            widget.setVisible(not own_page)
        self.launch_stack.setCurrentIndex(PAGE_MODES.index(mode) + 1 if own_page else 0)
        self.tabs.setTabVisible(1, not own_page)  # llama.cpp Flags tab
        if mode == MODE_NINFER:
            self._on_ninfer_changed()
        elif mode == MODE_STRATA:
            self._on_strata_changed()
        else:
            self._refresh_model_card()

    def _on_ninfer_changed(self) -> None:
        if self._mode() != MODE_NINFER:
            return
        artifact = self.ninfer_page.artifact()
        self.header_sub.setText(f"NInfer  ·  {artifact.name}" if artifact else "no artifact selected")

    def _on_strata_changed(self) -> None:
        if self._mode() != MODE_STRATA:
            return
        config = self.strata_page.config()
        self.header_sub.setText(f"Strata  ·  {config.name}" if config else "no config selected")

    def _start_label(self) -> str:
        return "Start router" if self._llama_mode() == MODE_ROUTER else "Start server"

    def _router_running(self) -> bool:
        return self.server.running and "--models-preset" in self.server.argv

    def _ninfer_running(self) -> bool:
        return self.ninfer_server.running

    def _sync_router(self) -> None:
        """Republish models.ini + manifest, then have a running router reload them."""
        if self._llama_mode() != MODE_ROUTER or not self.repos:
            return
        run_async(router.publish, self._on_published, self._on_publish_failed,
                  self.store, list(self.repos), self._server_values())

    def _on_publish_failed(self, message: str) -> None:
        self.status_label.setText(f"Could not write models.ini: {message}")

    def _on_published(self, _changed: list) -> None:
        self._published_models = sum(len(r.quants) for r in self.repos)
        self._update_preview()
        if not self._router_running():
            return
        # Compare with what the router last loaded, not with the previous publish: a
        # publish can finish after the router already started from the same file.
        if not router.diff(self._applied_sections, router.read_sections()):
            return
        run_async(router.loaded_models, self._on_router_load_state, None, self._server_values())

    def _on_router_load_state(self, loaded: list | None) -> None:
        if not self._router_running():
            return
        pending = router.diff(self._applied_sections, router.read_sections())
        if not pending:
            return
        # Reloading unloads a loaded model whose own preset changed, which would cut off
        # a reply in progress; everything else reloads without disturbing it.
        busy = sorted(set(loaded or []) & set(pending))
        if not busy:
            self._apply_router_changes()
            return
        self.publish_label.setText(
            f"Settings for {busy[0]} changed. Applying them unloads it, "
            "cutting off any reply in progress.")
        self.publish_row.show()

    def _apply_router_changes(self) -> None:
        self.publish_row.hide()
        self._reloading_sections = router.read_sections()
        run_async(router.reload, self._on_router_reloaded, None, self._server_values())

    def _on_router_reloaded(self, ok: bool) -> None:
        if ok:
            self._applied_sections = self._reloading_sections
            self.status_label.setText(f"Router updated - serving {self._published_models} model(s)")
        else:
            self.status_label.setText("Router did not accept the reload - stop and start it to apply changes")

    # ------------------------------------------------------------------ server
    def _conflicts(self, name: str) -> list[tuple[str, runtime.ServerProcess]]:
        """Running engines that cannot share the machine with `name` (control.CONFLICTS)."""
        labels = {"llama": "llama.cpp", "ninfer": "NInfer", "strata": "Strata"}
        return [(labels[other], self._engine(other)) for other in control.CONFLICTS[name]
                if self._engine(other).running]

    def _start_with_conflict_check(self, name: str, title: str, start) -> None:
        """Start an engine from its button, asking first if it conflicts with a running one.
        After stopping the others, waits for their VRAM to drain: Strata sizes its expert cache
        from the VRAM free when it loads."""
        def launch(*_args) -> None:
            error = start()
            if error:
                QMessageBox.warning(self, "Cannot start", error)

        conflicts = self._conflicts(name)
        if not conflicts:
            launch()
            return
        choice = self._ask_conflicts(conflicts, title)
        if choice == "stop":
            for _label, process in conflicts:
                process.stop(wait=True)
            self.status_label.setText(f"Waiting for GPU memory to be released before starting {title}…")
            run_async(control.settled_used_mb, launch, launch)
        elif choice == "anyway":
            launch()

    def _ask_conflicts(self, conflicts: list, title: str) -> str:
        """'stop', 'anyway' or 'cancel'."""
        names = " and ".join(label for label, _p in conflicts)
        box = QMessageBox(QMessageBox.Warning, f"Start {title}",
                          f"{names} {'are' if len(conflicts) > 1 else 'is'} running. Strata needs nearly "
                          f"the whole GPU and most of system RAM, so running it beside another engine "
                          f"usually fails partway through loading or runs from system memory.", parent=self)
        stop = box.addButton(f"Stop {names} and start", QMessageBox.AcceptRole)
        anyway = box.addButton("Start anyway", QMessageBox.DestructiveRole)
        box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(stop)
        box.exec()
        clicked = box.clickedButton()
        if clicked == stop:
            return "stop"
        return "anyway" if clicked == anyway else "cancel"

    def _toggle_server(self) -> None:
        """The llama.cpp engine: single model or router. NInfer and Strata have their own buttons."""
        if self.server.running:
            self.server.stop()
            return
        self._start_with_conflict_check("llama", "llama.cpp", self._start_llama)

    def _start_llama(self) -> str | None:
        """Start the llama.cpp engine. Returns why it could not rather than showing a dialog,
        because the control API calls this too and must never raise a modal box."""
        argv = self._llama_argv()
        if not argv:
            return "Select a model and a llama-server binary first."
        if self._llama_mode() == MODE_ROUTER:
            if not self.repos:
                return "The library has no models to serve."
            router.publish(self.store, list(self.repos), self._server_values())
            self._applied_sections = router.read_sections()
            self._published_models = sum(len(r.quants) for r in self.repos)
            self.publish_row.hide()
        self._llama_launch_values = dict(self._server_values())
        self._show_console(self.console)
        self.console.clear()
        self.server.start(argv)
        return None

    def _toggle_ninfer(self) -> None:
        if self.ninfer_server.running:
            self.ninfer_server.stop()
            return
        self._start_with_conflict_check("ninfer", "NInfer", self._start_ninfer)

    def _start_ninfer(self) -> str | None:
        """Start NInfer with the selected artifact's saved settings. Returns an error, never a dialog."""
        argv = self.ninfer_page.argv()
        if not argv:
            return "Select an NInfer artifact first."
        if not Path(argv[0]).is_file():
            return f"{argv[0]} does not exist."
        self.ninfer_page.persist()
        self._ninfer_launch = (self.ninfer_page.values.as_dict(), str(self.ninfer_page.artifact()))
        self._show_console(self.ninfer_console)
        self.ninfer_console.clear()
        self.ninfer_server.start(argv, health_url=self.ninfer_page.health_url())
        return None

    def _toggle_strata(self) -> None:
        if self.strata_server.running:
            self.strata_server.stop()
            return
        self._start_with_conflict_check("strata", "Strata", self._start_strata)

    def _start_strata(self) -> str | None:
        """Start Strata with the selected config. Returns an error, never a dialog."""
        argv = self.strata_page.argv()
        if not argv:
            return "Select a Strata config first."
        for path in (Path(argv[0]), strata.server_path(self.strata_page.folder())):
            if not path.is_file():
                return f"{path} does not exist."
        self._strata_config = str(self.strata_page.config())
        self._show_console(self.strata_console)
        self.strata_console.clear()
        # server.py resolves its own files from the Strata folder, as the setup's run script does.
        self.strata_server.start(argv, health_url=self.strata_page.health_url(),
                                 cwd=self.strata_page.folder())
        return None

    # ------------------------------------------------------------------ control API
    def _start_control(self) -> None:
        url = f"http://127.0.0.1:{self.control.port}"
        if self.control.start():
            self._control_retry.stop()
            message = f"[switchboard] control API listening on {url}"
        else:
            if not self._control_retry.isActive():
                self._control_retry.start()
            if self.control.error == self._control_logged_error:
                return  # already reported; keep retrying quietly
            self._control_logged_error = self.control.error
            message = (f"[switchboard] control API unavailable - port {self.control.port} is in use "
                       f"({self.control.error}); retrying every 5 s")
        for console in (self.console, self.ninfer_console, self.strata_console):
            self._append_to(console, f"{message}\n")
        self._refresh_status()

    def _control_status(self) -> str:
        return (f"API :{self.control.port}" if self.control.running
                else f"API off (:{self.control.port} busy)")

    # ------------------------------------------------------------------ control API target
    # Called by core.control. engine_state may run on any thread (it reads one attribute);
    # the rest run on the GUI thread via ControlBridge.
    def _engine(self, name: str) -> runtime.ServerProcess:
        if name == "ninfer":
            return self.ninfer_server
        if name == "llama":
            return self.server
        if name == "strata":
            return self.strata_server
        raise ValueError(f"unknown engine {name!r}")

    def engine_state(self, name: str) -> str:
        return self._engine(name).state

    def engine_info(self, name: str) -> dict:
        process = self._engine(name)
        if name == "ninfer":
            artifact = self.ninfer_page.artifact()
            launched = self._ninfer_launch[1] if self._ninfer_launch else None
            return {"state": process.state, "pid": process.pid or None,
                    "url": self.ninfer_page.health_url(),
                    # What is actually running, else what Start would launch.
                    "artifact": launched or (str(artifact) if artifact else None),
                    "model_id": self._ninfer_model_id or None}
        if name == "strata":
            config = self.strata_page.config()
            # "artifact" is the config, so suspend/resume and /strata/start treat it like
            # NInfer's artifact.
            return {"state": process.state, "pid": process.pid or None,
                    "url": self.strata_page.health_url(),
                    "artifact": self._strata_config or (str(config) if config else None),
                    "model_id": self._strata_model_id or None}
        effective = self._server_values()
        host = effective.get("host") or "127.0.0.1"
        host = "127.0.0.1" if host in ("0.0.0.0", "") else host
        return {"state": process.state, "pid": process.pid or None,
                "url": f"http://{host}:{int(effective.get('port') or 8080)}",
                "mode": MODE_ROUTER if (self._router_running()
                                        or (not process.running and self._llama_mode() == MODE_ROUTER))
                else MODE_SINGLE}

    def engine_api_key(self, name: str) -> str:
        if name == "ninfer":
            values = self._ninfer_launch[0] if self._ninfer_launch else self.ninfer_page.values.as_dict()
            return str(values.get("api_key") or "")
        return ""

    def start_engine(self, name: str, artifact: str | None) -> str | None:
        if name == "ninfer":
            if artifact and not self.ninfer_page.select_artifact(artifact):
                return f"no artifact matching {artifact!r} in {self.ninfer_page.folder()}\\models"
            return self._start_ninfer()
        if name == "strata":
            if artifact and not self.strata_page.select_config(artifact):
                return f"no config matching {artifact!r} in {self.strata_page.folder()}"
            return self._start_strata()
        return self._start_llama()

    def stop_engine(self, name: str) -> None:
        self._engine(name).stop()

    def log_engine(self, name: str, text: str) -> None:
        consoles = {"ninfer": self.ninfer_console, "strata": self.strata_console}
        self._append_to(consoles.get(name, self.console), text)

    def _on_server_state(self, _state: str) -> None:
        self._refresh_buttons()
        self._refresh_status()
        self._refresh_machine(force=True)

    def _on_ninfer_state(self, state: str) -> None:
        if state == runtime.STATE_READY and self._ninfer_launch:
            values, artifact = self._ninfer_launch
            run_async(ninfer.publish_running, self._on_ninfer_published, None,
                      values, artifact, self.ninfer_server.pid)
        elif state == runtime.STATE_STOPPED and self._ninfer_launch:
            self._ninfer_launch = None
            self._ninfer_model_id = ""
            run_async(ninfer.clear_running, lambda _r: None)
        self._refresh_buttons()
        self._refresh_status()
        self._refresh_machine(force=True)
        self._update_preview()  # the page's port-in-use check depends on the server state

    def _on_strata_state(self, state: str) -> None:
        if state == runtime.STATE_READY:
            run_async(strata.served_model_id, self._on_strata_model_id, None, self.strata_page.health_url())
        elif state == runtime.STATE_STOPPED:
            self._strata_config = ""
            self._strata_model_id = ""
        self._refresh_buttons()
        self._refresh_status()
        self._refresh_machine(force=True)
        self._update_preview()

    def _on_strata_model_id(self, model_id: str) -> None:
        if self.strata_server.state != runtime.STATE_READY:
            return
        self._strata_model_id = model_id
        self._refresh_status()
        if model_id:
            self.status_label.setText(f"Strata serving {model_id} at {self.strata_page.health_url()}/v1")

    def _style_button(self, button: QPushButton, state: str, start_text: str, stop_text: str) -> None:
        labels = {
            runtime.STATE_STOPPED: start_text,
            runtime.STATE_STARTING: stop_text,
            runtime.STATE_READY: stop_text,
            runtime.STATE_STOPPING: "Stopping…",
        }
        button.setText(labels.get(state, start_text))
        button.setProperty("accent", state == runtime.STATE_STOPPED)
        button.setProperty("danger", state in (runtime.STATE_STARTING, runtime.STATE_READY))
        button.style().unpolish(button)
        button.style().polish(button)
        button.setEnabled(state != runtime.STATE_STOPPING)

    def _refresh_buttons(self) -> None:
        noun = "router" if (self._router_running() or self._llama_mode() == MODE_ROUTER) else "server"
        self._style_button(self.start_button, self.server.state, self._start_label(), f"Stop {noun}")
        self._style_button(self.ninfer_button, self.ninfer_server.state, "Start NInfer", "Stop NInfer")
        self._style_button(self.strata_button, self.strata_server.state, "Start Strata", "Stop Strata")
        self.start_button.setVisible(self._mode() not in PAGE_MODES or self.server.running)
        # NInfer and Strata are optional. Their Start buttons appear once the engine has a
        # folder, or while its page is open, so someone who only uses llama.cpp is not shown
        # two buttons that can do nothing.
        mode = self._mode()
        self.ninfer_button.setVisible(mode == MODE_NINFER or self.ninfer_server.running
                                      or bool(self.ninfer_page.folder().strip()))
        self.strata_button.setVisible(mode == MODE_STRATA or self.strata_server.running
                                      or bool(self.strata_page.folder().strip()))

    def _engine_status(self) -> list[str]:
        parts = []
        if self.server.running:
            effective = self._server_values()
            host = effective.get("host") or "127.0.0.1"
            noun = "Router" if self._router_running() else "Server"
            served = f" ({self._published_models} models)" if self._router_running() else ""
            where = f" — http://{host}:{int(effective.get('port') or 0)}" \
                if self.server.state == runtime.STATE_READY else ""
            parts.append(f"{noun} {self._word(self.server.state)}{served}{where}")
        if self.ninfer_server.running:
            where = f" — {self.ninfer_page.health_url()}" \
                if self.ninfer_server.state == runtime.STATE_READY else ""
            parts.append(f"NInfer {self._word(self.ninfer_server.state)}{where}")
        if self.strata_server.running:
            ready = self.strata_server.state == runtime.STATE_READY
            model = f" — {self._strata_model_id}" if ready and self._strata_model_id else ""
            where = f" at {self.strata_page.health_url()}" if ready else ""
            parts.append(f"Strata {self._word(self.strata_server.state)}{model}{where}")
        return parts

    @staticmethod
    def _word(state: str) -> str:
        return {runtime.STATE_STARTING: "starting…", runtime.STATE_READY: "running",
                runtime.STATE_STOPPING: "stopping"}.get(state, "stopped")

    def _refresh_status(self) -> None:
        parts = self._engine_status() or ["stopped"]
        if hasattr(self, "control"):  # _refresh_status also runs before the API exists
            parts.append(self._control_status())
        self.engine_label.setText("   ·   ".join(parts))

    def _on_ninfer_published(self, model_id: str) -> None:
        if not self._ninfer_running():
            return
        self._ninfer_model_id = model_id
        parts = [f"NInfer running — {model_id} at {self.ninfer_page.health_url()}"
                 if part.startswith("NInfer") else part for part in self._engine_status()]
        parts.append(self._control_status())
        self.engine_label.setText("   ·   ".join(parts))
        self.status_label.setText(f"NInfer advertised in {router.manifest_path().name} as {model_id}")

    def _append_ninfer_log(self, text: str) -> None:
        self._append_to(self.ninfer_console, text)

    def _append_log(self, text: str) -> None:
        self._append_to(self.console, text)

    def _append_to(self, console: QPlainTextEdit, text: str) -> None:
        scrollbar = console.verticalScrollBar()
        follow = self.autoscroll.isChecked() and scrollbar.value() >= scrollbar.maximum() - 4
        position = scrollbar.value()
        # Insert through a separate cursor. Moving the view's own cursor to the end (as
        # this used to) makes Qt scroll to keep that cursor visible, which yanked the view
        # to the bottom on every line even with auto-scroll off.
        cursor = QTextCursor(console.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        scrollbar.setValue(scrollbar.maximum() if follow else position)

    # ------------------------------------------------------------------ machine state
    def _refresh_machine(self, force: bool = False) -> None:
        if not self._gpu_poll_in_flight:
            self._gpu_poll_in_flight = True
            run_async(runtime.gpu_info, self._on_gpu_info, self._on_gpu_failed)
        # Listing processes starts a PowerShell each time, so the timer only does it while
        # the Processes tab is showing; state changes and the Refresh button force it.
        wanted = force or self.tabs.currentIndex() == PROCESSES_TAB
        if wanted and not self._process_poll_in_flight:
            self._process_poll_in_flight = True
            run_async(runtime.running_servers, self._on_processes, self._on_processes_failed)

    def _on_gpu_failed(self, _message: str) -> None:
        self._gpu_poll_in_flight = False

    def _on_processes_failed(self, _message: str) -> None:
        self._process_poll_in_flight = False

    def _on_save_error(self, message: str) -> None:
        if hasattr(self, "status_label"):
            self.status_label.setText(message)

    def _on_gpu_info(self, gpus: list) -> None:
        self._gpu_poll_in_flight = False
        self._gpus = gpus
        if not gpus:
            self.gpu_label.setText("no NVIDIA GPU detected")
            return
        parts = [f"{g.name.replace('NVIDIA GeForce ', '')}  {g.used_mb / 1024:.1f} / "
                 f"{g.total_mb / 1024:.1f} GB" for g in gpus]
        self.gpu_label.setText("   ".join(parts))
        self._update_preview()

    def _on_processes(self, servers: list) -> None:
        self._process_poll_in_flight = False
        self.external = servers
        while self.proc_card.body.count():
            item = self.proc_card.body.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

        if not servers:
            empty = QLabel("Nothing running.", self.proc_card)
            empty.setObjectName("Hint")
            self.proc_card.add_widget(empty)
            return

        mine = {self.server.pid, self.ninfer_server.pid} - {0}
        for server in servers:
            row = QWidget(self.proc_card)
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            label = QLabel(server.label + ("   (started here)" if server.pid in mine else ""), row)
            layout.addWidget(label, 1)
            stop = QPushButton("Stop", row)
            stop.setProperty("danger", True)
            stop.clicked.connect(lambda _c=False, s=server: self._stop_external(s))
            layout.addWidget(stop)
            self.proc_card.add_widget(row)

    def _stop_external(self, server: runtime.ExternalServer) -> None:
        if server.pid and server.pid == self.server.pid:
            self.server.stop()
            return
        if server.pid and server.pid == self.ninfer_server.pid:
            self.ninfer_server.stop()
            return
        confirm = QMessageBox.question(
            self, "Stop server",
            f"Force-stop PID {server.pid}?\n\n{Path(server.model).name or server.command[:120]}",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if confirm == QMessageBox.Yes:
            runtime.kill_pid(server.pid)
            QTimer.singleShot(700, lambda: self._refresh_machine(force=True))

    # ------------------------------------------------------------------ geometry
    def _restore_geometry(self) -> None:
        geom = self.store.get("window") or {}
        if geom.get("w") and geom.get("h"):
            self.resize(int(geom["w"]), int(geom["h"]))
        if geom.get("x") is not None and geom.get("y") is not None:
            self.move(int(geom["x"]), int(geom["y"]))

    def closeEvent(self, event) -> None:
        self.store.set("window", {"x": self.x(), "y": self.y(),
                                  "w": self.width(), "h": self.height()})
        # Only an edit still waiting to be written. Saving the form unconditionally here
        # used to write whatever it showed, right or wrong, over the stored settings.
        self._flush_pending()
        self.ninfer_page.persist()
        running = [p for p in (self.server, self.ninfer_server, self.strata_server) if p.running]
        if running:
            names = " and ".join("Strata" if p is self.strata_server
                                 else Path(p.argv[0]).name if p.argv else "server" for p in running)
            # The servers are children of this window and cannot outlive it, so there is
            # no "leave it running" choice to offer.
            confirm = QMessageBox.question(
                self, "Server still running",
                f"Closing stops {names}. Close anyway?",
                QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Yes)
            if confirm != QMessageBox.Yes:
                event.ignore()
                return
            for process in running:
                process.stop(wait=True)  # the whole tree, router children included
            if self._ninfer_launch:
                # The finished signal may not be delivered once the window is closing.
                ninfer.clear_running()
        self.control.shutdown()
        event.accept()


def roots_configured(store: Store) -> bool:
    return bool(store.get("model_roots") or store.get("extra_models"))
