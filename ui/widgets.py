"""Reusable widgets: the flag value store, auto-generated flag controls, model list rendering."""
from __future__ import annotations

import html
from typing import Any, Callable

from PySide6.QtCore import (QModelIndex, QObject, QRect, QRectF, QRunnable,
                            QSize, Qt, Signal, QThreadPool)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFrame,
                               QHBoxLayout, QLabel, QLineEdit, QPushButton, QSizePolicy,
                               QSpinBox, QStyle, QStyledItemDelegate, QVBoxLayout, QWidget)

from core import flags as flagmod
from . import theme

ROLE_KEY = Qt.UserRole + 1
ROLE_SUB = Qt.UserRole + 3
ROLE_BADGES = Qt.UserRole + 4


# ------------------------------------------------------------------ background work
class _WorkerSignals(QObject):
    done = Signal(object)
    failed = Signal(str)


class Worker(QRunnable):
    """Run a callable off the UI thread; result arrives on signals.done."""

    def __init__(self, fn: Callable, *args, **kwargs):
        super().__init__()
        self.fn, self.args, self.kwargs = fn, args, kwargs
        self.signals = _WorkerSignals()

    def run(self) -> None:
        try:
            result = self.fn(*self.args, **self.kwargs)
        except Exception as exc:  # a background failure must never kill the UI
            self.signals.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self.signals.done.emit(result)


def run_async(fn: Callable, on_done: Callable, on_failed: Callable | None = None, *args, **kwargs) -> None:
    worker = Worker(fn, *args, **kwargs)
    worker.signals.done.connect(on_done)
    if on_failed:
        worker.signals.failed.connect(on_failed)
    QThreadPool.globalInstance().start(worker)


# ------------------------------------------------------------------ value store
_MISSING = object()


class FlagValues(QObject):
    """Single source of truth for flag values, so duplicate controls stay in sync."""

    changed = Signal(str, object)

    def __init__(self, initial: dict | None = None, parent=None, specs_by_key: dict | None = None):
        super().__init__(parent)
        self._data: dict[str, Any] = dict(initial or {})
        self._specs = specs_by_key if specs_by_key is not None else flagmod.SPECS_BY_KEY

    def get(self, key: str, fallback: Any = None) -> Any:
        return self._data.get(key, fallback)

    def set(self, key: str, value: Any) -> None:
        if self._data.get(key, _MISSING) != value:
            self._data[key] = value
            self.changed.emit(key, value)

    def replace(self, mapping: dict) -> None:
        for key in set(mapping) | set(self._data):
            new = mapping.get(key, self._specs[key].default if key in self._specs else None)
            self.set(key, new)

    def as_dict(self) -> dict:
        return dict(self._data)


class Combo(QComboBox):
    """QComboBox that paints its own chevron.

    Qt only partially honours the CSS border-triangle trick for ::down-arrow, which
    renders as a small square, so the native arrow is hidden and drawn here instead.
    """

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(QColor(theme.TEXT if self.underMouse() else theme.MUTED), 1.6)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        cx = self.rect().right() - 15
        cy = self.rect().center().y()
        path = QPainterPath()
        path.moveTo(cx - 4.0, cy - 2.0)
        path.lineTo(cx, cy + 2.5)
        path.lineTo(cx + 4.0, cy - 2.0)
        painter.drawPath(path)


