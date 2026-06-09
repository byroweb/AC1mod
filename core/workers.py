"""
QThread workers for long-running jPSXdec operations.
"""
import os
import re
import shutil
import logging
from pathlib import Path
from PyQt6.QtCore import QThread, pyqtSignal
from core.jpsxdec import (
    build_index, extract_all, parse_index_file, IndexEntry, _run,
    save_tim_for_item, extract_audio_for_item, replace_tim, inject_audio_into_bin,
)

# Log to a file next to the script so the user can inspect it
LOG_PATH = Path(__file__).parent.parent / "psxmod_debug.log"
logging.basicConfig(
    filename=str(LOG_PATH),
    filemode='w',
    level=logging.DEBUG,
    format='%(asctime)s %(levelname)s %(message)s',
)
log = logging.getLogger("psxmod")


class IndexWorker(QThread):
    progress = pyqtSignal(str)
    finished = pyqtSignal(list, str)

    def __init__(self, bin_path: Path, index_path: Path, out_dir: Path):
        super().__init__()
        self.bin_path = bin_path
        self.index_path = index_path
        # Normalize to absolute path to avoid cwd surprises
        self.out_dir = Path(out_dir).resolve()

    def run(self):
        log.info(f"=== IndexWorker starting ===")
        log.info(f"BIN:   {self.bin_path}")
        log.info(f"IDX:   {self.index_path}")
        log.info(f"OUTDIR:{self.out_dir}")

        self.progress.emit("Building index…")
        ok, msg = build_index(self.bin_path, self.index_path)
        log.info(f"build_index ok={ok} msg={msg[:200]!r}")
        if not ok:
            self.finished.emit([], f"Index build failed:\n{msg}")
            return

        self.progress.emit("Extracting all files (this may take a while)…")
        ok, msg = extract_all(self.index_path, self.out_dir)
        log.info(f"extract_all ok={ok} msg={msg[:300]!r}")
        if not ok:
            self.progress.emit(f"Warning during extraction: {msg}")

        self.progress.emit("Parsing index…")
        entries = parse_index_file(self.index_path)
        log.info(f"parse_index_file: {len(entries)} entries, "
                 f"{sum(1 for e in entries if e.is_image)} images")

        self.progress.emit("Locating extracted files…")
        self._attach_file_paths(entries)

        # If no TIMs were produced by batch extraction, attempt per-item savetim
        missing_tims = [e for e in entries if e.is_image and not e.tim_path]
        if missing_tims:
            total = len(missing_tims)
            log.info(f"{total} images missing TIMs; running per-item savetim")
            for i, e in enumerate(missing_tims, 1):
                self.progress.emit(f"Saving TIM {i}/{total}: {e.name}…")
                ok, msg = save_tim_for_item(self.index_path, e.number, self.out_dir)
                if not ok:
                    log.debug(f"save_tim_for_item failed for #{e.number}: {msg[:200]!r}")
            # Re-scan output dir to attach newly saved TIM files
            self.progress.emit("Refreshing extracted file list…")
            self._attach_file_paths(entries)

        # Log first 10 image entries and their resolved paths
        img_entries = [e for e in entries if e.is_image][:10]
        for e in img_entries:
            log.info(f"  #{e.number} {e.name!r:25} png={e.png_path}  tim={e.tim_path}")

        # Scan for any already-extracted WAVs (from a previous session)
        self._attach_file_paths(entries)

        self.finished.emit(entries, "")

    def _attach_file_paths(self, entries: list[IndexEntry]):
        """
        Build a lookup table from os.walk (avoids glob bracket-escape issues).
        jPSXdec appends _p00, _p01 etc. for palettes — strip that to get the base name.
        """
        png_map: dict[str, list[Path]] = {}
        tim_map: dict[str, list[Path]] = {}
        wav_map: dict[str, list[Path]] = {}

        file_count = 0
        for root, _dirs, files in os.walk(self.out_dir):
            for fname in files:
                fpath = Path(root) / fname
                lower = fname.lower()
                stem = fpath.stem
                file_count += 1
                if lower.endswith(".png"):
                    base = re.sub(r'_p\d+$', '', stem)
                    png_map.setdefault(base, []).append(fpath)
                elif lower.endswith(".tim"):
                    base = re.sub(r'_p\d+$', '', stem)
                    tim_map.setdefault(base, []).append(fpath)
                    tim_map.setdefault(stem, []).append(fpath)
                elif lower.endswith(".wav"):
                    base = re.sub(r'_p\d+$', '', stem)
                    wav_map.setdefault(base, []).append(fpath)
                    wav_map.setdefault(stem, []).append(fpath)

        log.info(f"os.walk found {file_count} files under {self.out_dir}")
        log.info(f"png_map: {len(png_map)} entries, tim_map: {len(tim_map)} entries")
        if png_map:
            sample = list(png_map.items())[:5]
            for k, v in sample:
                log.info(f"  png_map sample: {k!r} -> {v}")

        for entry in entries:
            if not (entry.is_image or entry.is_audio):
                continue
            base_name = re.sub(r'\[[\d.]+\].*$', '', entry.name)

            png_list = png_map.get(entry.name) or png_map.get(base_name) or []
            tim_list = tim_map.get(entry.name) or tim_map.get(base_name) or []
            wav_list = wav_map.get(entry.name) or wav_map.get(base_name) or []

            png_list = sorted(set(png_list), key=lambda p: str(p))
            tim_list = sorted(set(tim_list), key=lambda p: str(p))
            wav_list = sorted(set(wav_list), key=lambda p: str(p))

            entry.png_paths = png_list
            entry.tim_paths = tim_list
            entry.wav_paths = wav_list
            entry.png_path = png_list[0] if png_list else None
            entry.tim_path = tim_list[0] if tim_list else None
            entry.wav_path = wav_list[0] if wav_list else None


