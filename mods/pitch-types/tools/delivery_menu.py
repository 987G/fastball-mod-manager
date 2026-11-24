r"""Pitching++ deliveries in the player editor: lists every delivery copy in the editor's delivery list, so a created
or edited player can be given one.

The editor reads its list from FRONTEND/CREATEPLAYER/CREATEPLAYERANIMDEFNS.XML when the create-player screen opens
(f_700069ba30: <PitchingAnims> groups Generics, RealPlayers, Legends, fixed; each <AnimDefn anim_id="style"
name="shown name"/>, name up to 63 bytes; the list shows 31). Each copy goes in Real Players ("Current Players" in the
game) as "<his name> (Pitching++)" (a first initial, or the surname alone, where that is too long to show), right after
his own entry when the list has one, else in surname order. Names: data/delivery_menu.tsv (make_delivery_menu.py). Only
copies the installed PITCHERSTREAM.BBS has are listed (a style it lacks would play Generic Windup 1).

  install    the file without Pitching++ entries is kept as CREATEPLAYERANIMDEFNS.XML.orig (again whenever the file
             has none: a game update's new file becomes the one kept); the file is written from it with the entries;
             romfs_list.tsv updated
  uninstall  put the .orig back (exit 1, nothing changed, when the file has entries and no .orig); romfs_list.tsv
  status     "delivery menu: installed (N deliveries listed)", "delivery menu: original", or
             "delivery menu: partly installed (...)"

Writes go to a temporary file that then replaces the target (os.replace): never half written, never through a hard
link (test copies share the install's files). The game must be closed. Run by pitching_files.py (the Mod Manager's
file step) after deliveries_install.py installs and before it uninstalls.
usage: python -E -s delivery_menu.py install|uninstall|status [--game "C:\Games\MLB The Show 26"]
"""
import os
import re
import shutil
import struct
import sys
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
def _romfs_index_dir():
    """romfs_index.py's folder: the mod's own _shared (a player's install from a release zip), the mods folder's _shared
    (the public repo), or rtts-equipment (the development PC)"""
    for d in (HERE.parent / '_shared', HERE.parent.parent / '_shared', HERE.parent.parent / 'rtts-equipment'):
        if (d / 'romfs_index.py').exists():
            return d
    raise SystemExit("romfs_index.py not found (looked in %s/_shared, the mods folder's _shared and rtts-equipment)"
                     % HERE.parent)


sys.path.insert(0, str(_romfs_index_dir()))   # romfs_index.py
import romfs_index  # noqa: E402

DEFAULT_GAME = Path(r'C:\Games\MLB The Show 26')
REL = 'FRONTEND/CREATEPLAYER/CREATEPLAYERANIMDEFNS.XML'
BBS_REL = 'PS4/ANIMATION/PITCHERSTREAM.BBS'
NAMES = HERE / 'data' / 'delivery_menu.tsv'
LABEL = ' (Pitching++)'
MARK = '<!-- Pitching++ -->'   # ends every line this adds
SUFFIXES = {'jr.', 'jr', 'sr.', 'sr', 'ii', 'iii', 'iv'}
SHOWN_MAX = 31   # the delivery list shows this many bytes of a name (seen in game, 2026-11-21)


def paths(game):
    f = game / 'game' / 'romfs' / REL
    return f, f.with_name(f.name + '.orig')


def read(path):
    return path.read_bytes().decode('utf-8-sig')


def write(path, text):
    """UTF-8 with the BOM the shipped file has, through a temporary file."""
    tmp = path.with_name(path.name + '.pp-tmp')
    try:
        tmp.write_bytes(b'\xef\xbb\xbf' + text.encode('utf-8'))
        os.replace(tmp, path)
    except OSError as e:
        if tmp.exists():
            tmp.unlink()
        raise SystemExit('cannot write %s: %s' % (path, e))


def is_modded(text):
    return MARK in text


def installed_styles(game):
    """Style ids in the installed PITCHERSTREAM.BBS (u32 count, u32 capacity, u64 offset; 0x28-byte entries, the style
    id a u64 at +0x20)."""
    with open(game / 'game' / 'romfs' / BBS_REL, 'rb') as f:
        head = f.read(16)
        count, _, off = struct.unpack('<IIQ', head)
        f.seek(off)
        table = f.read(count * 0x28)
    return {struct.unpack_from('<Q', table, i * 0x28 + 0x20)[0] for i in range(count)}


def load_names():
    rows = []
    for line in NAMES.read_text(encoding='utf-8').splitlines()[1:]:
        s, pid, name = line.split('\t')
        rows.append((int(s), int(pid), name))
    return rows