# ------------------------------------------------------------------ flag controls
def build_control(spec: flagmod.FlagSpec, values: FlagValues, parent: QWidget | None = None) -> QWidget:
    """Create the input widget for a FlagSpec, two-way bound to the value store."""
    kind = spec.kind

    if kind == flagmod.KIND_BOOL:
        box = QCheckBox(parent)
        box.setChecked(bool(values.get(spec.key, spec.default)))
        box.toggled.connect(lambda v: values.set(spec.key, v))

        def apply_bool(v):
            box.blockSignals(True)
            box.setChecked(bool(v))
            box.blockSignals(False)

        _subscribe(values, spec.key, apply_bool, box)
        return box

    if kind == flagmod.KIND_CHOICE:
        combo = Combo(parent)
        for choice in spec.choices:
            combo.addItem("default" if choice == flagmod.UNSET else choice, choice)
        combo.setCurrentIndex(max(0, combo.findData(values.get(spec.key, spec.default))))
        combo.currentIndexChanged.connect(
            lambda _i: values.set(spec.key, combo.currentData()))

        def apply_choice(v):
            idx = combo.findData(v if v is not None else flagmod.UNSET)
            if idx >= 0:
                combo.blockSignals(True)
                combo.setCurrentIndex(idx)
                combo.blockSignals(False)

        _subscribe(values, spec.key, apply_choice, combo)
        return combo

    if kind in (flagmod.KIND_INT, flagmod.KIND_FLOAT):
        box = QSpinBox(parent) if kind == flagmod.KIND_INT else QDoubleSpinBox(parent)
        box.setSingleStep(spec.step)
        if kind == flagmod.KIND_FLOAT:
            box.setDecimals(spec.decimals)
        # A flag with no default of its own is either left out of the command or sent with a
        # number, and 0 is a real number for most of them (temperature 0, top-k 0). So the
        # box gets one extra position below the real minimum that stands for "not sent".
        unset = None
        if spec.default is None:
            unset = spec.minimum - spec.step  # one step down, so stepping up lands on the minimum
            box.setRange(unset, spec.maximum)
            box.setSpecialValueText("default")
        else:
            box.setRange(spec.minimum, spec.maximum)
            if spec.default <= spec.minimum:
                box.setSpecialValueText("default")

        def shown(v):
            return unset if v is None and unset is not None else (v if v is not None else spec.minimum)

        box.setValue(shown(values.get(spec.key, spec.default)))
        def on_num(v):
            if unset is not None and v < spec.minimum:
                if v != unset:  # typed somewhere between "unset" and the minimum
                    box.blockSignals(True)
                    box.setValue(unset)
                    box.blockSignals(False)
                v = None
            values.set(spec.key, v)

        box.valueChanged.connect(on_num)

        def apply_num(v):
            box.blockSignals(True)
            box.setValue(shown(v))
            box.blockSignals(False)

        _subscribe(values, spec.key, apply_num, box)
        box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        return box

    # text / path
    edit = QLineEdit(parent)
    edit.setPlaceholderText(spec.placeholder or "")
    edit.setText(str(values.get(spec.key, spec.default) or ""))
    edit.textChanged.connect(lambda t: values.set(spec.key, t))

    def apply_text(v):
        text = str(v or "")
        if edit.text() != text:
            edit.blockSignals(True)
            edit.setText(text)
            edit.blockSignals(False)

    _subscribe(values, spec.key, apply_text, edit)

    if kind != flagmod.KIND_PATH:
        return edit

    holder = QWidget(parent)
    row = QHBoxLayout(holder)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(6)
    row.addWidget(edit, 1)
    browse = QPushButton("…", holder)
    browse.setProperty("glyph", True)
    browse.setFixedWidth(34)
    browse.setToolTip("Browse")

    def pick():
        if spec.file_filter:
            path, _ = QFileDialog.getOpenFileName(holder, f"Select {spec.label}", edit.text(),
                                                  spec.file_filter)
        else:
            path = QFileDialog.getExistingDirectory(holder, f"Select {spec.label}", edit.text())
        if path:
            values.set(spec.key, path.replace("/", "\\"))

    browse.clicked.connect(pick)
    row.addWidget(browse)
    return holder


def _subscribe(values: FlagValues, key: str, apply: Callable, owner: QWidget) -> None:
    def on_changed(changed_key: str, value: Any) -> None:
        if changed_key == key:
            apply(value)

    values.changed.connect(on_changed)
    owner.destroyed.connect(lambda *_: _safe_disconnect(values, on_changed))


def _safe_disconnect(values: FlagValues, slot) -> None:
    try:
        values.changed.disconnect(slot)
    except (RuntimeError, TypeError):
        pass


def set_plain_text(view, text: str) -> None:
    """Replace a read-only text box's content only when it differs: rewriting the same text
    on every refresh would throw away a selection the user is in the middle of copying."""
    if view.toPlainText() != text:
        view.setPlainText(text)


def rich_tip(text: str, title: str = "") -> str:
    """Tooltip as rich text. Qt only word-wraps rich text; plain text stays on one long line."""
    parts = [f"<b>{html.escape(title)}</b>"] if title else []
    parts += [html.escape(line) for line in text.split("\n") if line.strip()]
    return "<qt>" + "".join(f"<p style='margin:0 0 6px 0'>{p}</p>" for p in parts) + "</qt>"


