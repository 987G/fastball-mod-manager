r"""NSW/UI/FRONTEND/FRONTEND.TEX (TX12NSW texture bank of the menus): read a picture, and rebuild the bank with some
pictures replaced and new ones added. Used for Pitching++'s pitch trajectory pictures (pitch_pictures.py).

Layout (worked out from the file; mods/rtts-equipment/live_bank.py has the same bank kind):
  header 0x30   'TX12NSW\0', u32 0xc, +0x0c u32 data size (each texture's blocks, 16-aligned, back to back),
                +0x10 u32 sum of the entries' level-0 unpacked sizes, +0x14 u32 entry count, +0x18 u32 gap between the
                table and the data (the layout block; data starts 4 KiB aligned)
  table         0x140 bytes per entry from 0x30, in no particular order:
                  name[64]; +0x40 8-byte data hash, +0x48 8-byte layout hash; +0x50 level 0 {u32 size, u16 w, h, ...};
                  +0xa0 8-byte hash and +0xb0 {size, w, h, ...} of the 32x32 stand-in;
                  +0xc0 block 0 {u32 unpacked, u32 stored, u32 offset, u32 0, u8 lz4, u8 0, u8 1, u8 ?} (level 0),
                  +0xfc block 1 the same (the stand-in; offset from the entry's data start),
                  +0x110 4 floats: the picture's colour (the trail's, 0..1) and its mean alpha,
                  +0x120 u32 format, +0x124 u32 data offset in the file
  layouts       the gap: u32 count, u32 size (= the gap), 8 zero bytes, 0x40 per layout
  data          to the end of the file (no trailer in this bank)

A picture here: 512x512 BC7, one mip, Tegra block-linear, plus a 32x32 BC7 stand-in. Replaced and added pictures are
stored without LZ4 (block flag 0, as two of the shipped entries are) at the end of the data; the replaced entries' old
data stays where it was, unused. build() and the table reading need the standard library only; picture() and encode()
(dev tools) need numpy.
"""
import hashlib
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
_EQ = HERE.parent.parent / 'rtts-equipment'


def _np():
    """numpy and the equipment mod's BC7 / block-linear / TX12 helpers, for the dev-side picture() and encode() only:
    build() and Bank's table reading need the standard library alone (the install step, pitch_pictures_std.py)."""
    sys.path.insert(0, str(_EQ / 'render'))
    sys.path.insert(0, str(_EQ))
    import numpy
    import bc7
    import live_bank
    import tx12
    return numpy, bc7, live_bank, tx12

ENTRY = 0x140
RAW = bytes([0, 0, 1, 0])


def _align(x, a):
    return (x + a - 1) // a * a


class Bank:
    def __init__(self, src):
        self.b = src if isinstance(src, (bytes, bytearray)) else Path(src).read_bytes()
        b = self.b
        assert b[:7] == b'TX12NSW', 'not a TX12NSW bank'
        self.stored, self.unpacked, self.n, self.gap = struct.unpack_from('<IIII', b, 0x0c)
        self.start = 0x30 + self.n * ENTRY + self.gap
        assert len(b) == self.start + self.stored, 'bank size does not match its header'
        self.names = [bytes(b[0x30 + i * ENTRY:0x30 + i * ENTRY + 64]).split(b'\0')[0].decode('latin1')
                      for i in range(self.n)]
        self.index = {nm.lower(): i for i, nm in enumerate(self.names)}

    def entry(self, name):
        i = self.index[name.lower()]
        return bytes(self.b[0x30 + i * ENTRY:0x30 + (i + 1) * ENTRY])

    def block(self, e, k):
        o = 0xc0 + 0x3c * k
        unpacked, stored, rel = struct.unpack_from('<III', e, o)
        off = struct.unpack_from('<I', e, 0x124)[0] + rel
        raw = bytes(self.b[off:off + stored])
        if e[o + 0x10] == 0:
            return raw
        assert e[o + 0x10] == 1, 'block is neither LZ4 nor stored'
        return _np()[3]._lz4_decompress(raw, unpacked)

    def picture(self, name, level=0):
        """A 512x512 BC7 picture's level 0 (or its 32x32 stand-in, level 1) as (h, w, 4) uint8 RGBA."""
        e = self.entry(name)
        w, h = struct.unpack_from('<HH', e, 0x54 if level == 0 else 0xb4)
        size = struct.unpack_from('<I', e, 0x50 if level == 0 else 0xb0)[0]
        assert size == w * h, '%s is not a BC7 picture' % name
        return _np()[3].decode_bcn(self.block(e, level), w, h, 7)


