r"""Pitching++ trajectory pictures, the install step: Python standard library only (players' PCs run the Mod Manager's
plain bundled Python). Makes the same pictures as pitch_pictures.py (numpy), from the player's own FRONTEND.TEX.

What comes from where:
  the game (the player's FRONTEND.TEX, read at install; nothing of it ships)
      the ball and its clear outline: cut from the game's fastball_4seam picture
      each trail's colour: the median trail colour of the type's own game picture (the airbender: the changeup's)
  this mod (pitch_pictures_masks.bin, made by `pitch_pictures.py masks` from make_shapes_table.py TYPES)
      each type's trail: coverage x scuffs x the clear ring around the ball, 8-bit, and where the ball sits
The masks file records the TYPES rows it was drawn from; on a PC that has make_shapes_table.py and numpy (the mod's
own tree) a stale file is redrawn first, so the pictures follow TYPES. make_shapes_table.py does not need to ship.

  frontend_tex(data) -> new FRONTEND.TEX bytes (the 19 pictures redrawn, breaking_airbender added)
  pictures(data)     -> {texture name: bytes, 512 x 512 RGBA rows}
BC7 here: decode = the full format (all 8 modes); encode = modes 6 / 5 / 4, chosen per block by error, fitted along
the one colour line these pictures have (trail colour -> white).
"""
import struct
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
MASKS = HERE / 'pitch_pictures_masks.bin'
MAGIC = b'PPMASK1\0'
SIZE = 512
ENTRY = 0x140
NAMES = ['fastball_4seam', 'fastball_sinker', 'curve_overhand', 'curve_slider', 'curve_slurve', 'breaking_splitter',
         'breaking_changeup', 'breaking_knuckleball', 'fastball_2seam', 'fastball_cutter', 'breaking_circlechange',
         'breaking_palmball', 'breaking_forkball', 'curve_knucklecurve', 'breaking_screwball', 'curve_12-6',
         'curve_sweeping', 'curve_sweeper', 'breaking_vulcanchange']
AIRBENDER = 'breaking_airbender'
COLOUR_FROM = {AIRBENDER: 'breaking_changeup'}
BALL_R = 44.5


# ---- the masks file ------------------------------------------------------------------------------------------------------

def read_masks(path=MASKS):
    """-> (rows [(name, type label, hb, ivb, end x, end y)], {name: bytes 512*512 alpha})."""
    d = Path(path).read_bytes()
    assert d[:8] == MAGIC, '%s is not a masks file' % path
    n = struct.unpack_from('<I', d, 8)[0]
    rows, o = [], 12
    for _ in range(n):
        name, label = d[o:o + 32].split(b'\0')[0].decode(), d[o + 32:o + 40].split(b'\0')[0].decode()
        hb, ivb, ex, ey = struct.unpack_from('<4f', d, o + 40)
        rows.append((name, label, hb, ivb, ex, ey))
        o += 56
    raw = zlib.decompress(d[o:])
    assert len(raw) == n * SIZE * SIZE
    return rows, {r[0]: raw[k * SIZE * SIZE:(k + 1) * SIZE * SIZE] for k, r in enumerate(rows)}


def ensure_masks():
    """In the mod's own tree (make_shapes_table.py present): redraw the masks when TYPES changed (needs numpy there).
    On a player's PC the shipped file is used as it is."""
    src = HERE / 'make_shapes_table.py'
    if not src.exists():
        return
    try:
        import pitch_pictures   # the drawing side (numpy); it reads TYPES the same way
    except ImportError:
        print('pitch pictures: numpy missing, %s used as it is' % MASKS.name)
        return
    want = [(t[0], round(t[1], 3), round(t[2], 3)) for t in pitch_pictures.types()]
    have = None
    if MASKS.exists():
        rows, _ = read_masks()
        have = [(r[1], round(r[2], 3), round(r[3], 3)) for r in rows]
    if have != want:
        data = pitch_pictures.masks_file()
        tmp = MASKS.with_name(MASKS.name + '.tmp')
        tmp.write_bytes(data)
        tmp.replace(MASKS)
        print('pitch pictures: %s redrawn from TYPES' % MASKS.name)