class Card(QFrame):
    """Titled container that flag rows get dropped into."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 16)
        outer.setSpacing(12)
        if title:
            label = QLabel(title.upper(), self)
            label.setObjectName("CardTitle")
            outer.addWidget(label)
        self.body = QVBoxLayout()
        self.body.setSpacing(10)
        outer.addLayout(self.body)

    def add_row(self, label_text: str, widget: QWidget, hint: str = "", hint_title: str = "") -> QLabel:
        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        label = QLabel(label_text, row)
        label.setObjectName("SectionLabel")
        label.setMinimumWidth(168)
        label.setMaximumWidth(168)
        label.setWordWrap(True)
        if hint:
            tip = rich_tip(hint, hint_title)
            label.setToolTip(tip)
            widget.setToolTip(tip)
        layout.addWidget(label, 0, Qt.AlignTop)
        layout.addWidget(widget, 1)
        self.body.addWidget(row)
        return label

    def add_widget(self, widget: QWidget) -> None:
        self.body.addWidget(widget)


# ------------------------------------------------------------------ model list
class ModelDelegate(QStyledItemDelegate):
    """Two-line row with right-aligned capability badges."""

    ROW_HEIGHT = 58
    PAD = 12

    def sizeHint(self, option, index) -> QSize:
        return QSize(220, self.ROW_HEIGHT)

    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)

        rect = QRectF(option.rect).adjusted(4, 3, -4, -3)
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)

        if selected or hovered:
            path = QPainterPath()
            path.addRoundedRect(rect, 9, 9)
            painter.fillPath(path, QColor(theme.RAISED if hovered and not selected else "#1d2534"))
            if selected:
                painter.setPen(QPen(QColor(theme.ACCENT), 1))
                painter.drawPath(path)
                marker = QRectF(rect.left() + 1, rect.center().y() - 11, 3, 22)
                mpath = QPainterPath()
                mpath.addRoundedRect(marker, 1.5, 1.5)
                painter.fillPath(mpath, QColor(theme.ACCENT))

        badges = index.data(ROLE_BADGES) or []
        badge_width = self._paint_badges(painter, rect, badges)

        text_left = rect.left() + self.PAD
        text_right = rect.right() - self.PAD - badge_width
        available = max(40, int(text_right - text_left))

        name_font = QFont(painter.font())
        name_font.setPointSizeF(10.0)
        name_font.setWeight(QFont.DemiBold)
        painter.setFont(name_font)
        painter.setPen(QColor(theme.TEXT))
        metrics = QFontMetrics(name_font)
        name = metrics.elidedText(str(index.data(Qt.DisplayRole) or ""), Qt.ElideMiddle, available)
        painter.drawText(QRect(int(text_left), int(rect.top() + 9), available, 18),
                         Qt.AlignLeft | Qt.AlignVCenter, name)

        sub_font = QFont(painter.font())
        sub_font.setPointSizeF(8.5)
        sub_font.setWeight(QFont.Normal)
        painter.setFont(sub_font)
        painter.setPen(QColor(theme.DIM))
        sub_metrics = QFontMetrics(sub_font)
        sub = sub_metrics.elidedText(str(index.data(ROLE_SUB) or ""), Qt.ElideRight, available)
        painter.drawText(QRect(int(text_left), int(rect.top() + 29), available, 16),
                         Qt.AlignLeft | Qt.AlignVCenter, sub)

        painter.restore()

    def _paint_badges(self, painter: QPainter, rect: QRectF, badges: list[str]) -> int:
        if not badges:
            return 0
        font = QFont(painter.font())
        font.setPointSizeF(7.5)
        font.setWeight(QFont.Bold)
        painter.setFont(font)
        metrics = QFontMetrics(font)

        x = rect.right() - self.PAD
        total = 0
        for badge in reversed(badges):
            text_width = metrics.horizontalAdvance(badge)
            width = text_width + 14
            chip = QRectF(x - width, rect.center().y() - 8, width, 16)
            bg, fg = theme.BADGE_COLORS.get(badge.split()[0], ("#232833", theme.MUTED))
            path = QPainterPath()
            path.addRoundedRect(chip, 8, 8)
            painter.fillPath(path, QColor(bg))
            painter.setPen(QColor(fg))
            painter.drawText(chip, Qt.AlignCenter, badge)
            x -= width + 5
            total += width + 5
        return total
