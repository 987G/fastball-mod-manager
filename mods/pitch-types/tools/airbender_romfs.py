"""Pitching++ Airbender, romfs part: the pitch's name. NSW/UI/DISPLAYTEXT/COMMON.NSW_EN, string table `pitchtypes`
(re/ui-stats.md 1.1): 20 rows {long, short} by pitch id, row 19 "NONE". This adds row 20 "Airbender" / "ABND" (NONE
stays row 19: the patched name clamps send 19 and 21+ there), rebuilding the chunk (rows, then the string pool, padded
to 16) and moving the chunks after it in the container directory, and the file size in the header.

    python -I airbender_romfs.py <COMMON.NSW_EN original> <out>          (writes a new file; never edits in place)
    python -I airbender_romfs.py --check <file>                          (lists the pitchtypes rows)
"""
import struct
import sys

HASH = 0x8d137d6e
ROW = ('Airbender', 'ABND')


def chunks(d):
    assert d[:8] == b'IFF0ASSH', 'not an IFF0ASSH container'
    n, h, dir_off = struct.unpack_from('<IIQ', d, 0x20)
    return [(0x30 + 16 * i,) + struct.unpack_from('<IIQ', d, dir_off + 16 * i) for i in range(n)]


def find(d):
    for ent, h, _, off in chunks(d):
        if h == HASH:
            assert d[off:off + 8] == b'VOIDASS0' and struct.unpack_from('<I', d, off + 8)[0] == HASH
            return off
    raise SystemExit('no pitchtypes table')


def rows(d):
    off = find(d)
    body = off + 16
    n1, n2, n3 = struct.unpack_from('<HHH', d, body)
    o2 = struct.unpack_from('<Q', d, body + 0x10)[0]
    s = lambda o: d[body + o:d.index(b'\0', body + o)].decode()
    return [(s(a), s(b)) for a, b in (struct.unpack_from('<QQ', d, body + o2 + 16 * i) for i in range(n2))]


def build(d):
    off = find(d)
    size = struct.unpack_from('<I', d, off + 12)[0]
    body = off + 16
    n1, n2, n3 = struct.unpack_from('<HHH', d, body)
    o1, o2, o3 = struct.unpack_from('<QQQ', d, body + 8)
    assert (n1, n2, n3) == (0, 20, 0) and o2 == 0x28, 'pitchtypes is not the shipped one (%d %d %d)' % (n1, n2, n3)
    old = rows(d)
    assert old[19] == ('NONE', 'NONE') and old[14] == ('Screwball', 'SCR'), old
    new = old + [ROW]
    pool, offs = bytearray(), []
    start = 0x28 + 16 * len(new)
    for a, b in new:
        pa = start + len(pool); pool += a.encode() + b'\0'
        pb = start + len(pool); pool += b.encode() + b'\0'
        offs.append((pa, pb))
    nb = bytearray(d[body:body + 0x28])   # the 0x28-byte header as shipped, with the new row count
    struct.pack_into('<H', nb, 2, len(new))
    for pa, pb in offs:
        nb += struct.pack('<QQ', pa, pb)
    nb += pool
    nb += b'\0' * (-len(nb) % 16)
    grow = len(nb) - size
    out = bytearray(d[:body]) + nb + d[body + size:]
    struct.pack_into('<I', out, off + 12, len(nb))
    for ent, h, z, o in chunks(d):
        if o > off:
            struct.pack_into('<Q', out, ent + 8, o + grow)
    assert struct.unpack_from('<I', d, 0x0c)[0] == len(d)
    struct.pack_into('<I', out, 0x0c, len(out))
    return bytes(out)


def all_entries(d):
    """Every directory entry of an IFF0ASSH container: (file offset of the entry, hash, chunk offset)."""
    ntab, tdir = struct.unpack_from('<IxxxxQ', d, 0x10)
    out = []
    for t in range(ntab):
        n, h, off = struct.unpack_from('<IIQ', d, tdir + 16 * t)
        for i in range(n):
            out.append((off + 16 * i,) + struct.unpack_from('<IxxxxQ', d, off + 16 * i))
    return out


OPTIONS = 0x96767077
PT_RECORDS = range(0x1ec, 0x1f1)   # CP_PT_PT1..5 (side-session-notes airbender-editor.md)


