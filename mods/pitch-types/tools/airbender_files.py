r"""Pitching++ Airbender, file step: the pitch's name, the player editor's 21st pitch label, Road to the Show's 20th
pitch tile (airbender_romfs.py), for the set airbender (make_airbender.py), and Road to the Show's trajectory pictures
redrawn from Pitching++'s movement with the airbender's own (pitch_pictures_std.py from pitch_pictures_masks.bin,
frontend_nsw_pictures.py). Standard library only.

  NSW/UI/DISPLAYTEXT/COMMON.NSW_EN        pitchtypes row 20 "Airbender / ABND"
  NSW/UI/CREATEPLAYEROPTIONS.NSW_EN       the five pitch pickers end in "Airbender"
  NSW/UI/FRONTEND/FRONTEND.NSW            the Road to the Show pitch screen lists 20 pitches; its trajectory and item
                                          picture arrays get entries 19 (list cell) and 20 (the id); the new picture registered
  NSW/UI/FRONTEND/FRONTEND.TEX            the 19 trajectory pictures redrawn, the airbender's added

  install    each file as the game has it (unmodded: shipped, or a game update's) is kept as <file>.orig, and the file
             is written from the .orig; romfs_list.tsv updated
  uninstall  each .orig put back (exit 1, nothing changed for a file that is modded and has no .orig); romfs_list.tsv
  status     "airbender: installed", "airbender: original", or "airbender: partly installed (...)"

Writes go to a temporary file that then replaces the target (os.replace): never half written, never through a hard
link (test copies share the install's files). The game must be closed. Run by pitching_files.py (the Mod Manager's
file step).
usage: python -E -s airbender_files.py install|uninstall|status [--game "C:\Games\MLB The Show 26"]
"""
import os
import shutil
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
def _romfs_index_dir():
    """romfs_index.py's folder: the mod's own _shared (a player's install from a release zip), the mods folder's _shared
    (the public repo), or rtts-equipment (the development PC)"""
    for d in (HERE.parent / '_shared', HERE.parent.parent / '_shared', HERE.parent.parent / 'rtts-equipment'):
        if (d / 'romfs_index.py').exists():
            return d
    raise SystemExit("romfs_index.py not found (looked in %s/_shared, the mods folder's _shared and rtts-equipment)"
                     % HERE.parent)


sys.path.insert(0, str(_romfs_index_dir()))   # romfs_index.py
import airbender_romfs  # noqa: E402
import frontend_nsw_pictures  # noqa: E402
import pitch_pictures_std  # noqa: E402   standard library only (players' plain Python)
import romfs_index  # noqa: E402

DEFAULT_GAME = Path(r'C:\Games\MLB The Show 26')


def pt_count(d):
    ent = [e for e in airbender_romfs.all_entries(d) if e[1] == airbender_romfs.OPTIONS][0]
    B = ent[2] + 16
    recs = struct.unpack_from('<Q', d, B + 0x10)[0]
    return d[B + recs + 0x98 * airbender_romfs.PT_RECORDS[0] + 0x94]


def frontend_nsw(d):
    return frontend_nsw_pictures.build(airbender_romfs.frontend_nsw(d), pitch_pictures_std.AIRBENDER, 'pitch_type_changeup')


def nsw_tile(d):
    return struct.unpack_from('<H', d, airbender_romfs.LIST_COUNT)[0] == 20


# relative path, the edit (unmodded bytes -> modded bytes), "touched by this mod" (any version of it: the file is not
# the game's own), "fully installed"
FILES = [
    ('NSW/UI/DISPLAYTEXT/COMMON.NSW_EN', airbender_romfs.build, lambda d: len(airbender_romfs.rows(d)) > 20, None),
    ('NSW/UI/CREATEPLAYEROPTIONS.NSW_EN', airbender_romfs.create_player_options, lambda d: pt_count(d) == 21, None),
    ('NSW/UI/FRONTEND/FRONTEND.NSW', frontend_nsw,
     lambda d: nsw_tile(d) or frontend_nsw_pictures.touched(d),
     lambda d: nsw_tile(d) and frontend_nsw_pictures.installed(d)),
    ('NSW/UI/FRONTEND/FRONTEND.TEX', pitch_pictures_std.frontend_tex, pitch_pictures_std.tex_installed, None),
]


def paths(game, rel):
    f = game / 'game' / 'romfs' / rel
    return f, f.with_name(f.name + '.orig')


def replace(path, data=None, src=None):
    tmp = path.with_name(path.name + '.ab-tmp')
    try:
        if src is not None:
            shutil.copy2(src, tmp)
        else:
            tmp.write_bytes(data)
        os.replace(tmp, path)
    except OSError as e:
        if tmp.exists():
            tmp.unlink()
        raise SystemExit('cannot write %s: %s' % (path, e))


def install(game):
    for rel, edit, modded, _ in FILES:
        f, orig = paths(game, rel)
        if not modded(f.read_bytes()):
            replace(orig, src=f)   # the game's own file, kept to build from and to put back
        elif not orig.exists():
            raise SystemExit('%s has the airbender but no %s: put the shipped file back first' % (f, orig.name))
        base = orig.read_bytes()
        if modded(base):
            raise SystemExit('%s has the airbender: put the shipped file back as %s first' % (orig, f.name))
        try:
            data = edit(base)
        except AssertionError as e:
            raise SystemExit('%s is not the file this was made for (game update?): %s' % (rel, e))
        replace(f, data)
        print('%s: written' % rel)
    print('romfs_list.tsv lines updated:', romfs_index.sync(game, [r for r, _, _, _ in FILES]))
    print('airbender: installed')


def uninstall(game):
    bad = 0
    for rel, _, modded, _ in FILES:
        f, orig = paths(game, rel)
        if orig.exists():
            os.replace(orig, f)
        elif modded(f.read_bytes()):
            print('%s has the airbender but no %s: nothing changed' % (f, orig.name))
            bad = 1
    print('romfs_list.tsv lines updated:', romfs_index.sync(game, [r for r, _, _, _ in FILES]))
    if bad:
        sys.exit(1)
    print('airbender: original')


def status(game):
    have = []
    for rel, _, modded, complete in FILES:
        d = paths(game, rel)[0].read_bytes()
        if (complete or modded)(d):
            have.append(rel)
        elif modded(d):
            have.append(rel + ' (older version)')
    if len(have) == len(FILES) and not any(h.endswith('version)') for h in have):
        print('airbender: installed')
    elif not have:
        print('airbender: original')
    else:
        print('airbender: partly installed (%s)' % ', '.join(have))


def main():
    args = sys.argv[1:]
    game = DEFAULT_GAME
    if '--game' in args:
        i = args.index('--game')
        game = Path(args[i + 1])
        del args[i:i + 2]
    if len(args) != 1 or args[0] not in ('install', 'uninstall', 'status'):
        raise SystemExit(__doc__)
    {'install': install, 'uninstall': uninstall, 'status': status}[args[0]](game)


if __name__ == '__main__':
    main()
