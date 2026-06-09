"""
Project file management.
A project is a JSON file that records:
  - the original BIN path
  - the index file path
  - per-entry replacement assignments
"""
import json
from pathlib import Path
from dataclasses import asdict
from typing import Optional


PROJECT_VERSION = 1


class Project:
    def __init__(self):
        self.project_path: Optional[Path] = None
        self.bin_path: Optional[Path] = None
        self.index_path: Optional[Path] = None
        self.existing_files_dir: Optional[Path] = None
        # Maps index entry number -> path of replacement TIM
        self.replacements: dict[int, str] = {}
        # Maps index entry number -> validation HTML for display on reload
        self.replacement_meta: dict[int, str] = {}
        # Audio replacements: entry number -> path of replacement WAV
        self.audio_replacements: dict[int, str] = {}
        # Audio validation HTML for display on reload
        self.audio_replacement_meta: dict[int, str] = {}
        # Entry numbers the user has ticked in the index list
        self.checked_entries: list[int] = []
        self.dirty: bool = False

    @property
    def name(self) -> str:
        if self.project_path:
            return self.project_path.stem
        if self.bin_path:
            return self.bin_path.stem
        return "unsaved project"

    def assign_replacement(self, entry_number: int, gif_path: Path):
        self.replacements[entry_number] = str(gif_path)
        self.dirty = True

    def clear_replacement(self, entry_number: int):
        self.replacements.pop(entry_number, None)
        self.dirty = True

    def save(self, path: Path):
        data = {
            "version": PROJECT_VERSION,
            "bin_path": str(self.bin_path) if self.bin_path else None,
            "index_path": str(self.index_path) if self.index_path else None,
            "existing_files_dir": str(self.existing_files_dir) if self.existing_files_dir else None,
            "replacements": {str(k): v for k, v in self.replacements.items()},
            "replacement_meta": {str(k): v for k, v in self.replacement_meta.items()},
            "audio_replacements": {str(k): v for k, v in self.audio_replacements.items()},
            "audio_replacement_meta": {str(k): v for k, v in self.audio_replacement_meta.items()},
            "checked_entries": sorted(self.checked_entries),
        }
        path.write_text(json.dumps(data, indent=2))
        self.project_path = path
        self.dirty = False

    @classmethod
    def load(cls, path: Path) -> "Project":
        data = json.loads(path.read_text())
        p = cls()
        p.project_path = path
        p.bin_path = Path(data["bin_path"]) if data.get("bin_path") else None
        p.index_path = Path(data["index_path"]) if data.get("index_path") else None
        p.existing_files_dir = Path(data["existing_files_dir"]) if data.get("existing_files_dir") else None
        p.replacements = {int(k): v for k, v in data.get("replacements", {}).items()}
        p.replacement_meta = {int(k): v for k, v in data.get("replacement_meta", {}).items()}
        p.audio_replacements = {int(k): v for k, v in data.get("audio_replacements", {}).items()}
        p.audio_replacement_meta = {int(k): v for k, v in data.get("audio_replacement_meta", {}).items()}
        p.checked_entries = [int(x) for x in data.get("checked_entries", [])]
        p.dirty = False
        return p