def sort_key(name):
    """The list's order: surname (everything after the first name, so "Woods Richardson" sorts under W), then first
    name; accents, middle initials ("Jose A. Ferrer") and a trailing Jr. / II ignored."""
    s = unicodedata.normalize('NFKD', name)
    w = ''.join(c for c in s if not unicodedata.combining(c)).lower().split()
    while len(w) > 2 and w[-1] in SUFFIXES:
        w.pop()
    rest = [x for x in w[1:] if not (len(x) == 2 and x.endswith('.'))]
    return (' '.join(rest), w[0] if w else '')


def shown(name):
    """name + LABEL within the list's 31 visible bytes (it cuts longer names: "Simeon Woods Richardson (Pitchi"):
    the first name as an initial, else the surname alone."""
    def fits(n):
        return len((n + LABEL).encode('utf-8')) <= SHOWN_MAX
    if fits(name) or ' ' not in name:
        return name + LABEL
    first, rest = name.split(' ', 1)
    return (first[0] + '. ' + rest if fits(first[0] + '. ' + rest) else rest) + LABEL


def esc(s):
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


def apply(text, entries):
    """text (no Pitching++ entries) with one Real Players line per (style, player id, name) in entries."""
    nl = '\r\n' if '\r\n' in text else '\n'
    p = text.index('<PitchingAnims>')
    a = text.index('<RealPlayers>', p)
    b = text.index('</RealPlayers>', a)
    a = text.index(nl, a) + len(nl)            # the first line after <RealPlayers>
    b = text.rindex(nl, a, b) + len(nl)        # the start of the </RealPlayers> line
    lines = text[a:b].split(nl)[:-1]
    own, keys = {}, []                         # vanilla entries: player id -> line index; (key, line index)
    for i, line in enumerate(lines):
        m = re.search(r'<AnimDefn anim_id="(\d+)" name="([^"]*)"', line)
        if m:
            own.setdefault(int(m.group(1)), i)
            keys.append((sort_key(m.group(2)), i))
    indent = re.match(r'\s*', next((l for l in lines if '<AnimDefn' in l), '\t\t\t')).group(0)
    after = {}                                 # line index (-1: before the first) -> new lines
    for style, pid, name in entries:
        k = sort_key(name)
        at = own.get(pid)
        if at is None:
            # before the first entry that sorts after it (the shipped list is in order but for a few pairs)
            at = next((i for kk, i in keys if kk > k), len(lines)) - 1
            while at >= 0 and '<AnimDefn' not in lines[at]:
                at -= 1
        after.setdefault(at, []).append((k, '%s<AnimDefn anim_id="%d" name="%s" /> %s'
                                         % (indent, style, esc(shown(name)), MARK)))
    out = [l for _, l in sorted(after.get(-1, []))]
    for i, line in enumerate(lines):
        out.append(line)
        out.extend(l for _, l in sorted(after.get(i, [])))
    return text[:a] + nl.join(out) + nl + text[b:]


def listed(text):
    return [int(s) for s in re.findall(r'anim_id="(\d+)"[^\n]*' + re.escape(MARK), text)]


def install(game):
    f, orig = paths(game)
    now = read(f)
    if not is_modded(now):
        # the file as the game has it (shipped, or a game update's): kept as the one to build from and put back
        tmp = orig.with_name(orig.name + '.pp-tmp')
        shutil.copy2(f, tmp)
        os.replace(tmp, orig)
    elif not orig.exists():
        raise SystemExit('%s has Pitching++ entries but no %s: put the shipped file back first' % (f, orig.name))
    base = read(orig)
    if is_modded(base):
        raise SystemExit('%s has Pitching++ entries: put the shipped file back as %s first' % (orig, f.name))
    have = installed_styles(game)
    entries = [e for e in load_names() if e[0] in have]
    write(f, apply(base, entries))
    print('romfs_list.tsv lines updated:', romfs_index.sync(game, [REL]))
    print('delivery menu: installed (%d deliveries listed)' % len(entries))


def uninstall(game):
    f, orig = paths(game)
    if orig.exists():
        os.replace(orig, f)
    elif is_modded(read(f)):
        print('%s has Pitching++ entries but no %s: nothing changed' % (f, orig.name))
        sys.exit(1)
    print('romfs_list.tsv lines updated:', romfs_index.sync(game, [REL]))
    print('delivery menu: original')


def status(game):
    f, _ = paths(game)
    text = read(f)
    if not is_modded(text):
        print('delivery menu: original')
        return
    ids = listed(text)
    missing = [s for s in ids if s not in installed_styles(game)]
    if missing:
        print('delivery menu: partly installed (%d of %d listed deliveries not in PITCHERSTREAM.BBS)'
              % (len(missing), len(ids)))
    else:
        print('delivery menu: installed (%d deliveries listed)' % len(ids))


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
