r"""Pitching++ deliveries: file installer for a game install (for the Show Mod Manager; mod.json "files").

Every 2026 pitcher gets his own copy of the delivery he already has (a new style id in PITCHERSTREAM.BBS, the same 22
keys, with the windup and stretch throws edited to his real release), and the edited clips go in new slots at the end
of MLBPITCHINGCORE.XBN. Nothing in the shipped files changes otherwise: every existing style and clip stays as it was.
Which pitcher uses which copy is set at run time by Pitching++ (runtime/mlb/styles.c, set delivery_styles), so
franchise and Road to the Show saves pick it up too. Each pitcher's own slide step (made from his edited stretch throw)
goes in a new file, PS4/ANIMATION/PITCHERSLIDE.XBN (slot i = the pitcher on copy 25000 + i); runtime/mlb/slidestep.c
(set slide_steps) plays it in place of the game's generic slide steps. The game itself never opens that file.

  install    check the files first (each the shipped one, or the .orig copy an earlier install kept with the file
             still the shipped one or this mod's result; nothing is written otherwise), keep the shipped files once
             as MLBPITCHINGCORE.XBN.orig / PITCHERSTREAM.BBS.orig, build both from them and deliveries_2026.pack,
             check the result, write the clip bank, then the styles, then PITCHERSLIDE.XBN from slides_2026.pack
             (when that pack is there); update romfs_list.tsv. The shipped packs (format 2) hold no game data: only
             the differences that rebuild each edited clip and slide step from the player's own clips
             (clipdiff.py, deliveries/make_diff_packs.py), each rebuilt clip checked against its md5; the whole
             packs (format 1, deliveries/ship_private/) stay private and install the same files
  uninstall  check every edited file has its .orig copy (else stop, exit 1, nothing changed), put PITCHERSTREAM.BBS
             back first, then MLBPITCHINGCORE.XBN, delete PITCHERSLIDE.XBN, fix romfs_list.tsv
  status     "deliveries: installed" (the styles, the clips they use and the slide steps all in place),
             "deliveries: original", or "deliveries: partly installed (...)"; exit 0 whenever the files were read

Writes go to a temporary file that then replaces the target (os.replace): never half written, never through a hard
link (test copies share the install's files); a failed write or replace removes its temporary file and stops with a
message. The game must be closed. Imports ../deliveries/make_styles.py (and what it imports) and
romfs_index.py (the mod's _shared folder, the mods folder's _shared, or ../../rtts-equipment).
usage: python -E -s deliveries_install.py install|uninstall|status [--game "C:\Games\MLB The Show 26"] [--pack file]
       [--slides file]
"""
import hashlib
import json
import lzma
import os
import shutil
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'deliveries'))
def _romfs_index_dir():
    """romfs_index.py's folder: the mod's own _shared (a player's install from a release zip), the mods folder's _shared
    (the public repo), or rtts-equipment (the development PC)"""
    for d in (HERE.parent / '_shared', HERE.parent.parent / '_shared', HERE.parent.parent / 'rtts-equipment'):
        if (d / 'romfs_index.py').exists():
            return d
    raise SystemExit("romfs_index.py not found (looked in %s/_shared, the mods folder's _shared and rtts-equipment)"
                     % HERE.parent)


sys.path.insert(0, str(_romfs_index_dir()))   # romfs_index.py
from make_styles import build_xbn, read_bbs, write_bbs  # noqa: E402
import romfs_index  # noqa: E402
sys.path.insert(0, str(HERE))
import clipdiff  # noqa: E402

DEFAULT_GAME = Path(r'C:\Games\MLB The Show 26')
PACK = HERE.parent / 'deliveries' / 'ship' / 'deliveries_2026.pack'
SLIDES = HERE.parent / 'deliveries' / 'ship' / 'slides_2026.pack'
REL = {'xbn': 'PS4/ANIMATION/MLBPITCHINGCORE.XBN', 'bbs': 'PS4/ANIMATION/PITCHERSTREAM.BBS'}
SLIDE_REL = 'PS4/ANIMATION/PITCHERSLIDE.XBN'   # new file: not in the shipped game, so no .orig
MARK = 25000   # the first new style id: installed <=> the BBS has it


