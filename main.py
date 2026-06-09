"""
PSXmod main window — v4
"""
import sys
import re
import struct
from pathlib import Path
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QSplitter, QTreeWidget, QTreeWidgetItem, QLabel, QPushButton,
    QFileDialog, QStatusBar, QMessageBox, QFrame, QGridLayout,
    QSizePolicy, QAbstractItemView, QPlainTextEdit, QSpinBox, QStackedWidget
)
from PyQt6.QtCore import Qt, QSize, pyqtSignal, QTimer
from PyQt6.QtGui import QPixmap, QImage, QColor, QFont, QBrush, QPainter, QPen
from PyQt6.QtWidgets import QComboBox

from core.jpsxdec import (
    IndexEntry, TimInfo, AudioInfo, parse_tim, decode_tim_to_rgba,
    encode_to_tim, parse_wav_info, validate_and_encode_audio,
    extract_audio_for_item, _run, save_tim_for_item,
)
from core.project import Project
from core.workers import IndexWorker, BuildWorker, AudioExtractWorker

import pygame.mixer as _pg_mixer

APP_DIR = Path(__file__).parent.resolve()
JPSXDEC_JAR = (APP_DIR / "jpsxdec" / "jpsxdec.jar").resolve()

# Per-game working paths. Each game gets its own folder, APP_DIR/<bin name>/,
# so the app root doesn't fill up with non-specific working folders. These
# defaults are repointed by set_workspace() the moment a BIN or project loads.
WORKSPACE_DIR = APP_DIR
EXISTING_FILES_DIR = (APP_DIR / "EXISTING_FILES").resolve()
NEW_FILES_DIR = (APP_DIR / "NEW_FILES").resolve()
NEW_AUDIO_DIR = (APP_DIR / "NEW_AUDIO").resolve()
TO_MODIFY_DIR = (APP_DIR / "TO_MODIFY").resolve()
INDEX_PATH = (APP_DIR / "jpsxdec.idx").resolve()

# App-level (not game-specific): always stays in the app root.
RECENT_PROJECT_FILE = (APP_DIR / "recent_project.txt").resolve()


def set_workspace(bin_path) -> Path:
    """Point all per-game working paths at APP_DIR/<bin name>/ so every game's
    extracted files, new assets, index and project file stay together in one
    folder named after the BIN. Creates the folder and returns it."""
    global WORKSPACE_DIR, EXISTING_FILES_DIR, NEW_FILES_DIR
    global NEW_AUDIO_DIR, TO_MODIFY_DIR, INDEX_PATH
    WORKSPACE_DIR = (APP_DIR / Path(bin_path).stem).resolve()
    EXISTING_FILES_DIR = WORKSPACE_DIR / "EXISTING_FILES"
    NEW_FILES_DIR = WORKSPACE_DIR / "NEW_FILES"
    NEW_AUDIO_DIR = WORKSPACE_DIR / "NEW_AUDIO"
    TO_MODIFY_DIR = WORKSPACE_DIR / "TO_MODIFY"
    INDEX_PATH = WORKSPACE_DIR / "jpsxdec.idx"
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    return WORKSPACE_DIR

from dataclasses import dataclass as _dataclass
from typing import Optional as _Optional


@_dataclass
class ImportInfo:
    path: Path
    file_type: str       # "PNG", "GIF", "TIM", "OTHER"
    width: int
    height: int
    num_colors: int      # 0 = direct colour (16/24bpp)
    bpp_detected: int    # best-guess bpp
    tim_info: _Optional[TimInfo] = None


def parse_import_file(path: Path) -> _Optional[ImportInfo]:
    suffix = path.suffix.lower()
    if suffix == ".tim":
        tim = parse_tim(path)
        if tim is None:
            return None
        return ImportInfo(
            path=path, file_type="TIM",
            width=tim.image_width, height=tim.image_height,
            num_colors=tim.clut_width * tim.clut_height if tim.has_clut else 0,
            bpp_detected=tim.bpp, tim_info=tim,
        )
    try:
        from PIL import Image as _PIL
        img = _PIL.open(str(path))
        w, h = img.size
        mode = img.mode
        if mode == "P":
            pal = img.getpalette() or []
            nc = len(pal) // 3
            bpp = 4 if nc <= 16 else 8
        elif mode in ("1", "L", "LA"):
            nc = 256; bpp = 8
        elif mode in ("RGB", "RGBA"):
            nc = 0; bpp = 24
        else:
            nc = 0; bpp = 24
        ftype = "GIF" if suffix == ".gif" else "PNG"
        return ImportInfo(path=path, file_type=ftype, width=w, height=h,
                          num_colors=nc, bpp_detected=bpp)
    except Exception:
        return None


TYPE_COLORS = {
    "Image": "#1D9E75",
    "Audio": "#BA7517",
    "Video": "#1a73e8",
    "File":  "#555555",
}


