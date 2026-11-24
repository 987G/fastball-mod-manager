r"""Perks+ file installer for a game install (used by the Show Mod Manager; mod.json "files").

  install    keep the shipped FRONTEND/RTTS/PERKS.XML once as PERKS.XML.orig, write Perks+'s changes (built from the
             .orig, never from a modded file) and update its size in romfs_list.tsv
  uninstall  put the .orig back and fix the size in romfs_list.tsv
  status     print "PERKS.XML: modded" or "PERKS.XML: original"; exit 0 whenever the file was read

Every write goes to a temporary file that then replaces the target (os.replace), so a file is never half written and
test copies hard-linked to the install never see the change.

usage: python -E -s perks_install.py install|uninstall|status [--game "C:\Games\MLB The Show 26"]
"""
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# romfs_index.py (shared with other mods): a player's install has it in mods/perks-plus/_shared, the public repo in
# mods/_shared, the author's PC in mods/rtts-equipment. The first folder that holds it wins.
SHARED = [HERE.parent / '_shared', HERE.parent.parent / '_shared', HERE.parent.parent / 'rtts-equipment']
_found = next((d for d in SHARED if (d / 'romfs_index.py').is_file()), None)
if _found is None:
    sys.exit('perks_install: romfs_index.py not found in ' + ', '.join(str(d) for d in SHARED))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(_found))
import perks_xml  # noqa: E402
import romfs_index  # noqa: E402

DEFAULT_GAME = Path(r'C:\Games\MLB The Show 26')
REL = 'FRONTEND/RTTS/PERKS.XML'


def paths(game):
    f = game / 'game' / 'romfs' / REL
    return f, f.with_name(f.name + '.orig')


def read(path):
    return path.read_bytes().decode('utf-8')


def replace_with(path, data):
    tmp = path.with_name(path.name + '.perks-tmp')
    tmp.write_bytes(data)
    os.replace(tmp, path)


def install(game):
    f, orig = paths(game)
    if not orig.exists():
        if perks_xml.is_modded(read(f)):
            raise SystemExit(f'{f} has the Perks+ changes but no {orig.name}: put the shipped file back first')
        tmp = orig.with_name(orig.name + '.perks-tmp')
        shutil.copy2(f, tmp)
        os.replace(tmp, orig)
    replace_with(f, perks_xml.apply(read(orig)).encode('utf-8'))
    print('romfs_list.tsv lines updated:', romfs_index.sync(game, [REL]))
    print('PERKS.XML: modded')


def uninstall(game):
    f, orig = paths(game)
    if orig.exists():
        os.replace(orig, f)
    elif perks_xml.is_modded(read(f)):
        raise SystemExit(f'{f} has the Perks+ changes and {orig.name} is missing: cannot restore it')
    print('romfs_list.tsv lines updated:', romfs_index.sync(game, [REL]))
    print('PERKS.XML: original')


def status(game):
    f, orig = paths(game)
    modded = perks_xml.is_modded(read(f))
    print(f'PERKS.XML: {"modded" if modded else "original"}' + (' (backup kept)' if orig.exists() else ''))


def main():
    args = sys.argv[1:]
    game = DEFAULT_GAME
    if '--game' in args:
        k = args.index('--game')
        game = Path(args[k + 1])
        del args[k:k + 2]
    if len(args) != 1 or args[0] not in ('install', 'uninstall', 'status'):
        raise SystemExit(__doc__)
    {'install': install, 'uninstall': uninstall, 'status': status}[args[0]](game)


if __name__ == '__main__':
    main()