def paths(game):
    return {k: game / 'game' / 'romfs' / Path(v) for k, v in REL.items()}


def slide_path(game):
    return game / 'game' / 'romfs' / Path(SLIDE_REL)


def orig_of(f):
    return f.with_name(f.name + '.orig')


def tmp_of(f):
    return f.with_name(f.name + '.deliveries-tmp')


def _drop(f):
    try:
        f.unlink()
    except OSError:
        pass


def _swap_in(tmp, path):
    try:
        os.replace(tmp, path)
    except OSError as e:
        _drop(tmp)
        raise SystemExit('cannot replace %s (%s): close the game and anything holding the file (an antivirus scan?), '
                         'then run this again' % (path, e))


def replace_with(path, data):
    """path's content becomes data: written to a temporary file first, which then replaces it (never half written,
    never through a hard link); the temporary file is removed if that fails"""
    tmp = tmp_of(path)
    try:
        tmp.write_bytes(data)
    except OSError as e:
        _drop(tmp)
        raise SystemExit('cannot write %s (%s)' % (tmp, e))
    _swap_in(tmp, path)


def copy_over(src, dst):
    tmp = tmp_of(dst)
    try:
        shutil.copy2(src, tmp)
    except OSError as e:
        _drop(tmp)
        raise SystemExit('cannot copy %s to %s (%s)' % (src, tmp, e))
    _swap_in(tmp, dst)


def clean_tmp(game):
    """temporary files a crashed run left behind"""
    for f in list(paths(game).values()) + [slide_path(game)]:
        for t in (tmp_of(f), tmp_of(orig_of(f))):
            if t.exists():
                _drop(t)


def load_pack():
    """the pack's header and its body (after the header line)"""
    raw = lzma.decompress(Path(PACK).read_bytes())
    nl = raw.index(b'\n')
    return json.loads(raw[:nl]), raw[nl + 1:]


def xbn_clip(xb, i):
    sz, fo = struct.unpack_from('<II', xb, 0x30 + i * 8)
    return xb[fo:fo + sz]


def pack_clips(hdr, body, xb):
    """the edited clips: format 1 holds them whole (private builds); format 2, the shipped one, holds only the
    differences from the player's own clips in xb (clipdiff.py), and each rebuilt clip must match its md5"""
    clips, pos = [], 0
    if hdr.get('format', 1) == 1:
        for n in hdr['clip_sizes']:
            clips.append(body[pos:pos + n]); pos += n
        return clips
    for i, e in enumerate(hdr['clips']):
        src = b''.join(xbn_clip(xb, s) for s in e['src'])
        try:
            c = clipdiff.apply(body[pos:pos + e['ops']], src)
        except (ValueError, IndexError):
            c = None
        if c is None or len(c) != e['size'] or md5(c) != e['md5']:
            raise SystemExit("clip %d did not rebuild from this game's MLBPITCHINGCORE.XBN: the pack is for game %s's "
                             "shipped file" % (i, hdr['game']))
        clips.append(c); pos += e['ops']
    return clips


def slide_bank(sh, body, clips, xb):
    """PITCHERSLIDE.XBN: format 1 holds it whole; format 2 holds its head and slot table, then per slide step the
    differences from that pitcher's edited stretch throw (clips) and his original one (xb), zeros between"""
    if sh.get('format', 1) == 1:
        return bytes(body)
    bank = bytearray(sh['size'])
    bank[:sh['head_len']] = body[:sh['head_len']]
    pos = sh['head_len']
    for e in sh['entries']:
        src = b''.join(clips[i] if kind == 'new' else xbn_clip(xb, i) for kind, i in e['src'])
        try:
            c = clipdiff.apply(body[pos:pos + e['ops']], src)
        except (ValueError, IndexError):
            c = None
        if c is None or len(c) != e['size'] or md5(c) != e['md5']:
            raise SystemExit('slide step %d did not rebuild: the slides pack does not match the deliveries pack' % e['slot'])
        sz, fo = struct.unpack_from('<II', bank, 0x30 + e['slot'] * 8)
        assert sz == e['size'], 'slides pack: slot %d' % e['slot']
        bank[fo:fo + sz] = c
        pos += e['ops']
    return bytes(bank)


