"""
Run this from the psxmod folder to diagnose extraction paths.
Usage: python3 debug_paths.py
It reads the existing jpsxdec.idx and EXISTING_FILES to show what was found.
"""
import os, sys
from pathlib import Path

APP_DIR = Path(__file__).parent
INDEX_PATH = APP_DIR / "jpsxdec.idx"
EXISTING_FILES = APP_DIR / "EXISTING_FILES"

print("=== PSXmod path diagnostic ===\n")

# 1. Check index exists
print(f"Index file: {INDEX_PATH}")
print(f"  exists: {INDEX_PATH.exists()}")
if INDEX_PATH.exists():
    lines = INDEX_PATH.read_text(errors='replace').splitlines()
    tim_lines = [l for l in lines if 'Type:Tim' in l]
    print(f"  total lines: {len(lines)}")
    print(f"  Tim entries: {len(tim_lines)}")
    if tim_lines:
        print(f"  first Tim line: {tim_lines[0][:120]}")

print()

# 2. Walk EXISTING_FILES and show what's there
print(f"EXISTING_FILES dir: {EXISTING_FILES}")
print(f"  exists: {EXISTING_FILES.exists()}")
if EXISTING_FILES.exists():
    all_files = []
    for root, dirs, files in os.walk(EXISTING_FILES):
        for f in files:
            all_files.append(Path(root) / f)
    print(f"  total files: {len(all_files)}")
    
    pngs = [f for f in all_files if f.suffix.lower() == '.png']
    tims = [f for f in all_files if f.suffix.lower() == '.tim']
    print(f"  .png files: {len(pngs)}")
    print(f"  .tim files: {len(tims)}")
    
    if pngs:
        print(f"\n  First 10 PNGs:")
        for f in pngs[:10]:
            print(f"    {f.relative_to(EXISTING_FILES)}")
    if tims:
        print(f"\n  First 10 TIMs:")
        for f in tims[:10]:
            print(f"    {f.relative_to(EXISTING_FILES)}")
    
    # 3. Try to match a known entry name to show the lookup table
    print(f"\n=== Simulating worker lookup table ===")
    import re
    png_map = {}
    for f in pngs:
        stem = f.stem
        base = re.sub(r'_p\d+$', '', stem)
        png_map.setdefault(base, f)
    
    print(f"  png_map keys (first 10): {list(png_map.keys())[:10]}")
    
    # Try to find RTIM.T[0] or similar
    test_names = ["RTIM.T[0]", "ARMS_T.T[0]", "ARMS_T.T[4]"]
    print(f"\n  Lookup tests:")
    for name in test_names:
        result = png_map.get(name)
        print(f"    {name!r:25} -> {result}")
else:
    print("  EXISTING_FILES not found - extraction may not have run yet")
    print("  Try opening a BIN first.")
