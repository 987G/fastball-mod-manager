r"""NSW/UI/FRONTEND/FRONTEND.NSW: the Road to the Show pitch screen's picture arrays get entries 19 and 20,
both the airbender: the Change Pitch list asks by cell (its 20th tile, cell 19), the equipped slot by the id (20), and the new trajectory texture is registered.

The container (IFF0ASSH): header +0x0c u32 file size, +0x10 u32 table count (3), +0x18 table list; each table
{u32 count, u32 kind, u64 offset} lists {u32 hash, u32 0, u64 offset} sorted by hash, each pointing at a 'VOIDASS0'
chunk {u32 hash, u32 body size, body}. Table 1 = layouts, table 2 = the textures the layouts use (empty chunks, just
the texture's hash).

Layouts changed (body offsets; ui-stats.md 2.3):
  pitch_trajectory (8e230924)  two Flip nodes (right- and left-handed look), 19 trajectory textures each
  PitchTypeIcons   (f0531017)  two Flip nodes, 19 pitch item pictures each
A Flip node's texture array is {u16 node size, u16 count, u32 0xff} + count x 0x18 {u32 texture hash, 0,
0x80000000, ...}; the screen picks entry [index]. An index past the end shows nothing (seen in game: the equipped
airbender, id 20, with 20 entries); the Change Pitch list's 20th tile asked for 19 and showed the Vulcan Change's.

Growing a node moves everything after it, so a grown layout is written whole at the end of the file and its table
entry pointed there; table 2, one entry longer, also goes to the end. Nothing else in the file moves (other edits
at fixed offsets, like airbender_romfs.LIST_COUNT, stay valid). The old copies stay in the file, unused.
"""
import struct

TRAJ = 0x8e230924
ICONS = 0xf0531017
ENTRY = 0x18


def uihash(s):
    h = 0
    for c in s.lower().encode('latin1'):
        h = (c ^ (((h << 8) | (h >> 24)) & 0xffffffff)) & 0xffffffff
    return h | 0x80000000


def tables(d):
    n, tl = struct.unpack_from('<I', d, 0x10)[0], struct.unpack_from('<I', d, 0x18)[0]
    out = []
    for t in range(n):
        cnt, kind, off = struct.unpack_from('<IIQ', d, tl + 16 * t)
        out.append((tl + 16 * t, cnt, kind, off))
    return out


def find(d, h, table=1):
    _, cnt, _, off = tables(d)[table]
    for i in range(cnt):
        hh, _, co = struct.unpack_from('<IIQ', d, off + 16 * i)
        if hh == h:
            assert d[co:co + 8] == b'VOIDASS0' and struct.unpack_from('<I', d, co + 8)[0] == h
            return off + 16 * i, co, struct.unpack_from('<I', d, co + 12)[0]
    return None


# ---- a layout's node tree (only the node kinds these two layouts use) --------------------------------------------------

def _size(b, p):
    k = b[p + 0x26] & 0x3f
    if k == 0x0f:
        return struct.unpack_from('<I', b, p + 0x48)[0]
    if k == 0x1c:
        return struct.unpack_from('<I', b, p + 0x6c)[0]
    if k == 0x19:
        return struct.unpack_from('<H', b, p + 0x48)[0]
    if k == 0x22:
        return 0x80
    if k == 0x18:
        return 0x50
    raise AssertionError('node kind %#x at %#x not expected in this layout' % (k, p))


def _walk(b, p, out):
    k = b[p + 0x26] & 0x3f
    size = _size(b, p)
    out.append((p, k, size))
    if k == 0x0f:
        q = p + struct.unpack_from('<I', b, p + 0x38)[0]
        while q < p + size:
            q += _walk(b, q, out)
    elif k == 0x1c:
        cnt = b[p + 0x70]
        for i in range(cnt):
            r = struct.unpack_from('<Q', b, p + 0x78 + 8 * i)[0]
            if r:
                _walk(b, p + r, out)
        q = p + struct.unpack_from('<I', b, p + 0x48)[0]
        _walk(b, q, out)     # the empty look (text)
    return size