def pack_head(path):
    """a pack's header line (JSON) without unpacking the rest, or None when the pack is not there"""
    if not Path(path).exists():
        return None
    d = lzma.LZMADecompressor(); buf = b''
    with open(path, 'rb') as fh:
        while b'\n' not in buf:
            chunk = fh.read(1 << 16)
            if not chunk:
                raise SystemExit('%s: no header line' % path)
            buf += d.decompress(chunk)
    return json.loads(buf[:buf.index(b'\n')])


def bbs_installed(b):
    styles, _, _ = read_bbs(b)
    return any(sid == MARK for sid, _ in styles)


def new_clip_top(b, ids):
    """1 + the highest clip index the copies (style ids in ids) use; 0 without copies"""
    styles, _, _ = read_bbs(b)
    return max([c + 1 for sid, items in styles if sid in ids for _, cl in items for c in cl] or [0])


def md5(b):
    return hashlib.md5(b).hexdigest()


def file_md5(f):
    h = hashlib.md5()
    with open(f, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 24), b''):
            h.update(chunk)
    return h.hexdigest()


def earlier_install(k, f, base, ids):
    """f is this mod's result from another pack version, built on base (the shipped file kept as .orig): the styles
    file lists exactly base's styles plus copies with this mod's ids; the clip bank starts with all of base's clips,
    byte for byte (spot-checked as the build checks its own result), and has clips added after them"""
    cur = f.read_bytes()
    if k == 'bbs':
        st0, al0, n0 = read_bbs(base); st, al, n = read_bbs(cur)
        d0 = dict(st0); d = dict(st)
        return al == al0 and all(d.get(sid) == items for sid, items in st0) and set(d) - set(d0) <= ids \
            and MARK in d
    cnt0, cnt = struct.unpack_from('<I', base, 4)[0], struct.unpack_from('<I', cur, 4)[0]
    if cnt <= cnt0:
        return False
    for i in list(range(0, cnt0, 997)) + [cnt0 - 1]:
        a_, b_ = struct.unpack_from('<II', base, 0x30 + i * 8), struct.unpack_from('<II', cur, 0x30 + i * 8)
        if a_[0] != b_[0] or base[a_[1]:a_[1] + a_[0]] != cur[b_[1]:b_[1] + b_[0]]:
            return False
    return True


