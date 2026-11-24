"""Give pitchers their own delivery style in a TEST COPY.
New PITCHERSTREAM.BBS entries are copies of a base style whose chosen keys point at new clips. The clips become new
slots appended to MLBPITCHINGCORE.XBN, and each pitcher's roster PITCHING_STYLE (PLAYERS.BIN, _LIVE, _INJ) is set to
his new style. Every file is rebuilt from the install's and replaced in the copy (mkmirror.write_new): the copy's romfs
is hard-linked to the install, so nothing is ever written through a link.
    python -I make_styles.py <test copy> <spec.json>
    python -I make_styles.py <test copy> --restore
spec: [{"player": 15173, "style": 50001, "base": 15173, "clips": {"19": "edits/x.bin", "7": 478}}, ...]
      a clip is a file (a new slot) or an existing clip index (no new slot).

Formats (game 1.0.20):
  BBS: u32 n, u32 cap, u64 entries offset; u32 n2, u32 cap2, u64 alias offset (50 u16 pairs). An entry (0x28) is
       u64 0, u64 items (relative to the entry), u32 ni, u32 cap, u64 items again, u64 style id; entries sorted by id.
       An item (0x18) is u32 count, u32 cap, u64 clip list (relative to the item), u64 key; the list is u16 clips.
  XBN: u32 3, u32 count, 40 bytes 0; count x {u32 size, u32 offset}; count x 8 bytes 0 (filled at load); clips.
"""
import json, os, struct, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(HERE, '..', 'tools'))
from mkmirror import write_new
from set_styles import anim_offsets

INSTALL = r'C:\Games\MLB The Show 26'
ANIM = os.path.join('game', 'romfs', 'PS4', 'ANIMATION')
STATS = os.path.join('game', 'romfs', 'FRONTEND', 'STATS')
BBS, XBN = os.path.join(ANIM, 'PITCHERSTREAM.BBS'), os.path.join(ANIM, 'MLBPITCHINGCORE.XBN')
ROSTERS = [os.path.join(STATS, f) for f in ('PLAYERS.BIN', 'PLAYERS_LIVE.BIN', 'PLAYERS_INJ.BIN')]


def read_bbs(b):
    n, cap, off, n2, cap2, aoff = struct.unpack_from('<IIQIIQ', b, 0)
    styles = []
    for i in range(n):
        base = off + i * 0x28
        z, rel, ni, nc, rel2, sid = struct.unpack_from('<QQIIQQ', b, base)
        items = []
        for j in range(ni):
            ib = base + rel + j * 0x18
            cn, ccap, crel, key = struct.unpack_from('<IIQQ', b, ib)
            items.append((key, list(struct.unpack_from('<%dH' % cn, b, ib + crel))))
        styles.append((sid, items))
    return styles, b[aoff:aoff + n2 * 4], n2


def write_bbs(styles, alias, n2):
    styles = sorted(styles, key=lambda s: s[0])
    n = len(styles)
    out = bytearray(0x20 + n * 0x28)
    pos = len(out)
    blobs = []
    for i, (sid, items) in enumerate(styles):
        base = 0x20 + i * 0x28
        ib0 = pos
        pos += len(items) * 0x18
        lists = []
        for key, clips in items:
            lists.append(pos); pos += 2 * len(clips)
        pos += (-pos) % 8
        struct.pack_into('<QQIIQQ', out, base, 0, ib0 - base, len(items), len(items), ib0 - base, sid)
        blob = bytearray(pos - ib0)
        for j, ((key, clips), lp) in enumerate(zip(items, lists)):
            ib = ib0 + j * 0x18
            struct.pack_into('<IIQQ', blob, ib - ib0, len(clips), len(clips), lp - ib, key)
            struct.pack_into('<%dH' % len(clips), blob, lp - ib0, *clips)
        blobs.append(blob)
    for blob in blobs:
        out += blob
    out += b'\0' * ((-len(out)) % 8)
    aoff = len(out)
    out += alias
    struct.pack_into('<IIQIIQ', out, 0, n, n, 0x20, n2, n2, aoff)
    return bytes(out)