def _tim_to_pixmap(tim_path: Path, palette_index: int = 0, max_size: int = 200) -> QPixmap | None:
    result = decode_tim_to_rgba(tim_path, palette_index)
    if result is None:
        return None
    w, h, rgba = result
    img = QImage(rgba, w, h, w * 4, QImage.Format.Format_RGBA8888)
    pix = QPixmap.fromImage(img)
    if max_size:
        pix = pix.scaled(
            max_size, max_size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
    return pix


def _wav_to_pixmap(wav_path: Path, w: int = 200, h: int = 80) -> QPixmap | None:
    """Render a rough amplitude waveform from a WAV file."""
    import wave as _wave, struct as _s
    try:
        with _wave.open(str(wav_path), 'rb') as wf:
            ch = wf.getnchannels()
            sw = wf.getsampwidth()
            nf = wf.getnframes()
            raw = wf.readframes(nf)
    except Exception:
        return None

    frame_size = ch * sw
    total = len(raw) // frame_size
    if total == 0:
        return None

    step = max(1, total // w)
    peaks = []
    for i in range(w):
        start = i * step * frame_size
        end = min(start + step * frame_size, len(raw))
        block = raw[start:end]
        if not block:
            peaks.append(0.0)
            continue
        if sw == 2:
            vals = [abs(_s.unpack_from('<h', block, j)[0]) / 32768.0
                    for j in range(0, len(block) - 1, 2 * ch)]
        elif sw == 1:
            vals = [abs(b - 128) / 128.0 for b in block[::ch]]
        else:
            vals = [0.5]
        peaks.append(max(vals) if vals else 0.0)

    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor(28, 28, 28))
    painter = QPainter(img)
    pen = QPen(QColor(29, 158, 117))
    pen.setWidth(1)
    painter.setPen(pen)
    mid = h // 2
    for x, peak in enumerate(peaks):
        bar = max(1, int(peak * (mid - 2)))
        painter.drawLine(x, mid - bar, x, mid + bar)
    painter.end()
    return QPixmap.fromImage(img)


_TEXT_EXT_RE = re.compile(r'\.(txt|dat|cnf|\d{2})$', re.IGNORECASE)

# PS1 BIN sector constants (Mode 2 Form 1)
_PS1_SECTOR_SIZE = 2352
_PS1_USER_DATA_OFFSET = 24
_PS1_USER_DATA_SIZE = 2048


def _read_bin_sectors(bin_path: Path, sector_start: int, sector_end: int,
                      file_size: int = 0) -> bytes | None:
    """Read raw file data from a PS1 BIN by sector range."""
    if not bin_path or not bin_path.exists():
        return None
    if sector_start <= 0 or sector_end < sector_start:
        return None
    try:
        data = bytearray()
        with open(bin_path, 'rb') as f:
            for s in range(sector_start, sector_end + 1):
                f.seek(s * _PS1_SECTOR_SIZE + _PS1_USER_DATA_OFFSET)
                chunk = f.read(_PS1_USER_DATA_SIZE)
                if not chunk:
                    break
                data.extend(chunk)
        if file_size > 0:
            return bytes(data[:file_size])
        trimmed = bytes(data).rstrip(b'\x00')
        return trimmed if trimmed else bytes(data[:_PS1_USER_DATA_SIZE])
    except Exception:
        return None


def _entry_is_text(entry: IndexEntry) -> bool:
    for s in (entry.full_id, entry.name):
        if s and _TEXT_EXT_RE.search(s):
            return True
    return False


def _entry_is_hex(entry: IndexEntry) -> bool:
    if entry.entry_type != 'File':
        return False
    for s in (entry.full_id, entry.name):
        if s and s.upper().endswith('.T'):
            return True
    return False


def extract_tim_for_entry(index_path: Path, entry: IndexEntry, out_dir: Path) -> Path | None:
    """Ensure a TIM for a single index item is present in out_dir and return its path.

    Uses jPSXdec per-item `-imgfmt tim` via `save_tim_for_item`, then searches for
    any matching .tim under `out_dir` and returns the first match.
    """
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # If a matching TIM already exists, return it
    for p in out_dir.rglob("*.tim"):
        if entry.name in p.name:
            return p

    # Attempt to save this item as a TIM (per-item)
    try:
        ok, msg = save_tim_for_item(index_path, entry.number, out_dir)
    except Exception:
        ok = False

    # Re-scan for any matching TIM file
    for p in out_dir.rglob("*.tim"):
        if entry.name in p.name:
            return p
    return None


# ---------------------------------------------------------------------------
# Index panel — proper collapsible QTreeWidget, built once, never mutated
# ---------------------------------------------------------------------------
class IndexPanel(QWidget):
    checked_changed = pyqtSignal(set)   # emits set[int] of checked entry numbers

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.header_label = QLabel("index")
        self.header_label.setStyleSheet(
            "padding:6px 10px; font-size:11px; font-weight:500;"
            "color:#6B8AA0; border-bottom:1px solid #A8BCCF; background:#D5E0EA;"
        )
        layout.addWidget(self.header_label)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["", "#", "name", "type / size"])
        self.tree.header().setDefaultSectionSize(80)
        self.tree.setColumnWidth(0, 48)   # checkbox / arrow col
        self.tree.setColumnWidth(1, 88)   # entry number
        self.tree.setColumnWidth(2, 180)  # name
        self.tree.setColumnWidth(3, 90)   # type/size
        self.tree.setIndentation(14)
        self.tree.setAnimated(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setStyleSheet("""
            QTreeWidget {
                border: none;
                font-size: 12px;
                font-family: monospace;
                background: #D5E0EA;
            }
            QTreeWidget::item {
                padding: 2px 2px;
                border-bottom: 1px solid #C0CFDC;
            }
            QTreeWidget::item:selected {
                background: #4A6E8A;
                color: #ECEFF1;
            }
            QTreeWidget::item:hover:!selected {
                background: #C5D5E4;
            }
            QTreeWidget::branch {
                background: #D5E0EA;
            }
            QTreeWidget::indicator {
                width: 13px;
                height: 13px;
                border: 1.5px solid #3D5567;
                border-radius: 2px;
                background: #B8CDD9;
            }
            QTreeWidget::indicator:hover {
                border-color: #1E2E3C;
                background: #A0B8CA;
            }
            QTreeWidget::indicator:checked {
                background: #1E2E3C;
                border-color: #1E2E3C;
                image: url(none);
            }
            QTreeWidget::indicator:unchecked:selected {
                border-color: #ECEFF1;
                background: rgba(255,255,255,0.15);
            }
            QTreeWidget::indicator:checked:selected {
                background: #ECEFF1;
                border-color: #ECEFF1;
            }
        """)
        layout.addWidget(self.tree)

        # Internal entry list matching tree items (avoids mutation-on-click)
        self._item_to_entry: dict[int, IndexEntry] = {}  # id(item) -> entry
        self._checked_entries: set[int] = set()

        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.itemExpanded.connect(self._on_item_expanded)
        self.tree.itemCollapsed.connect(self._on_item_collapsed)

    def populate(self, entries: list[IndexEntry]):
        # Block signals so currentItemChanged / itemChanged don't fire during build
        self.tree.blockSignals(True)
        self.tree.clear()
        self._item_to_entry.clear()
        self._checked_entries.clear()

        # Pre-pass: which top-level names have at least one child?
        folder_names = {e.parent_name for e in entries if e.parent_name}

        # Leaf flags include checkbox; folder flags do not
        leaf_flags = (
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsUserCheckable
        )
        folder_flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

        parent_items: dict[str, QTreeWidgetItem] = {}  # name -> item

        for entry in entries:
            is_folder = (not entry.parent_name) and (entry.name in folder_names)
            item = QTreeWidgetItem()

            if is_folder:
                item.setFlags(folder_flags)
                item.setText(0, "▶")
            else:
                item.setFlags(leaf_flags)
                item.setCheckState(0, Qt.CheckState.Unchecked)

            item.setText(1, str(entry.number))
            item.setText(2, entry.name)

            color = TYPE_COLORS.get(entry.entry_type, "#555")
            item.setForeground(2, QColor(color))

            if entry.is_image and entry.width:
                item.setText(3, f"{entry.entry_type} {entry.width}×{entry.height}")
                item.setForeground(3, QColor(color))
            else:
                item.setText(3, entry.entry_type)
                item.setForeground(3, QColor(color))

            self._item_to_entry[id(item)] = entry

            if entry.parent_name and entry.parent_name in parent_items:
                parent_items[entry.parent_name].addChild(item)
            else:
                self.tree.addTopLevelItem(item)

            if not entry.parent_name:
                parent_items[entry.name] = item

        # Collapse all by default — user expands what they need
        self.tree.collapseAll()

        image_count = sum(1 for e in entries if e.is_image)
        self.header_label.setText(
            f"index — {len(entries)} entries  ({image_count} images)"
        )
        self.tree.blockSignals(False)

    def current_entry(self) -> IndexEntry | None:
        item = self.tree.currentItem()
        if item is None:
            return None
        return self._item_to_entry.get(id(item))

    # ---- checkbox / arrow helpers ----

    def _on_item_changed(self, item, column):
        if column != 0:
            return
        if not (item.flags() & Qt.ItemFlag.ItemIsUserCheckable):
            return  # folder arrow text change — ignore
        entry = self._item_to_entry.get(id(item))
        if entry is None:
            return
        if item.checkState(0) == Qt.CheckState.Checked:
            self._checked_entries.add(entry.number)
        else:
            self._checked_entries.discard(entry.number)
        self.checked_changed.emit(set(self._checked_entries))

    def _on_item_expanded(self, item):
        if not (item.flags() & Qt.ItemFlag.ItemIsUserCheckable):
            item.setText(0, "▼")

    def _on_item_collapsed(self, item):
        if not (item.flags() & Qt.ItemFlag.ItemIsUserCheckable):
            item.setText(0, "▶")

    def get_checked_entries(self) -> set[int]:
        return set(self._checked_entries)

    def set_checked_entries(self, checked: set[int]):
        self._checked_entries = set(checked)
        self.tree.blockSignals(True)
        self._apply_checked_recursive(self.tree.invisibleRootItem())
        self.tree.blockSignals(False)

    def _apply_checked_recursive(self, parent):
        for i in range(parent.childCount()):
            item = parent.child(i)
            entry = self._item_to_entry.get(id(item))
            if entry and (item.flags() & Qt.ItemFlag.ItemIsUserCheckable):
                state = Qt.CheckState.Checked if entry.number in self._checked_entries else Qt.CheckState.Unchecked
                item.setCheckState(0, state)
            self._apply_checked_recursive(item)


# ---------------------------------------------------------------------------
# Image preview panel
# ---------------------------------------------------------------------------
SUPPORTED_IMPORT_EXTS = {".png", ".gif", ".tim", ".wav"}
AUDIO_IMPORT_EXTS = {".wav"}


class ImagePanel(QFrame):
    file_selected = pyqtSignal(object)  # emits Path

    def __init__(self, title: str, empty_text: str = "no image", interactive: bool = False, parent=None):
        super().__init__(parent)
        self._empty_text = empty_text
        self._interactive = interactive
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet("QFrame { border: none; }")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.header = QLabel(title)
        self.header.setStyleSheet(
            "padding:6px 10px; font-size:11px; font-weight:500; color:#6B8AA0;"
            "border-bottom:1px solid #A8BCCF; text-transform:uppercase; background:#E8EFF5;"
        )
        layout.addWidget(self.header)

        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setMinimumSize(160, 160)
        self.image_label.setStyleSheet(
            "background: repeating-conic-gradient(#ddd 0% 25%, white 0% 50%) 0 0/16px 16px;"
        )
        layout.addWidget(self.image_label, 1)

        self.meta_bar = QLabel()
        self.meta_bar.setStyleSheet(
            "padding:5px 10px; font-size:11px; color:#3D5A72;"
            "border-top:1px solid #A8BCCF; font-family:monospace; background:#E8EFF5;"
        )
        self.meta_bar.setWordWrap(True)
        self.meta_bar.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.meta_bar)

        if interactive:
            self.setAcceptDrops(True)
            self.image_label.setCursor(Qt.CursorShape.PointingHandCursor)

    # ---- display ----

    def set_image(self, png_path: Path | None):
        if png_path and png_path.exists():
            pix = QPixmap(str(png_path))
            scaled = pix.scaled(200, 200,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation)
            self.image_label.setPixmap(scaled)
            self.image_label.setText("")
        else:
            self.image_label.setPixmap(QPixmap())
            self.image_label.setText(self._empty_text if png_path is None else "file not found")

    def set_pil_image(self, pil_img):
        """Display a PIL Image directly (for imported PNG/GIF preview)."""
        try:
            rgba = pil_img.convert("RGBA")
            w, h = rgba.size
            raw = rgba.tobytes("raw", "RGBA")
            qimg = QImage(raw, w, h, w * 4, QImage.Format.Format_RGBA8888)
            pix = QPixmap.fromImage(qimg).scaled(200, 200,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation)
            self.image_label.setPixmap(pix)
            self.image_label.setText("")
        except Exception:
            self.image_label.setPixmap(QPixmap())
            self.image_label.setText("preview failed")

    def set_tim(self, tim_path: Path | None, palette_index: int = 0):
        if tim_path and tim_path.exists():
            pix = _tim_to_pixmap(tim_path, palette_index)
            if pix:
                self.image_label.setPixmap(pix)
                self.image_label.setText("")
                return
            self.image_label.setPixmap(QPixmap())
            self.image_label.setText("TIM decode failed")
        else:
            self.image_label.setPixmap(QPixmap())
            self.image_label.setText(self._empty_text if tim_path is None else "TIM not found")

    def set_empty_text(self, text: str):
        self._empty_text = text

    def set_meta(self, text: str):
        self.meta_bar.setText(text)

    def set_header(self, text: str):
        self.header.setText(text)

    # ---- drag/drop and click (interactive mode only) ----

    def dragEnterEvent(self, event):
        if not self._interactive:
            return
        urls = event.mimeData().urls()
        if urls and Path(urls[0].toLocalFile()).suffix.lower() in SUPPORTED_IMPORT_EXTS:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        if not self._interactive:
            return
        urls = event.mimeData().urls()
        if urls:
            self.file_selected.emit(Path(urls[0].toLocalFile()))

    def mousePressEvent(self, event):
        if not self._interactive:
            super().mousePressEvent(event)
            return
        if event.button() == Qt.MouseButton.LeftButton:
            path, _ = QFileDialog.getOpenFileName(
                self, "Open replacement image", "",
                "Images (*.png *.gif *.tim);;All files (*)"
            )
            if path:
                self.file_selected.emit(Path(path))