class BuildWorker(QThread):
    """
    Copies the original BIN, builds a fresh index for the copy, then applies
    all image replacements via jPSXdec -replacetim.
    """
    progress = pyqtSignal(str)
    finished = pyqtSignal(int, int, str)   # success_count, fail_count, error_msg

    def __init__(self, project, entries: list, output_bin_path: Path):
        super().__init__()
        self.project = project
        self.entries = entries
        self.output_bin_path = Path(output_bin_path)

    def run(self):
        from pathlib import Path as _Path

        bin_path = self.project.bin_path
        if not bin_path or not _Path(bin_path).exists():
            self.finished.emit(0, 0, f"Original BIN not found: {bin_path}")
            return

        # 1. Copy the BIN
        self.progress.emit(f"Copying {_Path(bin_path).name} → {self.output_bin_path.name}…")
        try:
            shutil.copy2(str(bin_path), str(self.output_bin_path))
        except Exception as e:
            self.finished.emit(0, 0, f"Copy failed: {e}")
            return

        # 2. Build a fresh index pointing at the copy
        temp_idx = self.output_bin_path.with_suffix('.idx')
        self.progress.emit("Building index for patched BIN…")
        ok, msg = build_index(self.output_bin_path, temp_idx)
        log.info(f"BuildWorker build_index ok={ok} msg={msg[:200]!r}")
        if not ok:
            self.finished.emit(0, 0, f"Index build failed:\n{msg}")
            return

        # 3. Apply image replacements
        entry_map = {e.number: e for e in self.entries}
        replacements = self.project.replacements
        total = len(replacements)
        success = 0
        fail = 0

        for i, (entry_num, tim_path_str) in enumerate(replacements.items(), 1):
            tim_path = _Path(tim_path_str)
            entry = entry_map.get(int(entry_num))
            name = entry.name if entry else f"#{entry_num}"
            self.progress.emit(f"Patching image {i}/{total}: {name}…")

            if not tim_path.exists():
                log.warning("BuildWorker: TIM not found: %s", tim_path)
                fail += 1
                continue

            ok, msg = replace_tim(temp_idx, int(entry_num), tim_path, self.output_bin_path)
            log.info(f"  replace_tim #{entry_num} ok={ok}")
            if ok:
                success += 1
            else:
                fail += 1
                self.progress.emit(f"  Warning: {name} failed — {msg[:80]}")

        # 4. Inject audio replacements (XA ADPCM sector overwrite)
        audio_replacements = self.project.audio_replacements
        audio_total = len(audio_replacements)
        for i, (entry_num, wav_path_str) in enumerate(audio_replacements.items(), 1):
            wav_path = _Path(wav_path_str)
            entry    = entry_map.get(int(entry_num))
            name     = entry.name if entry else f"#{entry_num}"
            self.progress.emit(f"Injecting audio {i}/{audio_total}: {name}…")

            if not wav_path.exists():
                log.warning("BuildWorker: WAV not found: %s", wav_path)
                fail += 1
                continue

            if entry is None or entry.sector_start == entry.sector_end == 0:
                log.warning("BuildWorker: no sector info for audio entry #%s", entry_num)
                fail += 1
                continue

            ok, msg = inject_audio_into_bin(
                self.output_bin_path, entry.sector_start, entry.sector_end, wav_path
            )
            log.info(f"  inject_audio #{entry_num} ok={ok} msg={msg[:80]!r}")
            if ok:
                success += 1
            else:
                fail += 1
                self.progress.emit(f"  Warning: {name} audio — {msg[:80]}")

        self.finished.emit(success, fail, "")


class AudioExtractWorker(QThread):
    """Extract a single audio entry's WAV on demand (lazy loading)."""
    finished = pyqtSignal(object, bool, str)  # entry, ok, message

    def __init__(self, index_path: Path, entry, out_dir: Path):
        super().__init__()
        self.index_path = index_path
        self.entry = entry
        self.out_dir = Path(out_dir).resolve()

    def run(self):
        ok, msg = extract_audio_for_item(self.index_path, self.entry.number, self.out_dir)
        if ok:
            # Scan for the newly created WAV and attach it to the entry
            base_name = re.sub(r'\[[\d.]+\].*$', '', self.entry.name)
            wav_map: dict[str, Path] = {}
            for root, _, files in os.walk(self.out_dir):
                for fname in files:
                    if not fname.lower().endswith('.wav'):
                        continue
                    fpath = Path(root) / fname
                    stem = fpath.stem
                    wav_map[stem] = fpath
                    wav_map.setdefault(re.sub(r'_p\d+$', '', stem), fpath)
            wav = wav_map.get(self.entry.name) or wav_map.get(base_name)
            if wav:
                self.entry.wav_path = wav
                self.entry.wav_paths = [wav]
        self.finished.emit(self.entry, ok, msg)