def create_player_options(d):
    """NSW/UI/CREATEPLAYEROPTIONS.NSW_EN: the five pitch pickers (label index = pitch id + 1, value = index) get a 21st
    label "Airbender" (the editor code turns picked 19 into id 20 and shows 20 as label 20: make_airbender.py
    editor_sites). One shared 21-entry label array at PT1's place (PT2's old first entry is overwritten; PT2..5 point at
    the shared one), the string appended to the options body, later chunks moved."""
    ent = [e for e in all_entries(d) if e[1] == OPTIONS]
    assert len(ent) == 1
    c = ent[0][2]
    assert d[c:c + 8] == b'VOIDASS0'
    size = struct.unpack_from('<I', d, c + 12)[0]
    B = c + 16
    pool, ltab, recs = struct.unpack_from('<QQQ', d, B)
    nlab = struct.unpack_from('<H', d, B + 0x68)[0]
    out = bytearray(d)
    s = lambda o: d[B + o:d.index(b'\0', B + o)]
    first = recs + 0x98 * PT_RECORDS[0] + struct.unpack_from('<q', d, B + recs + 0x98 * PT_RECORDS[0] + 0x50)[0]
    labels = [struct.unpack_from('<Q', d, B + first + 8 * i)[0] for i in range(20)]
    for r in PT_RECORDS:
        ro = recs + 0x98 * r
        la = ro + struct.unpack_from('<q', d, B + ro + 0x50)[0]
        assert d[B + ro + 0x94] == 20 and [s(struct.unpack_from('<Q', d, B + la + 8 * i)[0]) for i in range(20)] == [s(x) for x in labels]
        assert s(ro + struct.unpack_from('<q', d, B + ro + 0x48)[0]) == b'CP_PT_PT%d' % (r - 0x1eb)
    assert ltab <= first and first + 21 * 8 <= ltab + 8 * nlab, 'the shared array must stay in the relocated table'
    assert s(labels[15]) == b'Screwball' and s(labels[19]) == b'Vulcanchange'
    new_str = size                                    # body offset of "Airbender"
    struct.pack_into('<Q', out, B + first + 8 * 20, new_str)
    for r in PT_RECORDS:
        ro = recs + 0x98 * r
        struct.pack_into('<q', out, B + ro + 0x50, first - ro)
        out[B + ro + 0x94] = 21
    add = b'Airbender'.ljust(16, bytes(1))
    out = out[:B + size] + add + out[B + size:]
    struct.pack_into('<I', out, c + 12, size + len(add))
    for at, h, off in all_entries(d):
        if off > c:
            struct.pack_into('<Q', out, at + 8, off + len(add))
    assert struct.unpack_from('<I', d, 0x0c)[0] == len(d)
    struct.pack_into('<I', out, 0x0c, len(out))
    return bytes(out)


LIST_COUNT = 0x12b7d2   # FRONTEND.NSW, PitchProgression's List_Default (available pitches): u16 count 19 -> 20


def frontend_nsw(d):
    """NSW/UI/FRONTEND/FRONTEND.NSW: Road to the Show's pitch screen lists 20 tiles; cell 19 is the airbender (the code
    takes cell 19 as id 20: make_airbender.py rtts_sites)."""
    assert d[0x129770:0x129778] == b'VOIDASS0' and struct.unpack_from('<H', d, LIST_COUNT)[0] == 19, \
        'FRONTEND.NSW is not the shipped one'
    out = bytearray(d)
    struct.pack_into('<H', out, LIST_COUNT, 20)
    return bytes(out)


def main():
    if sys.argv[1] == '--check':
        d = open(sys.argv[2], 'rb').read()
        for i, r in enumerate(rows(d)):
            print(i, *r)
        return
    d = open(sys.argv[1], 'rb').read()
    out = build(d)
    # every other table must read back the same
    for (e0, h0, _, o0), (e1, h1, _, o1) in zip(chunks(d), chunks(out)):
        assert h0 == h1
        if h0 != HASH:
            n0 = struct.unpack_from('<I', d, o0 + 12)[0]
            assert d[o0:o0 + 16 + n0] == out[o1:o1 + 16 + n0], hex(h0)
    assert rows(out) == rows(d) + [ROW]
    open(sys.argv[2], 'wb').write(out)
    print('%s: %d bytes (+%d), pitchtypes row 20 = %s / %s' % (sys.argv[2], len(out), len(out) - len(d), *ROW))


if __name__ == '__main__':
    main()