# ---------------------------------------------------------------------------
# Palette swatch strip
# ---------------------------------------------------------------------------
class PaletteStrip(QWidget):
    def __init__(self, label: str, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(3)

        self._base_label = label
        self.label = QLabel(label)
        self.label.setStyleSheet("font-size:10px; color:#6B8AA0;")
        layout.addWidget(self.label)

        self.swatch_area = QWidget()
        self.swatch_layout = QGridLayout(self.swatch_area)
        self.swatch_layout.setContentsMargins(0, 0, 0, 0)
        self.swatch_layout.setSpacing(2)
        layout.addWidget(self.swatch_area)
        layout.addStretch()

    def set_colors(self, colors: list[tuple[int, int, int]], count_label: str = ""):
        while self.swatch_layout.count():
            w = self.swatch_layout.takeAt(0).widget()
            if w:
                w.deleteLater()
        self.label.setText(f"{self._base_label} ({count_label})")
        cols = 16
        for i, (r, g, b) in enumerate(colors[:256]):
            # BGR555 pure black (0x0000) = transparent / unused slot — show as pink
            if r == 0 and g == 0 and b == 0:
                r, g, b = 220, 20, 120
            swatch = QLabel()
            swatch.setFixedSize(13, 13)
            swatch.setStyleSheet(
                f"background: rgb({r},{g},{b});"
                f"border: 0.5px solid rgba(0,0,0,0.15); border-radius:2px;"
            )
            self.swatch_layout.addWidget(swatch, i // cols, i % cols)

    def clear(self):
        while self.swatch_layout.count():
            w = self.swatch_layout.takeAt(0).widget()
            if w:
                w.deleteLater()
        self.label.setText(f"{self._base_label} — no file loaded")


# ---------------------------------------------------------------------------
# Text preview widget
# ---------------------------------------------------------------------------
class TextViewWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.header = QLabel("text view")
        self.header.setStyleSheet(
            "padding:6px 10px; font-size:11px; font-weight:500; color:#6B8AA0;"
            "border-bottom:1px solid #A8BCCF; text-transform:uppercase; background:#E8EFF5;"
        )
        layout.addWidget(self.header)

        self._text_edit = QPlainTextEdit()
        self._text_edit.setReadOnly(True)
        self._text_edit.setFont(QFont("Courier New", 10))
        self._text_edit.setStyleSheet(
            "QPlainTextEdit { background:#1E2E3C; color:#ECEFF1; border:none; }"
        )
        layout.addWidget(self._text_edit)

    def show_bytes(self, data: bytes, name: str):
        self.header.setText(f"text — {name}")
        self._text_edit.setPlainText(data.decode('latin-1'))

    def show_file(self, path: Path | None, name: str, full_id: str = ""):
        self.header.setText(f"text — {name}")
        if path is None or not path.exists():
            self._text_edit.setPlainText(f"[file not found: {full_id or name}]")
            return
        try:
            self._text_edit.setPlainText(path.read_bytes().decode('latin-1'))
        except Exception as e:
            self._text_edit.setPlainText(f"[read error: {e}]")


# ---------------------------------------------------------------------------
# Hex editor widget
# ---------------------------------------------------------------------------
class HexViewWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.header = QLabel("hex view")
        self.header.setStyleSheet(
            "padding:6px 10px; font-size:11px; font-weight:500; color:#6B8AA0;"
            "border-bottom:1px solid #A8BCCF; text-transform:uppercase; background:#E8EFF5;"
        )
        layout.addWidget(self.header)

        ctrl_bar = QWidget()
        ctrl_bar.setStyleSheet("background:#D5E0EA; border-bottom:1px solid #A8BCCF;")
        ctrl_layout = QHBoxLayout(ctrl_bar)
        ctrl_layout.setContentsMargins(8, 3, 8, 3)
        ctrl_layout.setSpacing(6)
        _lbl = QLabel("bytes per row:")
        _lbl.setStyleSheet("font-size:11px; color:#3D5A72;")
        ctrl_layout.addWidget(_lbl)
        self._cols_spin = QSpinBox()
        self._cols_spin.setRange(4, 64)
        self._cols_spin.setValue(16)
        self._cols_spin.setSingleStep(4)
        self._cols_spin.setFixedWidth(56)
        self._cols_spin.setStyleSheet("font-size:11px;")
        ctrl_layout.addWidget(self._cols_spin)
        ctrl_layout.addStretch()
        layout.addWidget(ctrl_bar)

        self._hex_edit = QPlainTextEdit()
        self._hex_edit.setReadOnly(True)
        self._hex_edit.setFont(QFont("Courier New", 9))
        self._hex_edit.setStyleSheet(
            "QPlainTextEdit { background:#1E2E3C; color:#ECEFF1; border:none; }"
        )
        layout.addWidget(self._hex_edit)

        self._data: bytes = b''
        self._cols_spin.valueChanged.connect(self._refresh)

    def show_bytes(self, data: bytes, name: str):
        self.header.setText(f"hex — {name}")
        self._data = data
        self._refresh()

    def show_file(self, path: Path | None, name: str, full_id: str = ""):
        self.header.setText(f"hex — {name}")
        if path is None or not path.exists():
            self._hex_edit.setPlainText(f"[file not found: {full_id or name}]")
            self._data = b''
            return
        try:
            self._data = path.read_bytes()
        except Exception as e:
            self._hex_edit.setPlainText(f"[read error: {e}]")
            self._data = b''
            return
        self._refresh()

    def _refresh(self):
        cols = self._cols_spin.value()
        data = self._data
        lines = []
        for i in range(0, len(data), cols):
            chunk = data[i:i + cols]
            hex_part = ' '.join(f'{b:02X}' for b in chunk).ljust(cols * 3 - 1)
            ascii_part = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
            lines.append(f"{i:08X}  {hex_part}  {ascii_part}")
        self._hex_edit.setPlainText('\n'.join(lines))


# ---------------------------------------------------------------------------
# Detail panel
# ---------------------------------------------------------------------------
class DetailPanel(QWidget):
    replacement_saved = pyqtSignal(int, object, str)        # entry_number, out_path (Path), meta_html
    audio_replacement_saved = pyqtSignal(int, object, str)  # entry_number, out_path (Path), meta_html

    def __init__(self, parent=None):
        super().__init__(parent)
        self._index_path: Path | None = None
        self._bin_path: Path | None = None
        self._current_entry: IndexEntry | None = None
        self._current_tim_info: TimInfo | None = None
        self._current_audio_info: AudioInfo | None = None
        self._repl_meta_cache: dict[int, str] = {}
        self._audio_repl_meta_cache: dict[int, str] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._stack = QStackedWidget()
        layout.addWidget(self._stack)

        # ---- Page 0: image / audio view ----
        _image_page = QWidget()
        _ip_layout = QVBoxLayout(_image_page)
        _ip_layout.setContentsMargins(0, 0, 0, 0)
        _ip_layout.setSpacing(0)
        self._image_layout = _ip_layout

        top_widget = QWidget()
        top_layout = QHBoxLayout(top_widget)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(0)

        self.orig_panel = ImagePanel("original")
        self.repl_panel = ImagePanel(
            "replacement — no image",
            empty_text="no new image added",
            interactive=True,
        )
        self.repl_panel.file_selected.connect(self._on_repl_file_selected)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.VLine)
        divider.setStyleSheet("color:#A8BCCF;")

        top_layout.addWidget(self.orig_panel)
        top_layout.addWidget(divider)
        top_layout.addWidget(self.repl_panel)
        _ip_layout.addWidget(top_widget)

        h_line = QFrame()
        h_line.setFrameShape(QFrame.Shape.HLine)
        h_line.setStyleSheet("color:#A8BCCF;")
        _ip_layout.addWidget(h_line)

        # Audio playback bar — shown only when an audio entry is selected
        self._audio_bar = QWidget()
        self._audio_bar.setStyleSheet(
            "background:#D5E0EA; border-bottom:1px solid #A8BCCF;"
        )
        ab = QHBoxLayout(self._audio_bar)
        ab.setContentsMargins(10, 5, 10, 5)
        ab.setSpacing(8)
        self._play_orig_btn = QPushButton("▶  Play original")
        self._play_repl_btn = QPushButton("▶  Play replacement")
        self._stop_btn      = QPushButton("■  Stop")
        for _b in (self._play_orig_btn, self._play_repl_btn, self._stop_btn):
            _b.setFixedHeight(24)
            _b.setStyleSheet(
                "QPushButton { font-size:11px; padding:0 10px;"
                "background:#4E6B82; color:#ECEFF1; border:none; border-radius:3px; }"
                "QPushButton:hover { background:#5C7D96; }"
                "QPushButton:disabled { background:#A8BCCF; color:#D5E0EA; }"
            )
        self._stop_btn.setEnabled(False)
        self._play_repl_btn.setEnabled(False)
        ab.addWidget(self._play_orig_btn)
        ab.addWidget(self._play_repl_btn)
        ab.addStretch()
        ab.addWidget(self._stop_btn)
        self._audio_bar.hide()
        _ip_layout.addWidget(self._audio_bar)

        self._play_timer = QTimer(self)
        self._play_timer.setInterval(250)
        self._play_timer.timeout.connect(self._poll_playback)
        self._play_orig_btn.clicked.connect(self._play_original_audio)
        self._play_repl_btn.clicked.connect(self._play_replacement_audio)
        self._stop_btn.clicked.connect(self._stop_audio)

        palette_header = QLabel("clut / palette")
        palette_header.setStyleSheet(
            "padding:6px 10px; font-size:11px; font-weight:500; color:#6B8AA0;"
            "border-bottom:1px solid #A8BCCF; text-transform:uppercase; background:#E8EFF5;"
        )
        _ip_layout.addWidget(palette_header)

        palette_row = QWidget()
        pr_layout = QHBoxLayout(palette_row)
        pr_layout.setContentsMargins(0, 0, 0, 0)
        pr_layout.setSpacing(0)

        self.orig_palette = PaletteStrip("original")
        self.repl_palette = PaletteStrip("replacement")
        # Combo to select palette/frame variant for the original TIM
        self._orig_variant_combo: QComboBox | None = None

        pal_div = QFrame()
        pal_div.setFrameShape(QFrame.Shape.VLine)
        pal_div.setStyleSheet("color:#A8BCCF;")

        pr_layout.addWidget(self.orig_palette)
        pr_layout.addWidget(pal_div)
        pr_layout.addWidget(self.repl_palette)
        _ip_layout.addWidget(palette_row)

        self.validation_label = QLabel("select an image entry to begin")
        self.validation_label.setTextFormat(Qt.TextFormat.RichText)
        self.validation_label.setStyleSheet(
            "padding:6px 10px; font-size:11px; color:#6B8AA0;"
            "border-top:1px solid #A8BCCF; background:#E8EFF5;"
        )
        self.validation_label.setWordWrap(True)
        _ip_layout.addWidget(self.validation_label)
        _ip_layout.addStretch()

        self._stack.addWidget(_image_page)   # index 0

        # ---- Page 1: text view ----
        self._text_view = TextViewWidget()
        self._stack.addWidget(self._text_view)  # index 1

        # ---- Page 2: hex view ----
        self._hex_view = HexViewWidget()
        self._stack.addWidget(self._hex_view)   # index 2

    def set_index_path(self, p: Path):
        self._index_path = p

    def set_bin_path(self, p: Path):
        self._bin_path = p

    # ---- audio playback ----

    def _init_mixer(self) -> bool:
        try:
            if not _pg_mixer.get_init():
                _pg_mixer.init()
            return True
        except Exception:
            return False

    def _play_audio(self, path: Path):
        if not self._init_mixer():
            return
        try:
            _pg_mixer.music.load(str(path))
            _pg_mixer.music.play()
            self._stop_btn.setEnabled(True)
            self._play_timer.start()
        except Exception:
            pass

    def _stop_audio(self):
        try:
            if _pg_mixer.get_init():
                _pg_mixer.music.stop()
        except Exception:
            pass
        self._play_timer.stop()
        self._stop_btn.setEnabled(False)

    def _poll_playback(self):
        try:
            if not _pg_mixer.get_init() or not _pg_mixer.music.get_busy():
                self._play_timer.stop()
                self._stop_btn.setEnabled(False)
        except Exception:
            self._play_timer.stop()

    def _play_original_audio(self):
        entry = self._current_entry
        if entry and entry.wav_path and Path(entry.wav_path).exists():
            self._play_audio(Path(entry.wav_path))

    def _play_replacement_audio(self):
        entry = self._current_entry
        if entry:
            repl_wav = NEW_AUDIO_DIR / (entry.full_id + ".wav")
            if repl_wav.exists():
                self._play_audio(repl_wav)

    def show_entry(self, entry: IndexEntry):
        if _entry_is_text(entry):
            self.show_text_entry(entry)
            return
        if _entry_is_hex(entry):
            self.show_hex_entry(entry)
            return
        if entry.is_audio:
            self.show_audio_entry(entry)
            return

        self._stack.setCurrentIndex(0)
        self._stop_audio()
        self._audio_bar.hide()
        self.orig_panel.set_empty_text("no image")
        self.repl_panel.set_empty_text("no new image added")
        self._current_entry = entry
        self._current_audio_info = None
        self.orig_panel.set_header(f"original — {entry.name}")

        # Set up variant combo when multiple TIM variants exist
        tim_paths = getattr(entry, 'tim_paths', None) or []
        if len(tim_paths) > 1:
            self._ensure_variant_combo()
            self._populate_variant_combo(tim_paths)
            sel = self._orig_variant_combo.currentIndex() if self._orig_variant_combo else 0
            tim_path = tim_paths[sel] if sel < len(tim_paths) else entry.tim_path
        else:
            if self._orig_variant_combo:
                self._orig_variant_combo.clear()
            tim_path = entry.tim_path

        # Attempt per-item TIM extraction if not yet present
        if tim_path is None and self._index_path:
            tim_path = extract_tim_for_entry(self._index_path, entry, EXISTING_FILES_DIR)
            entry.tim_path = tim_path

        self._current_tim_info = None
        if tim_path and tim_path.exists():
            tim = parse_tim(tim_path)
            if tim:
                self._current_tim_info = tim
                self.orig_panel.set_tim(tim_path)
                clut_note = "" if tim.has_clut else "  (ext. CLUT — grayscale preview)"
                meta = (
                    f"size {tim.image_width}×{tim.image_height}  "
                    f"bpp {tim.bpp}  "
                    f"clut {tim.clut_width}×{tim.clut_height}  "
                    f"dx,dy {tim.pixel_x},{tim.pixel_y}  "
                    f"cx,cy {tim.clut_x},{tim.clut_y}"
                    f"{clut_note}"
                )
                self.orig_panel.set_meta(meta)
                self._load_tim_palette(tim_path, tim)
            else:
                self.orig_panel.set_tim(None)
                self.orig_panel.set_meta(f"(TIM parse failed) {entry.details}")
                self.orig_palette.clear()
        else:
            self.orig_panel.set_tim(None)
            self.orig_panel.set_meta(entry.details or "TIM not extracted")
            self.orig_palette.clear()

        repl_tim = NEW_FILES_DIR / (entry.full_id + ".tim")
        if repl_tim.exists():
            self.repl_panel.set_header(f"replacement — {repl_tim.name}")
            self.repl_panel.set_tim(repl_tim)
            cached_meta = self._repl_meta_cache.get(entry.number, "")
            self.repl_panel.set_meta(
                cached_meta or "previously imported — click or drop to replace"
            )
            repl_info = parse_tim(repl_tim)
            if repl_info and repl_info.has_clut:
                self._load_repl_palette(repl_tim, repl_info)
            else:
                self.repl_palette.clear()
            self.validation_label.setText(
                f'<span style="color:#1D9E75">&#9679; saved — {repl_tim.name}</span>'
            )
        else:
            self.repl_panel.set_header("replacement — no image")
            self.repl_panel.set_tim(None)
            if entry.width:
                self.repl_panel.set_meta(
                    f"required: {entry.width}×{entry.height}"
                    + (f"  ·  {entry.palette_count} palette(s)" if entry.palette_count else "")
                    + "  ·  click or drop file to import"
                )
            else:
                self.repl_panel.set_meta("click or drop a PNG/GIF/TIM to import")
            self.repl_palette.clear()
            self.validation_label.setText(
                '<span style="color:#aaa">drop a PNG, GIF, or TIM onto the replacement panel '
                "— or click it to browse</span>"
            )

    def _load_tim_palette(self, tim_path: Path, tim_info):
        try:
            data = tim_path.read_bytes()
            # CLUT block starts at byte 8: 4 byte block_len, then x,y,w,h (2 bytes each)
            # entries start at offset 8+8 = 16
            clut_offset = 16
            n = tim_info.clut_width * tim_info.clut_height
            colors = []
            for i in range(min(n, 256)):
                word = struct.unpack_from("<H", data, clut_offset + i * 2)[0]
                r = (word & 0x1F) << 3
                g = ((word >> 5) & 0x1F) << 3
                b = ((word >> 10) & 0x1F) << 3
                colors.append((r, g, b))
            self.orig_palette.set_colors(colors, f"{n} entries")
        except Exception as e:
            self.orig_palette.clear()

    def _ensure_variant_combo(self):
        if self._orig_variant_combo is None:
            self._orig_variant_combo = QComboBox()
            self._orig_variant_combo.setMaximumWidth(220)
            self._orig_variant_combo.currentIndexChanged.connect(self._on_variant_changed)
            # Place combo under the image panels (insert into image page layout)
            self._image_layout.insertWidget(1, self._orig_variant_combo)

    def _populate_variant_combo(self, png_paths: list[Path]):
        if not self._orig_variant_combo:
            return
        self._orig_variant_combo.blockSignals(True)
        self._orig_variant_combo.clear()
        for p in png_paths:
            self._orig_variant_combo.addItem(p.name)
        self._orig_variant_combo.setCurrentIndex(0)
        self._orig_variant_combo.blockSignals(False)

    def _on_variant_changed(self, idx: int):
        entry = self._current_entry
        if not entry:
            return

        tim_paths = getattr(entry, 'tim_paths', None) or []
        tim_path = tim_paths[idx] if idx < len(tim_paths) else entry.tim_path

        if tim_path and tim_path.exists():
            tim = parse_tim(tim_path)
            if tim:
                self.orig_panel.set_tim(tim_path)
                clut_note = "" if tim.has_clut else "  (ext. CLUT — grayscale preview)"
                meta = (
                    f"size {tim.image_width}×{tim.image_height}  "
                    f"bpp {tim.bpp}  "
                    f"clut {tim.clut_width}×{tim.clut_height}  "
                    f"dx,dy {tim.pixel_x},{tim.pixel_y}  "
                    f"cx,cy {tim.clut_x},{tim.clut_y}"
                    f"{clut_note}"
                )
                self.orig_panel.set_meta(meta)
                self._load_tim_palette(tim_path, tim)
                return

        self.orig_panel.set_tim(None)
        self.orig_panel.set_meta(entry.details or "TIM not extracted")
        self.orig_palette.clear()

    def _on_replacement_selected(self, path: Path):
        entry = self._current_entry
        ref = self._current_tim_info
        if entry is None:
            return

        info = parse_import_file(path)
        if info is None:
            self.validation_label.setText(
                '<span style="color:#c00">&#9679; Could not parse file.</span>'
            )
            return

        if ref is None:
            self.validation_label.setText(
                '<span style="color:#c00">&#9679; No original TIM info — cannot encode.</span>'
            )
            return

        # Build validation checks HTML (used in meta bar and saved to project)
        checks = []

        def _ok(label):
            return f'<span style="color:#1D9E75">&#9679; {label}</span>'

        def _warn(label):
            return f'<span style="color:#c00">&#9679; {label}</span>'

        rw, rh = ref.image_width, ref.image_height
        if info.width == rw and info.height == rh:
            checks.append(_ok(f"size {info.width}×{info.height}"))
        else:
            checks.append(_warn(
                f"size {info.width}×{info.height} → resized to {rw}×{rh}"
            ))

        if ref.bpp in (4, 8):
            max_colors = 16 if ref.bpp == 4 else 256
            if info.num_colors == 0:
                checks.append(_ok(f"colour: direct → quantized to {max_colors}"))
            elif info.num_colors <= max_colors:
                checks.append(_ok(f"palette: {info.num_colors} colours (max {max_colors})"))
            else:
                checks.append(_warn(
                    f"palette: {info.num_colors} colours → reduced to {max_colors}"
                ))
        else:
            depth_label = f"{info.bpp_detected}bpp" if info.num_colors == 0 else f"{info.num_colors} colours"
            checks.append(_ok(f"colour: {depth_label}"))

        checks.append(
            f'<span style="color:#888">&#9679; dx,dy {ref.pixel_x},{ref.pixel_y}  '
            f'cx,cy {ref.clut_x},{ref.clut_y}  bpp {ref.bpp} — auto-matched from original</span>'
        )

        meta_html = "  ·  ".join(checks)

        # Encode to TIM
        rel = entry.full_id + ".tim"
        out_path = NEW_FILES_DIR / rel
        ok, msg = encode_to_tim(path, ref, out_path)
        if ok:
            self.repl_panel.set_header(f"replacement — {out_path.name}")
            self.repl_panel.set_tim(out_path)  # show the actual encoded (resized) TIM
            self.repl_panel.set_meta(meta_html)
            self.validation_label.setText(
                f'<span style="color:#1D9E75">&#9679; saved → {out_path.name}</span>'
            )
            new_tim = parse_tim(out_path)
            if new_tim and new_tim.has_clut:
                self._load_repl_palette(out_path, new_tim)
            self._current_entry.replacement_gif = path
            self._repl_meta_cache[entry.number] = meta_html
            self.replacement_saved.emit(entry.number, out_path, meta_html)
        else:
            # Show original on failure so user sees something
            if info.file_type == "TIM":
                self.repl_panel.set_tim(path)
            else:
                try:
                    from PIL import Image as _PIL
                    self.repl_panel.set_pil_image(_PIL.open(str(path)))
                except Exception:
                    self.repl_panel.set_image(None)
            self.repl_panel.set_meta(meta_html)
            self.validation_label.setText(
                f'<span style="color:#c00">&#9679; encode failed: {msg}</span>'
            )

    def _on_repl_file_selected(self, path: Path):
        if self._current_entry and self._current_entry.is_audio:
            self._on_audio_replacement_selected(path)
        else:
            self._on_replacement_selected(path)

    def show_audio_entry(self, entry: IndexEntry):
        self._stack.setCurrentIndex(0)
        self._stop_audio()
        self._audio_bar.show()
        self.orig_panel.set_empty_text("no audio")
        self.repl_panel.set_empty_text("no audio")
        self._current_entry = entry
        self._current_tim_info = None
        self._current_audio_info = None

        # Original audio panel
        self.orig_panel.set_header(f"original — {entry.name}")
        wav_path = entry.wav_path
        if wav_path and wav_path.exists():
            info = parse_wav_info(wav_path)
            self._current_audio_info = info
            pix = _wav_to_pixmap(wav_path)
            if pix:
                self.orig_panel.image_label.setPixmap(
                    pix.scaled(200, 200, Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.FastTransformation)
                )
                self.orig_panel.image_label.setText("")
            else:
                self.orig_panel.image_label.setPixmap(QPixmap())
                self.orig_panel.image_label.setText("no audio")
            if info:
                ch = "stereo" if info.channels == 2 else "mono"
                self.orig_panel.set_meta(
                    f"{info.sample_rate} Hz  ·  {info.bit_depth}-bit  ·  {ch}  ·  "
                    f"{info.duration_str}  ·  {info.num_frames:,} frames"
                )
            else:
                self.orig_panel.set_meta(wav_path.name)
        else:
            self.orig_panel.image_label.setPixmap(QPixmap())
            self.orig_panel.image_label.setText("no audio extracted")
            hint = ""
            if entry.audio_sample_rate:
                hint = f"{entry.audio_sample_rate} Hz"
            if entry.audio_channels:
                hint += f"  {'stereo' if entry.audio_channels == 2 else 'mono'}"
            self.orig_panel.set_meta(hint or entry.details or "")

        # Palette strip not relevant for audio
        self.orig_palette.clear()

        # Replacement audio panel
        repl_wav = NEW_AUDIO_DIR / (entry.full_id + ".wav")
        if repl_wav.exists():
            pix = _wav_to_pixmap(repl_wav)
            if pix:
                self.repl_panel.image_label.setPixmap(
                    pix.scaled(200, 200, Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.FastTransformation)
                )
                self.repl_panel.image_label.setText("")
            else:
                self.repl_panel.image_label.setPixmap(QPixmap())
                self.repl_panel.image_label.setText("no audio")
            self.repl_panel.set_header(f"replacement — {repl_wav.name}")
            cached_meta = self._audio_repl_meta_cache.get(entry.number, "")
            self.repl_panel.set_meta(
                cached_meta or "previously imported — click or drop to replace"
            )
            self.repl_palette.clear()
            self.validation_label.setText(
                f'<span style="color:#1D9E75">&#9679; saved — {repl_wav.name}</span>'
            )
        else:
            self.repl_panel.set_header("replacement — no audio")
            self.repl_panel.image_label.setPixmap(QPixmap())
            self.repl_panel.image_label.setText("no audio")
            ref_hint = ""
            if self._current_audio_info:
                a = self._current_audio_info
                ch = "stereo" if a.channels == 2 else "mono"
                ref_hint = f"required: {a.sample_rate} Hz · {a.bit_depth}-bit · {ch} · ≤{a.duration_str}"
            self.repl_panel.set_meta(ref_hint or "click or drop a WAV to import")
            self.repl_palette.clear()
            self.validation_label.setText(
                '<span style="color:#aaa">drop a WAV onto the replacement panel '
                "— or click it to browse</span>"
            )

        # Update playback button states
        self._play_orig_btn.setEnabled(bool(wav_path and wav_path.exists()))
        self._play_repl_btn.setEnabled(repl_wav.exists())

    def _on_audio_replacement_selected(self, path: Path):
        entry = self._current_entry
        ref = self._current_audio_info
        if entry is None:
            return

        if path.suffix.lower() not in AUDIO_IMPORT_EXTS:
            self.validation_label.setText(
                '<span style="color:#c00">&#9679; Only WAV files are supported for audio replacement.</span>'
            )
            return

        if ref is None:
            # Try to parse from the extracted WAV on disk
            if entry.wav_path and entry.wav_path.exists():
                ref = parse_wav_info(entry.wav_path)
                self._current_audio_info = ref

        src = parse_wav_info(path)
        if src is None:
            self.validation_label.setText(
                '<span style="color:#c00">&#9679; Could not read WAV file.</span>'
            )
            return

        checks = []

        def _ok(label):
            return f'<span style="color:#1D9E75">&#9679; {label}</span>'

        def _warn(label):
            return f'<span style="color:#c00">&#9679; {label}</span>'

        def _note(label):
            return f'<span style="color:#888">&#9679; {label}</span>'

        ch_str = "stereo" if src.channels == 2 else "mono"
        checks.append(_ok(f"{src.sample_rate} Hz  ·  {src.bit_depth}-bit  ·  {ch_str}  ·  {src.duration_str}"))

        if ref:
            if src.sample_rate != ref.sample_rate:
                checks.append(_warn(f"sample rate {src.sample_rate} Hz ≠ {ref.sample_rate} Hz — must match"))
            else:
                checks.append(_ok(f"sample rate matches ({ref.sample_rate} Hz)"))

            if src.channels != ref.channels:
                ref_ch = "stereo" if ref.channels == 2 else "mono"
                checks.append(_warn(f"channels: {ch_str} ≠ {ref_ch} — must match"))
            else:
                checks.append(_ok(f"channels match ({ch_str})"))

            if src.num_frames > ref.num_frames:
                over = (src.num_frames - ref.num_frames) / ref.sample_rate
                checks.append(_warn(f"too long by {over:.2f}s — trim before importing"))
            elif src.num_frames < ref.num_frames:
                pad = (ref.num_frames - src.num_frames) / ref.sample_rate
                checks.append(_note(f"short by {pad:.2f}s — will be padded with silence"))
            else:
                checks.append(_ok("duration matches exactly"))

            if src.bit_depth > 16:
                checks.append(_note(f"bit depth {src.bit_depth} → clamped to 16-bit"))
            elif src.bit_depth < 16:
                checks.append(_note(f"bit depth {src.bit_depth} → expanded to 16-bit"))
            else:
                checks.append(_ok("16-bit depth (ideal)"))
        else:
            checks.append(_note("no reference audio — proceeding without comparison"))

        meta_html = "  ·  ".join(checks)

        if ref and (src.sample_rate != ref.sample_rate or src.channels != ref.channels
                    or src.num_frames > ref.num_frames):
            self.repl_panel.set_meta(meta_html)
            self.validation_label.setText(
                '<span style="color:#c00">&#9679; Fix the issues above before importing.</span>'
            )
            return

        # Proceed with encode
        rel = entry.full_id + ".wav"
        out_path = NEW_AUDIO_DIR / rel
        ok, msg, result_info = validate_and_encode_audio(path, ref or src, out_path)

        if ok:
            pix = _wav_to_pixmap(out_path)
            if pix:
                self.repl_panel.image_label.setPixmap(
                    pix.scaled(200, 200, Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.FastTransformation)
                )
                self.repl_panel.image_label.setText("")
            self.repl_panel.set_header(f"replacement — {out_path.name}")
            self.repl_panel.set_meta(meta_html)
            self.validation_label.setText(
                f'<span style="color:#1D9E75">&#9679; saved → {out_path.name}'
                + (f" ({msg})" if msg != "ok" else "")
                + "</span>"
            )
            self._audio_repl_meta_cache[entry.number] = meta_html
            self._play_repl_btn.setEnabled(True)
            self.audio_replacement_saved.emit(entry.number, out_path, meta_html)
        else:
            self.repl_panel.set_meta(meta_html)
            self.validation_label.setText(
                f'<span style="color:#c00">&#9679; {msg}</span>'
            )

    def _load_repl_palette(self, tim_path: Path, tim_info: TimInfo):
        try:
            data = tim_path.read_bytes()
            clut_offset = 20  # 8 global header + 12 clut block header
            n = tim_info.clut_width * tim_info.clut_height
            colors = []
            for i in range(min(n, 256)):
                word = struct.unpack_from("<H", data, clut_offset + i * 2)[0]
                r = (word & 0x1F) << 3
                g = ((word >> 5) & 0x1F) << 3
                b = ((word >> 10) & 0x1F) << 3
                colors.append((r, g, b))
            self.repl_palette.set_colors(colors, f"{n} entries")
        except Exception:
            self.repl_palette.clear()

    def _resolve_entry_file(self, entry: IndexEntry) -> Path | None:
        candidates = []
        if entry.full_id:
            candidates.append(EXISTING_FILES_DIR / entry.full_id)
        if entry.name:
            candidates.append(EXISTING_FILES_DIR / entry.name)
        for p in candidates:
            if p.exists() and p.is_file():
                return p
        # Recursive search by filename
        if entry.full_id:
            fname = Path(entry.full_id).name
            for p in EXISTING_FILES_DIR.rglob(fname):
                return p
        if entry.name:
            for p in EXISTING_FILES_DIR.rglob(entry.name):
                if p.is_file():
                    return p
        return None

    def _load_entry_bytes(self, entry: IndexEntry) -> bytes | None:
        path = self._resolve_entry_file(entry)
        if path is not None:
            try:
                return path.read_bytes()
            except Exception:
                pass
        if self._bin_path:
            size = 0
            try:
                size = int(entry.details)
            except (TypeError, ValueError):
                pass
            return _read_bin_sectors(self._bin_path, entry.sector_start, entry.sector_end, size)
        return None

    def show_text_entry(self, entry: IndexEntry):
        self._stop_audio()
        self._current_entry = entry
        data = self._load_entry_bytes(entry)
        if data is not None:
            self._text_view.show_bytes(data, entry.name)
        else:
            self._text_view.show_file(None, entry.name, entry.full_id or '')
        self._stack.setCurrentIndex(1)

    def show_hex_entry(self, entry: IndexEntry):
        self._stop_audio()
        self._current_entry = entry
        data = self._load_entry_bytes(entry)
        if data is not None:
            self._hex_view.show_bytes(data, entry.name)
        else:
            self._hex_view.show_file(None, entry.name, entry.full_id or '')
        self._stack.setCurrentIndex(2)

    def clear(self):
        self._stack.setCurrentIndex(0)
        self._stop_audio()
        self._audio_bar.hide()
        self._current_tim_info = None
        self._current_audio_info = None
        self.orig_panel.set_empty_text("no image")
        self.repl_panel.set_empty_text("no new image added")
        self.orig_panel.set_tim(None)
        self.orig_panel.set_meta("")
        self.orig_panel.set_header("original")
        self.repl_panel.image_label.setPixmap(QPixmap())
        self.repl_panel.image_label.setText("no new image added")
        self.repl_panel.set_meta("")
        self.repl_panel.set_header("replacement — no image")
        self.orig_palette.clear()
        self.repl_palette.clear()
        self.validation_label.setText("select an entry to begin")


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PSXmod")
        self.resize(1100, 680)
        self.project = Project()
        self.entries: list[IndexEntry] = []
        self._worker: IndexWorker | None = None
        self._build_worker: BuildWorker | None = None
        self._audio_extract_worker: AudioExtractWorker | None = None
        self._build_ui()
        self._auto_load_recent_project()

    def _build_ui(self):
        toolbar = QWidget()
        toolbar.setStyleSheet("background:#4E6B82; border-bottom:1px solid #3D5567;")
        tb_layout = QHBoxLayout(toolbar)
        tb_layout.setContentsMargins(10, 6, 10, 6)
        tb_layout.setSpacing(6)

        self.app_label = QLabel("PSXmod")
        self.app_label.setStyleSheet("font-size:14px; font-weight:600; color:#ECEFF1;")
        tb_layout.addWidget(self.app_label)

        # BIN open button (hidden after a BIN is loaded)
        self.open_bin_btn = QPushButton("open BIN")
        self.open_bin_btn.setStyleSheet(self._btn_style())
        self.open_bin_btn.clicked.connect(self._on_open_bin)
        tb_layout.addWidget(self.open_bin_btn)

        # Full path label shown after BIN is loaded
        self.bin_label = QLabel()
        self.bin_label.setStyleSheet(
            "font-size:11px; color:#ECEFF1; padding:3px 8px;"
            "background:rgba(0,0,0,0.2); border:1px solid rgba(255,255,255,0.2); border-radius:4px;"
        )
        self.bin_label.hide()
        tb_layout.addWidget(self.bin_label)

        tb_layout.addStretch()

        self.open_proj_btn = QPushButton("open project")
        self.open_proj_btn.setStyleSheet(self._btn_style())
        self.open_proj_btn.clicked.connect(self._on_open_project)
        tb_layout.addWidget(self.open_proj_btn)

        self.save_proj_btn = QPushButton("save project")
        self.save_proj_btn.setStyleSheet(self._btn_style())
        self.save_proj_btn.clicked.connect(self._on_save_project)
        self.save_proj_btn.setEnabled(False)
        tb_layout.addWidget(self.save_proj_btn)

        self.extract_checked_btn = QPushButton("extract checked items")
        self.extract_checked_btn.setStyleSheet(self._btn_style())
        self.extract_checked_btn.setEnabled(False)
        self.extract_checked_btn.clicked.connect(self._on_extract_checked)
        tb_layout.addWidget(self.extract_checked_btn)

        self.export_btn = QPushButton("export xdelta")
        self.export_btn.setStyleSheet(self._btn_style())
        self.export_btn.setEnabled(False)
        tb_layout.addWidget(self.export_btn)

        self.build_btn = QPushButton("build patched BIN")
        self.build_btn.setStyleSheet(self._btn_style(primary=True))
        self.build_btn.setEnabled(False)
        self.build_btn.clicked.connect(self._on_build_patched_bin)
        tb_layout.addWidget(self.build_btn)

        central = QWidget()
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        main_layout.addWidget(toolbar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(1)
        splitter.setStyleSheet("QSplitter::handle { background:#A8BCCF; }")

        self.index_panel = IndexPanel()
        self.index_panel.setMinimumWidth(260)
        self.index_panel.tree.currentItemChanged.connect(self._on_entry_selected)
        splitter.addWidget(self.index_panel)

        self.detail_panel = DetailPanel()
        self.detail_panel.replacement_saved.connect(self._on_replacement_saved)
        self.detail_panel.audio_replacement_saved.connect(self._on_audio_replacement_saved)
        splitter.addWidget(self.detail_panel)

        self.index_panel.checked_changed.connect(self._on_checked_changed)

        splitter.setSizes([360, 740])
        main_layout.addWidget(splitter, 1)

        self.setCentralWidget(central)

        self.status = QStatusBar()
        self.status.setStyleSheet(
            "QStatusBar { background:#3D5567; color:#B0C8DC; font-size:11px; }"
        )
        self.setStatusBar(self.status)
        self.status.showMessage("ready — open a BIN file to begin")

    def _btn_style(self, primary=False) -> str:
        if primary:
            return (
                "QPushButton { font-size:12px; padding:4px 12px;"
                "background:#3D7FA3; border:1px solid #2E6080;"
                "color:#FFFFFF; border-radius:4px; }"
                "QPushButton:hover { background:#4A90B8; }"
                "QPushButton:disabled { color:#A8BCCF; border-color:#5C7D96;"
                "background:rgba(255,255,255,0.08); }"
            )
        return (
            "QPushButton { font-size:12px; padding:4px 12px;"
            "background:rgba(255,255,255,0.15); border:1px solid rgba(255,255,255,0.25);"
            "color:#ECEFF1; border-radius:4px; }"
            "QPushButton:hover { background:rgba(255,255,255,0.25); }"
            "QPushButton:disabled { color:#7A9BB5; border-color:rgba(255,255,255,0.1); }"
        )

    # ---- recent project helpers ----

    def _save_recent_project(self, path: Path):
        try:
            RECENT_PROJECT_FILE.write_text(str(path))
        except Exception:
            pass

    def _auto_load_recent_project(self):
        if not RECENT_PROJECT_FILE.exists():
            return
        try:
            path = Path(RECENT_PROJECT_FILE.read_text().strip())
        except Exception:
            return
        if path.exists():
            self._load_project_from_path(path)

    def _load_project_from_path(self, path: Path):
        try:
            self.project = Project.load(path)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not load project:\n{e}")
            return

        idx = self.project.index_path
        ef  = self.project.existing_files_dir
        bin_path = self.project.bin_path

        if bin_path:
            set_workspace(bin_path)      # new assets/index land in the game folder

        if idx and idx.exists() and ef and ef.exists():
            self.detail_panel.set_index_path(idx)
            if bin_path:
                self.detail_panel.set_bin_path(Path(bin_path))
                self.bin_label.setText(str(bin_path))
                self.bin_label.show()
                self.open_bin_btn.hide()
                self.setWindowTitle(f"PSXmod — {bin_path.name}")
            self.save_proj_btn.setEnabled(True)
            self.status.showMessage("Loading index from project…")
            from core.jpsxdec import parse_index_file
            entries = parse_index_file(idx)
            import os, re as _re
            png_map: dict[str, Path] = {}
            tim_map: dict[str, Path] = {}
            wav_map: dict[str, list] = {}
            for root, _, files in os.walk(ef):
                for fname in files:
                    fpath = Path(root) / fname
                    lower = fname.lower()
                    stem = fpath.stem
                    if lower.endswith(".png"):
                        base = _re.sub(r'_p\d+$', '', stem)
                        png_map.setdefault(base, fpath)
                    elif lower.endswith(".tim"):
                        tim_map.setdefault(stem, fpath)
                    elif lower.endswith(".wav"):
                        base = _re.sub(r'_p\d+$', '', stem)
                        wav_map.setdefault(base, []).append(fpath)
                        wav_map.setdefault(stem, []).append(fpath)
            for entry in entries:
                if entry.is_image:
                    entry.png_path = png_map.get(entry.name)
                    entry.tim_path = tim_map.get(entry.name)
                elif entry.is_audio:
                    base_name = _re.sub(r'\[[\d.]+\].*$', '', entry.name)
                    wav_list = wav_map.get(entry.name) or wav_map.get(base_name) or []
                    wav_list = sorted(set(wav_list), key=lambda p: str(p))
                    entry.wav_paths = wav_list
                    entry.wav_path  = wav_list[0] if wav_list else None
            self.entries = entries
            NEW_FILES_DIR.mkdir(parents=True, exist_ok=True)
            NEW_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
            self.detail_panel._repl_meta_cache = dict(self.project.replacement_meta)
            self.detail_panel._audio_repl_meta_cache = dict(self.project.audio_replacement_meta)
            self.index_panel.populate(entries)
            self.index_panel.set_checked_entries(set(self.project.checked_entries))
            self._refresh_extract_btn()
            self._update_build_btn()
            image_count = sum(1 for e in entries if e.is_image)
            self.status.showMessage(
                f"Project loaded: {self.project.name}  —  "
                f"{len(entries)} entries ({image_count} images)"
            )
        else:
            self.status.showMessage(
                f"Project loaded: {self.project.name}  —  "
                f"open a BIN to rebuild the index"
            )

    def _on_open_bin(self):
        if not JPSXDEC_JAR.exists():
            QMessageBox.critical(
                self, "jPSXdec not found",
                f"jpsxdec.jar not found at:\n{JPSXDEC_JAR}\n\nRun INSTALL.sh first."
            )
            return

        path, _ = QFileDialog.getOpenFileName(
            self, "Open BIN file", "", "BIN files (*.bin);;All files (*)"
        )
        if not path:
            return

        bin_path = Path(path)
        set_workspace(bin_path)          # repoint working dirs into APP_DIR/<bin name>/
        self.project.bin_path = bin_path
        self.project.index_path = INDEX_PATH
        self.project.existing_files_dir = EXISTING_FILES_DIR

        # Show full path in toolbar
        self.bin_label.setText(str(bin_path))
        self.bin_label.show()
        self.open_bin_btn.hide()
        self.setWindowTitle(f"PSXmod — {bin_path.name}")

        self.detail_panel.set_index_path(INDEX_PATH)
        self.detail_panel.set_bin_path(bin_path)
        self.status.showMessage("Building index and extracting files…")
        self.detail_panel.clear()
        self.index_panel.tree.clear()

        self._worker = IndexWorker(bin_path, INDEX_PATH, EXISTING_FILES_DIR)
        self._worker.progress.connect(self.status.showMessage)
        self._worker.finished.connect(self._on_index_ready)
        self._worker.start()

    def _on_index_ready(self, entries: list[IndexEntry], error: str):
        if error:
            QMessageBox.critical(self, "Error", error)
            self.status.showMessage("Error during indexing.")
            return

        self.entries = entries
        NEW_FILES_DIR.mkdir(parents=True, exist_ok=True)
        NEW_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        self.index_panel.populate(entries)
        self.index_panel.set_checked_entries(set(self.project.checked_entries))
        self._refresh_extract_btn()
        self.save_proj_btn.setEnabled(True)
        self._update_build_btn()
        image_count = sum(1 for e in entries if e.is_image)
        self.status.showMessage(
            f"Loaded {len(entries)} entries ({image_count} images). "
            "Expand a folder in the index to select an image."
        )

    def _on_entry_selected(self, current, _previous):
        entry = self.index_panel.current_entry()
        if entry and (entry.is_image or entry.is_audio
                      or _entry_is_text(entry) or _entry_is_hex(entry)):
            self.detail_panel.show_entry(entry)
            if entry.is_audio and (not entry.wav_path or not Path(entry.wav_path).exists()):
                self._start_audio_extract(entry)
        else:
            self.detail_panel.clear()

    def _start_audio_extract(self, entry):
        if self._audio_extract_worker and self._audio_extract_worker.isRunning():
            self._audio_extract_worker.finished.disconnect()
        idx = self.project.index_path
        if not idx or not idx.exists():
            return
        self._audio_extract_worker = AudioExtractWorker(idx, entry, EXISTING_FILES_DIR)
        self._audio_extract_worker.finished.connect(self._on_audio_extracted)
        self._audio_extract_worker.start()
        self.status.showMessage(f"Extracting audio: {entry.name}…")

    def _on_audio_extracted(self, entry, ok, msg):
        current = self.index_panel.current_entry()
        if current and current.number == entry.number:
            if ok and entry.wav_path:
                self.detail_panel.show_entry(entry)
                self.status.showMessage(f"Audio loaded: {entry.name}")
            else:
                self.status.showMessage(f"Audio extraction failed: {entry.name} — {msg[:80]}")

    def _on_open_project(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open project", "", "PSXmod project (*.psxmod);;All files (*)"
        )
        if not path:
            return
        self._load_project_from_path(Path(path))
        if self.project.project_path:
            self._save_recent_project(self.project.project_path)

    def _update_build_btn(self):
        has_replacements = bool(self.project.replacements or self.project.audio_replacements)
        self.build_btn.setEnabled(has_replacements)

    def _on_replacement_saved(self, entry_number: int, tim_path: Path, meta_html: str):
        self.project.assign_replacement(entry_number, tim_path)
        self.project.replacement_meta[entry_number] = meta_html
        self.detail_panel._repl_meta_cache[entry_number] = meta_html
        self._update_build_btn()
        if self.project.project_path:
            try:
                self.project.save(self.project.project_path)
                self.status.showMessage(f"auto-saved — {self.project.project_path.name}")
            except Exception as e:
                self.status.showMessage(f"auto-save failed: {e}")

    def _on_build_patched_bin(self):
        if not self.project.bin_path or not Path(self.project.bin_path).exists():
            QMessageBox.critical(self, "BIN not found",
                "Original BIN file is not set or no longer exists.\n"
                "Open the project's BIN file first.")
            return
        if not self.project.replacements:
            QMessageBox.information(self, "Nothing to patch",
                "No image replacements are recorded in this project yet.")
            return

        bin_path = Path(self.project.bin_path)
        default_out = bin_path.with_stem(bin_path.stem + "_patched")
        out_path, _ = QFileDialog.getSaveFileName(
            self, "Save patched BIN", str(default_out),
            "BIN files (*.bin);;All files (*)"
        )
        if not out_path:
            return

        self.build_btn.setEnabled(False)
        self.save_proj_btn.setEnabled(False)
        self.status.showMessage("Building patched BIN…")

        self._build_worker = BuildWorker(self.project, self.entries, Path(out_path))
        self._build_worker.progress.connect(self.status.showMessage)
        self._build_worker.finished.connect(self._on_build_finished)
        self._build_worker.start()

    def _on_build_finished(self, success: int, fail: int, error: str):
        self.save_proj_btn.setEnabled(True)
        self._update_build_btn()
        if error:
            QMessageBox.critical(self, "Build failed", error)
            self.status.showMessage(f"Build failed: {error[:120]}")
        else:
            parts = [f"{success} image(s) patched"]
            if fail:
                parts.append(f"{fail} failed (see log)")
            msg = "Build complete — " + ", ".join(parts)
            QMessageBox.information(self, "Build complete", msg)
            self.status.showMessage(msg)

    def _on_audio_replacement_saved(self, entry_number: int, wav_path: Path, meta_html: str):
        self.project.audio_replacements[entry_number] = str(wav_path)
        self.project.audio_replacement_meta[entry_number] = meta_html
        self.detail_panel._audio_repl_meta_cache[entry_number] = meta_html
        self.project.dirty = True
        self._update_build_btn()
        if self.project.project_path:
            try:
                self.project.save(self.project.project_path)
                self.status.showMessage(f"auto-saved — {self.project.project_path.name}")
            except Exception as e:
                self.status.showMessage(f"auto-save failed: {e}")

    def _on_checked_changed(self, checked: set):
        self.project.checked_entries = sorted(checked)
        self.extract_checked_btn.setEnabled(bool(checked))
        if self.project.project_path:
            try:
                self.project.save(self.project.project_path)
            except Exception:
                pass

    def _refresh_extract_btn(self):
        checked = self.index_panel.get_checked_entries()
        self.extract_checked_btn.setEnabled(bool(checked))

    def _on_extract_checked(self):
        import shutil as _shutil
        try:
            from PIL import Image as _PIL
        except ImportError:
            _PIL = None

        checked = self.index_panel.get_checked_entries()
        to_export = [e for e in self.entries if e.number in checked and e.is_image]
        if not to_export:
            QMessageBox.information(self, "Nothing to export",
                "No image entries are checked.")
            return

        out_dir = TO_MODIFY_DIR
        out_dir.mkdir(parents=True, exist_ok=True)

        exported = 0
        failed = 0
        for entry in to_export:
            png_out = out_dir / (entry.name + ".png")
            if entry.png_path and entry.png_path.exists():
                _shutil.copy2(str(entry.png_path), str(png_out))
                exported += 1
            elif entry.tim_path and entry.tim_path.exists():
                result = decode_tim_to_rgba(entry.tim_path)
                if result and _PIL:
                    w, h, rgba = result
                    try:
                        _PIL.frombytes("RGBA", (w, h), rgba).save(str(png_out))
                        exported += 1
                    except Exception:
                        failed += 1
                else:
                    failed += 1
            else:
                failed += 1

        msg = f"Exported {exported} PNG(s) to TO_MODIFY/"
        if failed:
            msg += f"\n{failed} skipped (TIM not extracted yet)"
        QMessageBox.information(self, "Export complete", msg)
        self.status.showMessage(msg.replace("\n", "  —  "))

    def _on_save_project(self):
        if not self.project.project_path:
            default_proj = str(WORKSPACE_DIR / f"{self.project.name}.psxmod")
            path, _ = QFileDialog.getSaveFileName(
                self, "Save project", default_proj,
                "PSXmod project (*.psxmod)"
            )
            if not path:
                return
            save_path = Path(path)
        else:
            save_path = self.project.project_path
        try:
            self.project.save(save_path)
            self._save_recent_project(save_path)
            self.status.showMessage(f"Project saved: {save_path.name}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not save project:\n{e}")


_GLOBAL_QSS = """
QWidget {
    background-color: #E8EFF5;
    color: #1E2E3C;
    font-family: system-ui, -apple-system, sans-serif;
}
QScrollBar:vertical {
    background: #D5E0EA;
    width: 9px;
    border: none;
}
QScrollBar::handle:vertical {
    background: #90A8C0;
    border-radius: 4px;
    min-height: 20px;
}
QScrollBar::handle:vertical:hover { background: #4E6B82; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal {
    background: #D5E0EA;
    height: 9px;
    border: none;
}
QScrollBar::handle:horizontal {
    background: #90A8C0;
    border-radius: 4px;
    min-width: 20px;
}
QScrollBar::handle:horizontal:hover { background: #4E6B82; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QToolTip {
    background: #263238;
    color: #ECEFF1;
    border: 1px solid #4E6B82;
    padding: 3px 6px;
}
"""


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("PSXmod")
    app.setStyleSheet(_GLOBAL_QSS)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