def install(game):
    p = paths(game)
    clean_tmp(game)
    hdr, body = load_pack()
    shipped = {'bbs': hdr['made_from']['bbs_md5'], 'xbn': hdr['made_from']['xbn_md5']}
    # 1. what to build from: the .orig copy an earlier install kept, or the file itself, which must then be the shipped
    #    one. Nothing is written before every check has passed.
    base = {}
    for k, f in p.items():
        o = orig_of(f)
        if o.exists():
            base[k] = o.read_bytes()
            if md5(base[k]) != shipped[k]:
                raise SystemExit("%s is not game %s's shipped %s: this pack is for that version" % (o, hdr['game'], f.name))
        else:
            base[k] = f.read_bytes()
            if md5(base[k]) != shipped[k]:
                if k == 'bbs' and bbs_installed(base[k]):
                    raise SystemExit('%s has the edited deliveries but no %s: put the shipped file back first (verify '
                                     'the game files)' % (f, o.name))
                raise SystemExit("%s differs from game %s's shipped file: this pack is for that version" % (f, hdr['game']))
    bb0, xb = base['bbs'], base['xbn']
    clips = pack_clips(hdr, body, xb)
    del body
    styles, alias, n2 = read_bbs(bb0)
    by_id = dict(styles)
    aliased = {struct.unpack_from('<H', alias, 4 * i)[0] for i in range(n2)}   # looked up before the styles
    assert not aliased & {s['style'] for s in hdr['styles']}, 'a new style id is an alias source (the game would redirect it)'
    cnt = struct.unpack_from('<I', xb, 4)[0]
    new = {}
    for s in hdr['styles']:
        assert s['style'] < 32768 and s['style'] not in by_id, 'new style ids: unused, < 32768 (read as a signed short)'
        keys = {int(k): cnt + i for k, i in s['keys'].items()}
        # a key listing several clips plays list[style % count] (f_700138a3d0): the copy keeps the one the pitcher's
        # own style picked as its only clip, so a new style id cannot pick another variant
        orig = s.get('orig', s['base'])
        new[s['style']] = [(key, [keys[key]] if key in keys else (list(cl) if len(cl) < 2 else [cl[orig % len(cl)]]))
                           for key, cl in by_id[s['base']]]
    bb = write_bbs(list(styles) + list(new.items()), alias, n2)
    xo, _ = build_xbn(xb, clips)
    # check before writing: old styles and clips unchanged, new ones exactly as packed
    chk, alias2, _ = read_bbs(bb); d = dict(chk)
    assert alias2 == alias and all(d[sid] == items for sid, items in styles), 'shipped styles changed'
    assert all(d[sid] == items for sid, items in new.items()), 'new styles'
    for i, c in enumerate(clips):
        sz, fo = struct.unpack_from('<II', xo, 0x30 + (cnt + i) * 8)
        assert xo[fo:fo + sz] == c, 'clip %d' % i
    for i in range(0, cnt, 997):
        a_, b_ = struct.unpack_from('<II', xb, 0x30 + i * 8), struct.unpack_from('<II', xo, 0x30 + i * 8)
        assert xb[a_[1]:a_[1] + a_[0]] == xo[b_[1]:b_[1] + b_[0]], 'shipped clip %d' % i
    built = {'bbs': bb, 'xbn': xo}
    # 2. where a .orig copy exists, the file itself must be that copy, this install's result or an earlier version of
    #    this mod's (an update of the pack): anything else means the game files changed since (an update or a repair),
    #    and building from the old copy would put old files back
    ids = {s['style'] for s in hdr['styles']}
    for k, f in p.items():
        o = orig_of(f)
        if o.exists() and file_md5(f) not in (shipped[k], md5(built[k])) and not earlier_install(k, f, base[k], ids):
            raise SystemExit("%s changed since %s was kept (a game update or repair?): it is neither the shipped file "
                             "nor this mod's. Delete %s and install again." % (f, o.name, o))
    slides = None
    if Path(SLIDES).exists():
        raw = lzma.decompress(Path(SLIDES).read_bytes())
        nl = raw.index(b'\n'); sh = json.loads(raw[:nl])
        slides = slide_bank(sh, raw[nl + 1:], clips, xb)
        del raw
        assert md5(slides) == sh['xbn_md5'] and sh['first_style'] == MARK, 'slides_2026.pack'
        n = struct.unpack_from('<I', slides, 4)[0]
        assert n <= max(s['style'] for s in hdr['styles']) - MARK + 1, 'slides for copies this pack does not have'
    # 3. keep the shipped files (checked above) once, then write: the clip bank before the styles that use its new
    #    clips, the slide steps last (status says "installed" only when all three are in place)
    for f in p.values():
        if not orig_of(f).exists():
            copy_over(f, orig_of(f))
    replace_with(p['xbn'], xo)
    replace_with(p['bbs'], bb)
    print("deliveries: %d pitchers' copies, %d clips added (%.0f MB clip bank)" % (len(new), len(clips), len(xo) / 2 ** 20))
    if slides is not None:
        replace_with(slide_path(game), slides)
        print('slide steps: %d pitchers (%.0f MB, %s)' % (sh['clips'], len(slides) / 2 ** 20, SLIDE_REL))
    print('romfs_list.tsv lines updated:', romfs_index.sync(game, list(REL.values())))