def _bc7(px):
    """(h, w, 4) uint8 -> BC7 blocks, block-linear, as the bank stores them."""
    np, bc7, live_bank, _ = _np()
    h, w = px.shape[:2]
    bw, bh = w // 4, h // 4
    blocks = px.reshape(bh, 4, bw, 4, 4).transpose(0, 2, 1, 3, 4).reshape(bw * bh, 16, 4)
    seen = blocks[:, :, 3].any(1)
    out = np.empty((bw * bh, 16), np.uint8)
    out[~seen] = np.frombuffer(bc7.encode(np.zeros((1, 16, 4), np.uint8))[0], np.uint8)
    if seen.any():
        out[seen] = np.frombuffer(b''.join(bc7.encode(blocks[seen])), np.uint8).reshape(-1, 16)
    return live_bank.swizzle(out.tobytes(), bw * 16, bh)


def _small(px):
    """The 32x32 stand-in: 16x16 box average with premultiplied alpha."""
    np = _np()[0]
    p = px.astype(np.float64)
    a = p[..., 3:4] / 255
    s = (p[..., :3] * a).reshape(32, 16, 32, 16, 3).mean((1, 3))
    sa = a.reshape(32, 16, 32, 16, 1).mean((1, 3))
    rgb = np.where(sa > 0, s / np.maximum(sa, 1e-9), 0)
    return np.clip(np.round(np.concatenate([rgb, sa * 255], 2)), 0, 255).astype(np.uint8)


def encode(px):
    """A (512, 512, 4) uint8 picture -> (level 0 bytes, stand-in bytes, colour floats)."""
    assert px.shape == (512, 512, 4), px.shape
    np = _np()[0]
    a = px[..., 3]
    m = a > 0
    trail = m & ~(px[..., :3].min(2) > 235)
    col = (np.median(px[trail][:, :3], 0) if trail.any() else np.zeros(3)) / 255.0
    return _bc7(px), _bc7(_small(px)), (float(col[0]), float(col[1]), float(col[2]), float(a.mean() / 255.0))


def build(data, encoded, added=None):
    """data: the bank's bytes. encoded: {texture name: encode(...)} for pictures to replace or add. added: {new name:
    name of the entry to copy (format, layout)} for the ones that are new. Returns the new bank's bytes."""
    added = added or {}
    bank = Bank(data)
    for nm in encoded:
        assert nm.lower() in bank.index or nm in added, 'not in the bank: %s' % nm
    for nm, tmpl in added.items():
        assert nm.lower() not in bank.index, 'already in the bank: %s' % nm
        assert tmpl.lower() in bank.index, 'template not in the bank: %s' % tmpl
    n = bank.n + len(added)
    layouts = bytearray(bank.b[0x30 + bank.n * ENTRY:bank.start])
    used = 0x10 + 0x40 * struct.unpack_from('<I', layouts, 0)[0]
    start = _align(0x30 + n * ENTRY + used, 0x1000)
    gap = start - (0x30 + n * ENTRY)
    layouts = layouts[:used] + bytes(gap - used)
    struct.pack_into('<I', layouts, 4, gap)
    shift = start - bank.start

    table = bytearray(bank.b[0x30:0x30 + bank.n * ENTRY])
    for i in range(bank.n):
        o = i * ENTRY + 0x124
        struct.pack_into('<I', table, o, struct.unpack_from('<I', table, o)[0] + shift)
    for nm, tmpl in added.items():
        e = bytearray(bank.entry(tmpl))
        e[:64] = nm.encode('ascii').ljust(64, b'\0')
        table += e
    index = dict(bank.index)
    for k, nm in enumerate(added):
        index[nm.lower()] = bank.n + k

    tail = bytearray()
    off = start + bank.stored
    unpacked = bank.unpacked
    for nm, (big, small, colour) in encoded.items():
        i = index[nm.lower()]
        e = memoryview(table)[i * ENTRY:(i + 1) * ENTRY]
        assert struct.unpack_from('<IHH', e, 0x50) == (len(big), 512, 512), nm
        assert struct.unpack_from('<IHH', e, 0xb0) == (len(small), 32, 32), nm
        if nm in added:
            unpacked += len(big)
        else:
            unpacked += len(big) - struct.unpack_from('<I', e, 0xc0)[0]
        e[0x40:0x48] = hashlib.md5(big).digest()[:8]
        e[0xa0:0xa8] = hashlib.md5(small).digest()[:8]
        rel = _align(len(big), 16)
        struct.pack_into('<IIII', e, 0xc0, len(big), len(big), 0, 0)
        e[0xd0:0xd4] = RAW
        struct.pack_into('<IIII', e, 0xfc, len(small), len(small), rel, 0)
        e[0x10c:0x110] = RAW
        struct.pack_into('<4f', e, 0x110, *colour)
        struct.pack_into('<I', e, 0x124, off)
        span = _align(rel + len(small), 16)
        tail += big + bytes(rel - len(big)) + small + bytes(span - rel - len(small))
        off += span
    head = bytearray(bank.b[:0x30])
    struct.pack_into('<IIII', head, 0x0c, bank.stored + len(tail), unpacked, n, gap)
    assert off < 1 << 32
    out = bytes(head) + bytes(table) + bytes(layouts) + bytes(bank.b[bank.start:]) + bytes(tail)
    assert len(out) == start + bank.stored + len(tail)
    return out