# ---- the bank: entries, LZ4, block-linear --------------------------------------------------------------------------------

def lz4(src, size):
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        tok = src[i]
        i += 1
        lit = tok >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        out += src[i:i + lit]
        i += lit
        if i >= n:
            break
        off = src[i] | (src[i + 1] << 8)
        i += 2
        ml = tok & 15
        if ml == 15:
            while True:
                b = src[i]
                i += 1
                ml += b
                if b != 255:
                    break
        ml += 4
        s = len(out) - off
        if off >= ml:
            out += out[s:s + ml]
        else:
            for k in range(ml):
                out.append(out[s + k])
    assert len(out) == size, 'LZ4 block unpacked to %d bytes, expected %d' % (len(out), size)
    return bytes(out)


def entries(data):
    n = struct.unpack_from('<I', data, 0x14)[0]
    out = {}
    for i in range(n):
        o = 0x30 + i * ENTRY
        out[bytes(data[o:o + 64]).split(b'\0')[0].decode('latin1').lower()] = bytes(data[o:o + ENTRY])
    return out


def level0(data, e):
    unpacked, stored = struct.unpack_from('<II', e, 0xc0)
    off = struct.unpack_from('<I', e, 0x124)[0]
    raw = bytes(data[off:off + stored])
    if e[0xd0] == 0:
        return raw
    assert e[0xd0] == 1, 'block is neither LZ4 nor stored'
    return lz4(raw, unpacked)


def _block_height(rows):
    gobs, bh = (rows + 7) // 8, 1
    while bh < gobs and bh < 16:
        bh *= 2
    return bh