def uninstall(game):
    p = paths(game)
    clean_tmp(game)
    # check first: nothing changes until every edited file is known to have its .orig copy
    bbs_now = p['bbs'].read_bytes()
    if bbs_installed(bbs_now) and not orig_of(p['bbs']).exists():
        raise SystemExit('%s has the edited deliveries but no %s, so the shipped file cannot be put back: verify the '
                         'game files, then run uninstall again' % (p['bbs'], orig_of(p['bbs']).name))
    if not orig_of(p['xbn']).exists():
        head = pack_head(PACK)
        if head and file_md5(p['xbn']) != head['made_from']['xbn_md5']:
            raise SystemExit('%s is not the shipped file and has no %s to put back: verify the game files, then run '
                             'uninstall again' % (p['xbn'], orig_of(p['xbn']).name))
    # the styles first: styles pointing past the end of a restored clip bank is the one state never to leave behind
    for k in ('bbs', 'xbn'):
        o = orig_of(p[k])
        if o.exists():
            copy_over(o, p[k])
            o.unlink()
    sl = slide_path(game)
    if sl.exists():
        try:
            sl.unlink()
        except OSError as e:
            raise SystemExit('cannot delete %s (%s): close the game, then run uninstall again' % (sl, e))
    print('romfs_list.tsv lines updated:', romfs_index.sync(game, list(REL.values())))


def status(game):
    """"deliveries: installed" only when the styles, the clips they use and the slide steps (when this mod ships them)
    are all in place; "deliveries: original" when none is and no .orig copy is left; otherwise
    "deliveries: partly installed (...)", which the manager's "deliveries: installed" test reads as not installed,
    so enabling the mod installs again and repairs it"""
    p = paths(game)
    bbs = p['bbs'].read_bytes()
    with open(p['xbn'], 'rb') as fh:
        xcount = struct.unpack('<II', fh.read(8))[1]
    head = pack_head(PACK)   # the copies' style ids (without the pack: the id range they use)
    ids = {st['style'] for st in head['styles']} if head else set(range(MARK, MARK + 1000))
    top = new_clip_top(bbs, ids)
    have = {'styles': bbs_installed(bbs), 'clips': bool(top) and xcount >= top}
    sh = pack_head(SLIDES)
    sl = slide_path(game)
    if sh is not None:
        have['slide steps'] = sl.exists() and file_md5(sl) == sh['xbn_md5']
    kept = [o.name for o in (orig_of(p['bbs']), orig_of(p['xbn'])) if o.exists()]
    if all(have.values()):
        print('deliveries: installed')
    elif not any(have.values()) and not kept and not sl.exists():
        print('deliveries: original')
    else:
        print('deliveries: partly installed (%s; %s): run install to repair, or uninstall' % (
            ', '.join('%s %s' % (k, 'in place' if v else 'missing') for k, v in have.items()),
            'kept: ' + ', '.join(kept) if kept else 'no .orig copies'))
    print('slide steps: installed' if sl.exists() else 'slide steps: not installed')


def main():
    args = sys.argv[1:]
    game = DEFAULT_GAME
    if '--game' in args:
        i = args.index('--game'); game = Path(args[i + 1]); del args[i:i + 2]
    if '--pack' in args:   # another pack (tests)
        global PACK
        i = args.index('--pack'); PACK = Path(args[i + 1]); del args[i:i + 2]
    if '--slides' in args:   # another slide-step pack (tests)
        global SLIDES
        i = args.index('--slides'); SLIDES = Path(args[i + 1]); del args[i:i + 2]
    if len(args) != 1 or args[0] not in ('install', 'uninstall', 'status'):
        raise SystemExit(__doc__)
    {'install': install, 'uninstall': uninstall, 'status': status}[args[0]](game)


if __name__ == '__main__':
    main()