def build_xbn(b, new_clips):
    cnt = struct.unpack_from('<I', b, 4)[0]
    data0 = 0x30 + cnt * 16
    k = len(new_clips)
    cnt2 = cnt + k
    delta = 16 * k
    out = bytearray(b[:0x30])
    struct.pack_into('<I', out, 4, cnt2)
    slots = bytearray()
    for i in range(cnt):
        sz, fo = struct.unpack_from('<II', b, 0x30 + i * 8)
        slots += struct.pack('<II', sz, fo + delta if sz else fo)
    data = bytearray(b[data0:])
    pos = 0x30 + cnt2 * 16 + len(data)
    for c in new_clips:
        pad = (-pos) % 16
        data += b'\0' * pad; pos += pad
        slots += struct.pack('<II', len(c), pos)
        data += c; pos += len(c)
    assert pos < 1 << 32
    out += slots + b'\0' * (cnt2 * 8) + data
    return bytes(out), cnt


def main():
    copy = sys.argv[1]
    if sys.argv[2] == '--restore':
        for rel in [BBS, XBN] + ROSTERS:
            p = os.path.join(copy, rel)
            if os.path.exists(os.path.join(INSTALL, rel)):
                if os.path.exists(p):
                    os.remove(p)
                os.link(os.path.join(INSTALL, rel), p)
        print('restored the install files (hard links)')
        return
    spec = json.load(open(sys.argv[2]))
    styles, alias, n2 = read_bbs(open(os.path.join(INSTALL, BBS), 'rb').read())
    by_id = {sid: items for sid, items in styles}
    xb = open(os.path.join(INSTALL, XBN), 'rb').read()
    cnt = struct.unpack_from('<I', xb, 4)[0]
    new_clips, new = [], {}
    for s in spec:
        assert s['style'] < 65536, 'roster styles are 16-bit'
        items = [(key, list(clips)) for key, clips in by_id[s['base']]]
        for key, c in s['clips'].items():
            if isinstance(c, str):
                new_clips.append(open(os.path.join(HERE, c) if not os.path.isabs(c) else c, 'rb').read())
                idx = cnt + len(new_clips) - 1
            else:
                idx = int(c)
            items = [(k, [idx] if k == int(key) else cl) for k, cl in items]
        new[s['style']] = items
    styles = [(sid, items) for sid, items in styles if sid not in new] + list(new.items())
    bb = write_bbs(styles, alias, n2)
    xo, _ = build_xbn(xb, new_clips)
    # verify before writing: reread both
    chk, alias2, _ = read_bbs(bb)
    d = dict(chk)
    assert alias2 == alias and all(d[sid] == items for sid, items in new.items()), 'BBS round trip'
    assert all(d[sid] == items for sid, items in styles if sid not in new), 'old styles changed'
    for i, c in enumerate(new_clips):
        sz, fo = struct.unpack_from('<II', xo, 0x30 + (cnt + i) * 8)
        assert xo[fo:fo + sz] == c
    for i in (0, cnt // 2, cnt - 1):   # old clips unchanged
        a, b_ = struct.unpack_from('<II', xb, 0x30 + i * 8), struct.unpack_from('<II', xo, 0x30 + i * 8)
        assert xb[a[1]:a[1] + a[0]] == xo[b_[1]:b_[1] + b_[0]]
    write_new(os.path.join(copy, BBS), bb)
    write_new(os.path.join(copy, XBN), xo)
    print('BBS: %d styles (+%d new: %s), %d bytes; XBN: %d slots (+%d), %d bytes' % (
        len(styles), len([s for s in new if s not in by_id]), sorted(new), len(bb), cnt + len(new_clips), len(new_clips), len(xo)))
    want = {s['player']: s['style'] for s in spec}
    for rel in ROSTERS:
        path = os.path.join(copy, rel)
        if not os.path.exists(path):
            continue
        b = bytearray(open(os.path.join(INSTALL, rel), 'rb').read())
        nset = 0
        for r, off in anim_offsets(bytes(b)):
            if r.get('PLAYER_ID') in want:
                struct.pack_into('<H', b, off + 4, want[r['PLAYER_ID']]); nset += 1
        write_new(path, bytes(b))
        print('%s: %d players set' % (os.path.basename(rel), nset))


if __name__ == '__main__':
    main()