def block_offsets(bw, bh):
    """Offset of each 16-byte block (row-major, bw x bh blocks) in Tegra block-linear data."""
    width_bytes = bw * 16
    gbh = _block_height(bh)
    gobs_x = (width_bytes + 63) // 64
    out = []
    for y in range(bh):
        for bx in range(bw):
            x = bx * 16
            gob = (y // (8 * gbh)) * (gobs_x * gbh * 512) + (x // 64) * (gbh * 512) + ((y % (8 * gbh)) // 8) * 512
            xi, yi = x % 64, y % 8
            out.append(gob + (xi // 32) * 256 + (yi // 2) * 64 + ((xi % 32) // 16) * 32 + (yi % 2) * 16)
    return out


# ---- BC7 decode ----------------------------------------------------------------------------------------------------------

_P2 = ('001100110011001100010001000100010111011101110111000100110011011100000001000100110011011101111111'
       '000100110111111100000001001101110000000000010011001101111111111100000001011111110000000000010111'
       '000101111111111100000000111111110000111111111111000000000000111100001000111011110111000100000000'
       '000000001000111001110011000100000011000100000000000010001100111000000000100011000111001100110001'
       '001100010001000000001000100011000110011001100110001101100110110000010111111010000000111111110000'
       '011100011000111000111001100111000101010101010101000011110000111101011010010110100011001111001100'
       '001111000011110001010101101010100110100101101001010110101010010101110011110011100001001111001000'
       '001100100100110000111011110111000110100110010110001111001100001101100110100110010000011001100000'
       '010011100100000000100111001000000000001001110010000001001110010001101100100100110011011011001001'
       '011000111001110000111001110001100110110011001001011000110011100101111110100000010001100011100111'
       '0000111100110011001100111111000000100010111011100100010001110111')
_P3 = ('001100110221222200010011221122210000200122112211022200220011011100000000112211220011001100220022'
       '002200221111111100110011221122110000000011112222000011111111222200001111222222220012001200120012'
       '011201120112011201220122012201220011011211221222001120012200222000010011011211220111001120012200'
       '000011221122112200220022002211110111011102220222000100012221222100000011012201220000110022102210'
       '012201220011000000120012112222220110122112210110000001101221122100221102110200220110011020022222'
       '001101220122001100002000221122210000000211221222022200220012001100110012002202220120012001200120'
       '000011112222000001201201201201200120201212010120001122001122001100111122220000110101010122222222'
       '000000002121212100221122002211220022001100220011022012210220122101012222222201010000212121212121'
       '010101010101222202220111022201110002111200021112000021122112211202220111011102220002111211120002'
       '011001100110222200000000211221120110011022222222002200110011002200221122112200220000000000002112'
       '0002000100020001022212220222122201012222222222220111201122012220')
_A2 = [15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 2, 8, 2, 2, 8, 8, 15, 2, 8, 2, 2, 8, 8, 2, 2,
       15, 15, 6, 8, 2, 8, 15, 15, 2, 8, 2, 2, 2, 15, 15, 6, 6, 2, 6, 8, 15, 15, 2, 2, 15, 15, 15, 15, 15, 2, 2, 15]
_A3A = [3, 3, 15, 15, 8, 3, 15, 15, 8, 8, 6, 6, 6, 5, 3, 3, 3, 3, 8, 15, 3, 3, 6, 10, 5, 8, 8, 6, 8, 5, 15, 15,
        8, 15, 3, 5, 6, 10, 8, 15, 15, 3, 15, 5, 15, 15, 15, 15, 3, 15, 5, 5, 5, 8, 5, 10, 5, 10, 8, 13, 15, 12, 3, 3]
_A3B = [15, 8, 8, 3, 15, 15, 3, 8, 15, 15, 15, 15, 15, 15, 15, 8, 15, 8, 15, 3, 15, 8, 15, 8, 3, 15, 6, 10, 15, 15,
        10, 8, 15, 3, 15, 10, 10, 8, 9, 10, 6, 15, 8, 15, 3, 6, 6, 8, 15, 3, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15,
        3, 15, 15, 8]
_PART2 = [[int(c) for c in _P2[16 * p:16 * p + 16]] for p in range(64)]
_PART3 = [[int(c) for c in _P3[16 * p:16 * p + 16]] for p in range(64)]
# subsets, partition bits, rotation bits, index-selection bits, colour bits, alpha bits, p-bits per endpoint,
# p-bits shared per subset, index bits, second index bits
_MODES = {0: (3, 4, 0, 0, 4, 0, 1, 0, 3, 0), 1: (2, 6, 0, 0, 6, 0, 0, 1, 3, 0), 2: (3, 6, 0, 0, 5, 0, 0, 0, 2, 0),
          3: (2, 6, 0, 0, 7, 0, 1, 0, 2, 0), 4: (1, 0, 2, 1, 5, 6, 0, 0, 2, 3), 5: (1, 0, 2, 0, 7, 8, 0, 0, 2, 2),
          6: (1, 0, 0, 0, 7, 7, 1, 0, 4, 0), 7: (2, 6, 0, 0, 5, 5, 1, 0, 2, 0)}
W = {2: [0, 21, 43, 64], 3: [0, 9, 18, 27, 37, 46, 55, 64],
     4: [0, 4, 9, 13, 17, 21, 26, 30, 34, 38, 43, 47, 51, 55, 60, 64]}


def _expand(v, bits):
    return (v << (8 - bits)) | (v >> (2 * bits - 8))


def bc7_decode_block(blk):
    """16 bytes -> 16 (r, g, b, a) tuples, texel k = 4 * row + column."""
    v = int.from_bytes(blk, 'little')
    m = 0
    while m < 8 and not (v >> m) & 1:
        m += 1
    if m == 8:
        return [(0, 0, 0, 0)] * 16
    ns, pb, rb, isb, cb, ab, epb, spb, ib, ib2 = _MODES[m]
    pos = m + 1

    def take(k):
        nonlocal pos
        r = (v >> pos) & ((1 << k) - 1)
        pos += k
        return r
    part, rot, swap = take(pb), take(rb), take(isb)
    ne = 2 * ns
    ep = [[0, 0, 0, 0] for _ in range(ne)]
    for ch in range(3):
        for e in range(ne):
            ep[e][ch] = take(cb)
    for e in range(ne):
        ep[e][3] = take(ab) if ab else 0
    cbits, abits = cb, ab
    if epb:
        for e in range(ne):
            p = take(1)
            ep[e] = [(x << 1) | p for x in ep[e]]
        cbits, abits = cb + 1, (ab + 1 if ab else 0)
    if spb:
        for s in range(ns):
            p = take(1)
            for e in (2 * s, 2 * s + 1):
                ep[e] = [(x << 1) | p for x in ep[e]]
        cbits = cb + 1
    for e in range(ne):
        for ch in range(3):
            ep[e][ch] = _expand(ep[e][ch], cbits)
        ep[e][3] = _expand(ep[e][3], abits) if ab else 255
    sub = [0] * 16 if ns == 1 else (_PART2 if ns == 2 else _PART3)[part]
    anchors = {0}
    if ns == 2:
        anchors.add(_A2[part])
    elif ns == 3:
        anchors |= {_A3A[part], _A3B[part]}

    def indices(k, anch):
        out = []
        for t in range(16):
            out.append(take(k - 1 if t in anch else k))
        return out
    i1 = indices(ib, anchors)
    wc = wa = [W[ib][i] for i in i1]
    if ib2:
        w2 = [W[ib2][i] for i in indices(ib2, {0})]
        wc, wa = (w2, wc) if swap else (wc, w2)
    out = []
    for t in range(16):
        e0, e1 = ep[2 * sub[t]], ep[2 * sub[t] + 1]
        c = [((64 - wc[t]) * e0[ch] + wc[t] * e1[ch] + 32) >> 6 for ch in range(3)]
        a = ((64 - wa[t]) * e0[3] + wa[t] * e1[3] + 32) >> 6
        px = c + [a]
        if rot:
            px[3], px[rot - 1] = px[rot - 1], px[3]
        out.append(tuple(px))
    return out


def decode_picture(raw, w=SIZE, h=SIZE):
    """BC7, block-linear -> list of rows, each a list of (r, g, b, a). Same blocks are decoded once."""
    bw, bh = w // 4, h // 4
    offs = block_offsets(bw, bh)
    rows = [[None] * w for _ in range(h)]
    cache = {}
    for k, o in enumerate(offs):
        blk = raw[o:o + 16]
        px = cache.get(blk)
        if px is None:
            px = cache[blk] = bc7_decode_block(blk)
        by, bx = divmod(k, bw)
        for t in range(16):
            rows[by * 4 + t // 4][bx * 4 + t % 4] = px[t]
    return rows


# ---- drawing -------------------------------------------------------------------------------------------------------------

def _median(xs):
    s = sorted(xs)
    n = len(s)
    return float(s[n // 2]) if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _max_alpha(blk):
    """The block's highest alpha without decoding it (modes 4-6, no rotation: alpha lies between the two alpha
    endpoints), or None when it has to be decoded to know."""
    v = int.from_bytes(blk, 'little')
    if v & 0x40 and not v & 0x3f:                            # mode 6: 7-bit alpha + p-bit per endpoint
        return max(((v >> 49) & 0x7f) << 1 | (v >> 63) & 1, ((v >> 56) & 0x7f) << 1 | (v >> 64) & 1)
    if v & 0x20 and not v & 0x1f and not (v >> 6) & 3:      # mode 5, no rotation: 8-bit alpha
        return max((v >> 50) & 0xff, (v >> 58) & 0xff)
    if v & 0x10 and not v & 0xf and not (v >> 5) & 3:       # mode 4, no rotation: 6-bit alpha
        return max(_expand((v >> 38) & 0x3f, 6), _expand((v >> 44) & 0x3f, 6))
    return None


def trail_colour_raw(raw):
    """Median colour of the texels with alpha over 200 that are not white (the trail), per channel, decoding only the
    blocks that can hold such a texel."""
    pts = []
    for o in range(0, len(raw), 16):
        blk = raw[o:o + 16]
        m = _max_alpha(blk)
        if m is not None and m <= 200:
            continue
        pts += [p for p in bc7_decode_block(blk) if p[3] > 200 and not min(p[0], p[1], p[2]) > 235]
    return tuple(_median([p[ch] for p in pts]) for ch in range(3))


def trail_colour(pic):
    pts = [p for row in pic for p in row if p[3] > 200 and not min(p[0], p[1], p[2]) > 235]
    return tuple(_median([p[ch] for p in pts]) for ch in range(3))


def ball_sprite(pic):
    """The ball cut out of the game's fastball_4seam picture: (R, {(dy, dx): alpha}, (ox, oy)), as pitch_pictures."""
    ys, xs = [], []
    for y, row in enumerate(pic):
        for x, p in enumerate(row):
            if min(p[0], p[1], p[2]) > 235 and p[3] > 200:
                ys.append(y)
                xs.append(x)
    cy, cx = (min(ys) + max(ys)) / 2.0, (min(xs) + max(xs)) / 2.0
    R = 46
    iy, ix = int(round(cy)), int(round(cx))
    sprite = {}
    for dy in range(-R, R):
        for dx in range(-R, R):
            yy, xx = dy + 0.5 + (iy - cy), dx + 0.5 + (ix - cx)
            inside = min(1.0, max(0.0, BALL_R - (yy * yy + xx * xx) ** 0.5 + 0.5))
            a = pic[iy + dy][ix + dx][3] * inside
            if a > 0:
                sprite[(dy, dx)] = a
    return R, sprite, (cx - ix, cy - iy)


def compose(mask, colour, end, ball):
    """One picture: the trail's alpha mask in the trail colour, the ball pasted on its end -> 512*512*4 bytes."""
    R, sprite, (ox, oy) = ball
    alpha = [float(v) for v in mask]
    rgb = {}
    ix, iy = int(round(end[0] - ox)), int(round(end[1] - oy))
    for (dy, dx), sa255 in sprite.items():
        y, x = iy + dy, ix + dx
        if not (0 <= x < SIZE and 0 <= y < SIZE):
            continue
        k = y * SIZE + x
        sa, da = sa255 / 255.0, alpha[k] / 255.0
        oa = sa + da * (1 - sa)
        rgb[k] = tuple((255.0 * sa + colour[ch] * da * (1 - sa)) / max(oa, 1e-6) for ch in range(3))
        alpha[k] = oa * 255
    base = bytes(int(round(c)) for c in colour)
    out = bytearray(base + b'\0') * (SIZE * SIZE)
    for k, a in enumerate(alpha):
        if a > 0:
            out[4 * k + 3] = min(255, max(0, int(round(a))))
    for k, c in rgb.items():
        if alpha[k] > 0:
            out[4 * k:4 * k + 3] = bytes(min(255, max(0, int(round(v)))) for v in c)
    return bytes(out)


def pictures(data):
    """{texture name: 512*512 RGBA bytes} for the 19 game pictures and the airbender's, from the player's bank."""
    ensure_masks()
    rows, masks = read_masks()
    ent = entries(data)
    colour = {n: trail_colour_raw(level0(data, ent[n])) for n in NAMES}
    ball = ball_sprite(decode_picture(level0(data, ent['fastball_4seam'])))
    out = {}
    for name, label, hb, ivb, ex, ey in rows:
        col = colour[COLOUR_FROM.get(name, name)]
        out[name] = compose(masks[name], col, (ex, ey), ball)
    return out


# ---- BC7 encode ----------------------------------------------------------------------------------------------------------

def _quant(v, bits):
    m = (1 << bits) - 1
    return min(m, max(0, int(round(v * m / 255.0))))


def _put(acc, pos, val, nbits):
    return acc | ((val & ((1 << nbits) - 1)) << pos), pos + nbits


def _fit1(vals, bits, levels, weights=None):
    """1-D endpoints for vals (16 floats) with `bits`-bit codes and the given weight table: tries the min/max codes and
    their neighbours, keeps the least error. -> (code0, code1, indices, error)."""
    lo, hi = min(vals), max(vals)
    wt = weights or [1.0] * 16
    best = None
    c_lo, c_hi = _quant(lo, bits), _quant(hi, bits)
    top = (1 << bits) - 1
    for c0 in {max(0, c_lo - 1), c_lo, min(top, c_lo + 1)}:
        for c1 in {max(0, c_hi - 1), c_hi, min(top, c_hi + 1)}:
            e0, e1 = _expand(c0, bits), _expand(c1, bits)
            pal = [((64 - w) * e0 + w * e1 + 32) >> 6 for w in levels]
            idx, err = [], 0.0
            for v, wv in zip(vals, wt):
                j = min(range(len(pal)), key=lambda i: (pal[i] - v) ** 2)
                idx.append(j)
                err += (pal[j] - v) ** 2 * wv
            if best is None or err < best[3]:
                best = (c0, c1, idx, err)
    return best


def _fit_rgb(px, cov, bits, levels):
    """Colours on one line: endpoints at the two texels furthest apart (alpha-weighted), codes rounded per channel,
    indices by nearest. -> (c0 [3], c1 [3], indices, error)."""
    pts = [p[:3] for p in px]
    a, b = pts[0], pts[0]
    far = -1
    for i in range(16):
        for j in range(i + 1, 16):
            d = sum((pts[i][c] - pts[j][c]) ** 2 for c in range(3))
            if d > far:
                far, a, b = d, pts[i], pts[j]
    best = None
    for p0, p1 in ((a, b), (b, a)):
        c0 = [_quant(p0[c], bits) for c in range(3)]
        c1 = [_quant(p1[c], bits) for c in range(3)]
        e0 = [_expand(x, bits) for x in c0]
        e1 = [_expand(x, bits) for x in c1]
        pal = [[((64 - w) * e0[c] + w * e1[c] + 32) >> 6 for c in range(3)] for w in levels]
        idx, err = [], 0.0
        for p, wv in zip(pts, cov):
            j = min(range(len(pal)), key=lambda i: sum((pal[i][c] - p[c]) ** 2 for c in range(3)))
            idx.append(j)
            err += sum((pal[j][c] - p[c]) ** 2 for c in range(3)) * wv
        if best is None or err < best[3]:
            best = (c0, c1, idx, err)
    return best


def _anchor(c0, c1, idx, nbits):
    top = 1 << (nbits - 1)
    if idx[0] >= top:
        return c1, c0, [2 * top - 1 - i for i in idx]
    return c0, c1, idx


_W4 = W[4]
# nearest 4-bit index for a position 0..64 along the line
_NEAR4 = [min(range(16), key=lambda i: abs(_W4[i] - t)) for t in range(65)]


def _endpoint6(v):
    """7-bit codes + p-bit for one RGBA endpoint, the p-bit that brings it closest."""
    best = None
    for p in (0, 1):
        c = [min(127, max(0, int(round((x - p) / 2.0)))) for x in v]
        e = sum((ci * 2 + p - x) ** 2 for ci, x in zip(c, v))
        if best is None or e < best[0]:
            best = (e, c, p)
    return best[1], best[2]


def _mode6(px, cov):
    """One RGBA line (7 bits + one p-bit per endpoint, 4-bit indices): endpoints at the lowest and highest alpha,
    each texel projected on the line and the nearest of three indices kept."""
    lo = min(range(16), key=lambda i: px[i][3])
    hi = max(range(16), key=lambda i: px[i][3])
    c0, p0 = _endpoint6(px[lo])
    c1, p1 = _endpoint6(px[hi])
    e0 = [x * 2 + p0 for x in c0]
    e1 = [x * 2 + p1 for x in c1]
    d = [e1[c] - e0[c] for c in range(4)]
    pal = [[((64 - w) * e0[c] + w * e1[c] + 32) >> 6 for c in range(4)] for w in _W4]
    idx, err = [], 0.0
    for p, wv in zip(px, cov):
        num = (p[0] - e0[0]) * d[0] * wv + (p[1] - e0[1]) * d[1] * wv + (p[2] - e0[2]) * d[2] * wv + (p[3] - e0[3]) * d[3]
        den = (d[0] * d[0] + d[1] * d[1] + d[2] * d[2]) * wv + d[3] * d[3]
        t = 0 if den <= 0 else min(64, max(0, int(round(64.0 * num / den))))
        j0 = _NEAR4[t]
        bj, be = j0, None
        for j in (j0 - 1, j0, j0 + 1):
            if 0 <= j < 16:
                q = pal[j]
                e = ((q[0] - p[0]) ** 2 + (q[1] - p[1]) ** 2 + (q[2] - p[2]) ** 2) * wv + (q[3] - p[3]) ** 2
                if be is None or e < be:
                    bj, be = j, e
        idx.append(bj)
        err += be
    c0, c1 = c0 + [p0], c1 + [p1]
    if idx[0] >= 8:
        c0, c1, idx = c1, c0, [15 - i for i in idx]
    acc, pos = _put(0, 0, 1 << 6, 7)
    for ch in range(4):
        acc, pos = _put(acc, pos, c0[ch], 7)
        acc, pos = _put(acc, pos, c1[ch], 7)
    acc, pos = _put(acc, pos, c0[4], 1)
    acc, pos = _put(acc, pos, c1[4], 1)
    for t, i in enumerate(idx):
        acc, pos = _put(acc, pos, i, 3 if t == 0 else 4)
    assert pos == 128
    return err, acc.to_bytes(16, 'little')


def _mode54(px, cov, mode):
    """Colour and alpha apart: mode 5 (7-bit colour, 8-bit alpha, 2-bit indices each) or mode 4 index mode 0 (5-bit
    colour with 2-bit indices, 6-bit alpha with 3-bit indices)."""
    if mode == 5:
        cbits, abits, cl, al = 7, 8, W[2], W[2]
    else:
        cbits, abits, cl, al = 5, 6, W[2], W[3]
    c0, c1, ci, ce = _fit_rgb(px, cov, cbits, cl)
    a0, a1, ai, ae = _fit1([p[3] for p in px], abits, al)
    c0, c1, ci = _anchor(c0, c1, ci, 2)
    a0, a1, ai = _anchor(a0, a1, ai, 2 if mode == 5 else 3)
    if mode == 5:
        acc, pos = _put(0, 0, 1 << 5, 6)
        acc, pos = _put(acc, pos, 0, 2)              # rotation: none
    else:
        acc, pos = _put(0, 0, 1 << 4, 5)
        acc, pos = _put(acc, pos, 0, 2)              # rotation: none
        acc, pos = _put(acc, pos, 0, 1)              # index mode 0: the 2-bit indices are the colour's
    for ch in range(3):
        acc, pos = _put(acc, pos, c0[ch], cbits)
        acc, pos = _put(acc, pos, c1[ch], cbits)
    acc, pos = _put(acc, pos, a0, abits)
    acc, pos = _put(acc, pos, a1, abits)
    for t, i in enumerate(ci):
        acc, pos = _put(acc, pos, i, 1 if t == 0 else 2)
    ab = 2 if mode == 5 else 3
    for t, i in enumerate(ai):
        acc, pos = _put(acc, pos, i, ab - 1 if t == 0 else ab)
    assert pos == 128, (mode, pos)
    return ce + ae, acc.to_bytes(16, 'little')


EMPTY = None


def bc7_encode_block(px):
    """16 (r, g, b, a) -> 16 bytes."""
    global EMPTY
    if not any(p[3] for p in px):
        if EMPTY is None:
            EMPTY = _mode6([(0, 0, 0, 0)] * 16, [0.001] * 16)[1]
        return EMPTY
    cov = [p[3] / 255.0 + 1e-3 for p in px]
    tot = sum(c - 1e-3 for c in cov) or 1e-9
    mean = [sum(p[c] * (cv - 1e-3) for p, cv in zip(px, cov)) / tot for c in range(3)]
    px = [tuple(p[:3]) + (p[3],) if p[3] else (mean[0], mean[1], mean[2], 0) for p in px]
    cands = [_mode6(px, cov)]
    if any(abs(p[c] - px[0][c]) > 2 for p in px for c in range(3)):   # more than one colour: try colour / alpha apart
        cands.append(_mode54(px, cov, 5))
        cands.append(_mode54(px, cov, 4))
    return min(cands, key=lambda c: c[0])[1]


def encode_picture(rgba, w=SIZE, h=SIZE):
    """w*h*4 RGBA bytes -> BC7 block-linear bytes."""
    bw, bh = w // 4, h // 4
    offs = block_offsets(bw, bh)
    out = bytearray(w * h)
    for k, o in enumerate(offs):
        by, bx = divmod(k, bw)
        px = []
        for t in range(16):
            i = ((by * 4 + t // 4) * w + bx * 4 + t % 4) * 4
            px.append(tuple(rgba[i:i + 4]))
        out[o:o + 16] = bc7_encode_block(px)
    return bytes(out)


def small(rgba):
    """The 32x32 stand-in: 16x16 box average with premultiplied alpha (as frontend_tex._small)."""
    out = bytearray(32 * 32 * 4)
    for by in range(32):
        for bx in range(32):
            s = [0.0, 0.0, 0.0]
            sa = 0.0
            for y in range(by * 16, by * 16 + 16):
                row = y * SIZE
                for x in range(bx * 16, bx * 16 + 16):
                    i = (row + x) * 4
                    a = rgba[i + 3] / 255.0
                    if a:
                        sa += a
                        for c in range(3):
                            s[c] += rgba[i + c] * a
            sa /= 256.0
            k = (by * 32 + bx) * 4
            if sa > 0:
                for c in range(3):
                    out[k + c] = min(255, max(0, int(round(s[c] / 256.0 / sa))))
            out[k + 3] = min(255, max(0, int(round(sa * 255))))
    return bytes(out)


def encode(rgba):
    """-> (level 0 bytes, stand-in bytes, colour floats) as frontend_tex.encode."""
    trail = []
    asum = 0
    for i in range(0, len(rgba), 4):
        r, g, b, a = rgba[i:i + 4]
        asum += a
        if a and not min(r, g, b) > 235:
            trail.append((r, g, b))
    col = [(_median([t[c] for t in trail]) if trail else 0.0) / 255.0 for c in range(3)]
    return encode_picture(rgba), encode_picture(small(rgba), 32, 32), \
        (col[0], col[1], col[2], asum / (SIZE * SIZE) / 255.0)


def tex_installed(data):
    """FRONTEND.TEX has the airbender picture (only the entry table is read)."""
    return AIRBENDER in entries(data)


def frontend_tex(data):
    """FRONTEND.TEX bytes as the game has them -> with the 19 pictures redrawn and the airbender's added."""
    import frontend_tex as ft
    assert AIRBENDER not in entries(data), 'FRONTEND.TEX already has the airbender picture'
    pics = pictures(data)
    return ft.build(data, {n: encode(p) for n, p in pics.items()}, {AIRBENDER: 'breaking_vulcanchange'})
