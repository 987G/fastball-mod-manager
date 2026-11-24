r"""Quirks+ file installer for a game install (used by the Show Mod Manager; mod.json "files").

  install    keep the shipped PS4/PRESENTATION/DIALOGSCENEFE.DSD once as DIALOGSCENEFE.DSD.orig, write the quirk
             offer scenes (built from the .orig, never from a modded file), update its size in romfs_list.tsv, and
             regenerate quirkmod.txt (the runtime module's rules) from quirks.json
  uninstall  put the .orig back and fix the size in romfs_list.tsv
  status     print "DIALOGSCENEFE.DSD: modded" or "DIALOGSCENEFE.DSD: original"; exit 0 whenever the file was read

Both install and uninstall can be run again after an interruption: every write goes to a temporary file that then
replaces the target (os.replace), so a file is never half written, and test copies hard-linked to the install never
see the change.

usage: python -E -s quirks_install.py install|uninstall|status [--game "C:\Games\MLB The Show 26"]
"""
import json
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build_offers  # noqa: E402
import export_quirkmod  # noqa: E402

romfs_index = build_offers.load_romfs_index()

DEFAULT_GAME = Path(r'C:\Games\MLB The Show 26')
NAME = 'DIALOGSCENEFE.DSD'


def paths(game):
    f = game / 'game' / 'romfs' / build_offers.REL
    return f, f.with_name(f.name + '.orig')


def replace_with(path, data):
    tmp = path.with_name(path.name + '.quirks-tmp')
    tmp.write_bytes(data)
    os.replace(tmp, path)


def install(game):
    f, orig = paths(game)
    if not orig.exists():
        if build_offers.is_modded(f.read_bytes()):
            raise SystemExit(f'{f} has the quirk scenes but no {orig.name}: put the shipped file back first')
        tmp = orig.with_name(orig.name + '.quirks-tmp')
        shutil.copy2(f, tmp)
        os.replace(tmp, orig)
    cfg = json.loads((HERE.parent / 'quirks.json').read_text(encoding='utf-8'))
    src = orig.read_bytes()
    out = build_offers.build(src, cfg)
    build_offers.verify(out, cfg, src)
    replace_with(f, out)
    print('romfs_list.tsv lines updated:', romfs_index.sync(game, [build_offers.REL]))
    txt = export_quirkmod.export(cfg)
    q = HERE.parent / 'quirkmod.txt'
    if not q.exists() or q.read_text(encoding='utf-8') != txt:
        tmp = q.with_name(q.name + '.quirks-tmp')
        tmp.write_text(txt, encoding='utf-8')
        os.replace(tmp, q)
    print(f'{NAME}: modded ({len(out)} bytes)')


def uninstall(game):
    f, orig = paths(game)
    if orig.exists():
        os.replace(orig, f)
    elif build_offers.is_modded(f.read_bytes()):
        raise SystemExit(f'{f} has the quirk scenes and {orig.name} is missing: cannot restore it')
    print('romfs_list.tsv lines updated:', romfs_index.sync(game, [build_offers.REL]))
    print(f'{NAME}: original')


def status(game):
    f, orig = paths(game)
    modded = build_offers.is_modded(f.read_bytes())
    print(f'{NAME}: {"modded" if modded else "original"}' + (' (backup kept)' if orig.exists() else ''))


def main():
    args = sys.argv[1:]
    game = DEFAULT_GAME
    if '--game' in args:
        i = args.index('--game')
        game = Path(args[i + 1])
        del args[i:i + 2]
    if args not in (['install'], ['uninstall'], ['status']):
        raise SystemExit(__doc__)
    {'install': install, 'uninstall': uninstall, 'status': status}[args[0]](game)


if __name__ == '__main__':
    main()
