"""LLM Switchboard - entry point."""
from __future__ import annotations

import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).parent
else:
    APP_DIR = Path(__file__).parent
sys.path.insert(0, str(APP_DIR))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QIcon  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from core.store import Store  # noqa: E402
from ui import theme  # noqa: E402
from ui.window import MainWindow  # noqa: E402


def seed_defaults(store: Store) -> None:
    """First run: look for llama.cpp builds alongside wherever the app was installed."""
    if store.get("binary_dirs"):
        return
    neighbours = [APP_DIR.parent, APP_DIR]
    found = [str(p) for p in neighbours if p.exists()]
    if found:
        store.set("binary_dirs", found)


def resource(*parts: str) -> Path:
    # PyInstaller onedir unpacks bundled data under _internal (sys._MEIPASS).
    return Path(getattr(sys, "_MEIPASS", APP_DIR)).joinpath(*parts)


def main() -> int:
    if sys.platform == "win32":
        # Without an explicit AppUserModelID, a source run groups under python.exe
        # and the taskbar shows Python's icon instead of the window icon.
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("llm.switchboard")
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName("LLM Switchboard")
    app.setWindowIcon(QIcon(str(resource("assets", "icon.png"))))
    app.setStyle("Fusion")
    app.setStyleSheet(theme.stylesheet())

    store = Store()
    seed_defaults(store)

    window = MainWindow(store)
    window.show()
    theme.apply_dark_titlebar(window)
    if store.load_error:
        QMessageBox.warning(window, "Settings could not be loaded", store.load_error)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