def grow(body, add):
    """body: a layout's body. add: texture hash to append to each of its Flip arrays (all get one more entry).
    Returns the new body."""
    b = bytes(body)
    sec = list(struct.unpack_from('<13Q', b, 0))
    nodes = []
    _walk(b, sec[0], nodes)
    assert sum(1 for n in nodes if n[0] + n[2] == sec[1]) >= 1, 'node tree does not end where the layout says'
    flips = [(p, s) for p, k, s in nodes if k == 0x19]
    assert len(flips) == 2, 'expected two picture arrays'
    ins = []
    for p, s in flips:
        size, cnt, ff = struct.unpack_from('<HHI', b, p + 0x48)
        assert (size, ff) == (0x50 + cnt * ENTRY, 0xff) and size == s, 'picture array is not the expected kind'
        ins.append(p + s)                                     # the new entry goes at the end of the array

    def moved(v):                                             # where body offset v ends up
        return v + ENTRY * sum(1 for q in ins if q <= v)

    # query table entries {u64 offset, u64 count} in section 7 (before the name pool); the section starts with other
    # 16-byte records, told apart by an offset that is not inside the section
    pool = struct.unpack_from('<Q', b, 0x60)[0]
    queries = []
    for q in range(sec[7], pool - 15, 16):
        o, c = struct.unpack_from('<QQ', b, q)
        if sec[7] <= o < pool and 0 < c <= 64:
            queries.append(q)
    out = bytearray(b)
    for q in queries:
        o = struct.unpack_from('<Q', b, q)[0]
        struct.pack_into('<Q', out, q, moved(o))
    for i in range(13):
        if sec[i]:
            struct.pack_into('<Q', out, 8 * i, moved(sec[i]))
    pool_end = struct.unpack_from('<I', b, 0x6c)[0]
    struct.pack_into('<Q', out, 0x60, moved(pool))
    struct.pack_into('<I', out, 0x6c, moved(pool_end))
    for p, k, s in nodes:
        # sizes: a node grows by the entries added inside it
        add_n = ENTRY * sum(1 for q in ins if p < q <= p + s)
        if k == 0x0f:
            struct.pack_into('<I', out, p + 0x48, s + add_n)
        elif k == 0x1c:
            struct.pack_into('<I', out, p + 0x6c, s + add_n)
            for i in range(b[p + 0x70]):
                r = struct.unpack_from('<Q', b, p + 0x78 + 8 * i)[0]
                if r:
                    struct.pack_into('<Q', out, p + 0x78 + 8 * i, moved(p + r) - p)
        elif k == 0x19:
            cnt = struct.unpack_from('<H', b, p + 0x4a)[0]
            struct.pack_into('<HH', out, p + 0x48, s + ENTRY, cnt + 1)
        # query pointers in the node: u64 fields that hold a query table entry's offset
        for f in range(p + 0x28, p + min(s, 0x78), 8):
            v = struct.unpack_from('<Q', b, f)[0]
            if v in queries:
                struct.pack_into('<Q', out, f, moved(v))
    # insert the entries (from the back, so earlier offsets stay right)
    for p, s in sorted(flips, reverse=True):
        last = bytearray(b[p + s - ENTRY:p + s])
        struct.pack_into('<I', last, 0, add)
        out[p + s:p + s] = last
    return bytes(out)


def entries(body):
    """The texture hashes of a layout's Flip arrays: [[...], [...]]."""
    nodes = []
    _walk(body, struct.unpack_from('<Q', body, 0)[0], nodes)
    res = []
    for p, k, s in nodes:
        if k == 0x19:
            cnt = struct.unpack_from('<H', body, p + 0x4a)[0]
            res.append([struct.unpack_from('<I', body, p + 0x50 + ENTRY * i)[0] for i in range(cnt)])
    return res


def _align(x, a=16):
    return (x + a - 1) // a * a


def build(d, traj_texture, icon_texture):
    """d: FRONTEND.NSW bytes (with or without airbender_romfs's tile count). traj_texture: the airbender's trajectory
    texture name (new, registered here); icon_texture: an existing texture for its item picture. Returns new bytes."""
    th, ih = uihash(traj_texture), uihash(icon_texture)
    assert struct.unpack_from('<I', d, 0x0c)[0] == len(d), 'file size field does not match'
    out = bytearray(d)
    for h, tex in ((TRAJ, th), (ICONS, ih)):
        ent, co, size = find(d, h)
        body = d[co + 16:co + 16 + size]
        assert [len(a) for a in entries(body)] == [19, 19], 'picture arrays are not the shipped 19'
        nb = grow(grow(body, tex), tex)   # entry 19 (the Change Pitch list's cell 19) and 20 (the pitch id itself)
        out += bytes(_align(len(out)) - len(out))
        at = len(out)
        out += b'VOIDASS0' + struct.pack('<II', h, len(nb)) + nb
        struct.pack_into('<Q', out, ent + 8, at)
    # table 2: the registered textures, sorted by hash; one more (an empty chunk), whole table moved to the end
    head, cnt, kind, off = tables(d)[2]
    rows = [struct.unpack_from('<IIQ', d, off + 16 * i) for i in range(cnt)]
    assert all(r[0] != th for r in rows), 'trajectory texture already registered'
    assert any(r[0] == ih for r in rows), 'icon texture is not registered'
    out += bytes(_align(len(out)) - len(out))
    chunk = len(out)
    out += b'VOIDASS0' + struct.pack('<II', th, 0)
    rows.append((th, 0, chunk))
    rows.sort(key=lambda r: r[0])
    out += bytes(_align(len(out)) - len(out))
    tab = len(out)
    for r in rows:
        out += struct.pack('<IIQ', *r)
    struct.pack_into('<IIQ', out, head, cnt + 1, kind, tab)
    out += bytes(_align(len(out)) - len(out))
    struct.pack_into('<I', out, 0x0c, len(out))
    return bytes(out)


def counts(d):
    """Entries in the pitch_trajectory arrays: [19, 19] as shipped."""
    r = find(d, TRAJ)
    if r is None:
        return []
    _, co, size = r
    return [len(a) for a in entries(d[co + 16:co + 16 + size])]


def touched(d):
    """The arrays were grown by any version of this edit."""
    return any(n > 19 for n in counts(d))


def installed(d):
    """True when this file's pitch_trajectory arrays have 21 entries (this version)."""
    return counts(d) == [21, 21]
