"""Make a test copy of the installed game that costs almost no disk space and never changes the install.

    python mkmirror.py [--src "C:/Games/MLB The Show 26"] --dst <folder> [--exe <built mlb.exe>]

game/romfs and game/exefs become hard links to the install's files (NTFS, same volume). Everything the game writes
(profile, caches, mlb.cfg, logs) is a real copy. A file the mod changes must be replaced, never written in place:
write_new() writes a new file and renames it over the link, so the install's file keeps its bytes.
"""
import argparse
import os
import shutil
from pathlib import Path

COPY_TOP = ['mlb.cfg', 'SDL3.dll', 'nvngx_dlss.dll', 'totk_xlate.exe', 'totk_xlate.id', 'pipeline-cache.bin']
COPY_TREES = ['profile', 'shader-cache']


def write_new(path, data):
    """Replace path with new contents without touching the file a hard link shares."""
    path = Path(path)
    tmp = path.with_name(path.name + '.pitchmod-new')
    tmp.write_bytes(data)
    os.replace(tmp, path)


def link_tree(src, dst):
    n = 0
    for root, dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        out = os.path.join(dst, rel) if rel != '.' else dst
        os.makedirs(out, exist_ok=True)
        for f in files:
            d = os.path.join(out, f)
            if not os.path.exists(d):
                os.link(os.path.join(root, f), d)
                n += 1
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--src', default=r'C:\Games\MLB The Show 26')
    ap.add_argument('--dst', required=True)
    ap.add_argument('--exe', help='the built mlb.exe to put in the copy')
    a = ap.parse_args()
    src, dst = Path(a.src), Path(a.dst)
    if dst.resolve() == src.resolve():
        raise SystemExit('the copy must not be the install')
    (dst / 'game').mkdir(parents=True, exist_ok=True)
    for sub in ('romfs', 'exefs'):
        n = link_tree(src / 'game' / sub, dst / 'game' / sub)
        print(f'game/{sub}: {n} files linked')
    for f in (src / 'game').iterdir():
        if f.is_file():
            shutil.copy2(f, dst / 'game' / f.name)
    for f in COPY_TOP:
        if (src / f).exists() and not (dst / f).exists():
            shutil.copy2(src / f, dst / f)
    for t in COPY_TREES:
        if (src / t).exists() and not (dst / t).exists():
            shutil.copytree(src / t, dst / t)
    if a.exe:
        shutil.copy2(a.exe, dst / 'mlb.exe')
    print('copy ready at', dst)


if __name__ == '__main__':
    main()
