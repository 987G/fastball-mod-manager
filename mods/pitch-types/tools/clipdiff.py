r"""Pitching++: byte differences between an edited clip and the game's own clips (standard library only; shipped).

A difference is a list of operations that rebuilds the edited clip from source bytes the player's own game files
hold: "copy n bytes from source offset o" or "insert these n new bytes". The shipped packs keep only these, never
the game's clips themselves (no game data in the release). deliveries_install.py applies them at install.

Encoding (one clip): unsigned LEB128 varints. For each operation a head h = n << 1 | kind; kind 1 = copy, followed by
the zigzag-coded change of the source offset from where the previous copy ended; kind 0 = insert, followed by the n
bytes. The source of a clip is the concatenation of the clips its pack entry names.
"""

W = 8   # the shortest copy: shorter matching runs go in as new bytes


def _put(out, v):
    while True:
        b = v & 0x7f
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return


def _get(buf, i):
    v = s = 0
    while True:
        b = buf[i]
        i += 1
        v |= (b & 0x7f) << s
        if b < 0x80:
            return v, i
        s += 7


def apply(ops, src):
    """the clip ops (bytes) rebuild from src (bytes)"""
    out = bytearray()
    i, at, n_ops = 0, 0, len(ops)
    while i < n_ops:
        h, i = _get(ops, i)
        n = h >> 1
        if h & 1:
            z, i = _get(ops, i)
            at += (z >> 1) ^ -(z & 1)
            if at < 0 or at + n > len(src):
                raise ValueError('copy outside the source')
            out += src[at:at + n]
            at += n
        else:
            out += ops[i:i + n]
            i += n
    return bytes(out)


def _extend(a, i, b, j):
    """how many bytes a[i:] and b[j:] have in common from the start"""
    n, la, lb = 0, len(a), len(b)
    step = 256
    while step >= 1:
        while i + n + step <= la and j + n + step <= lb and a[i + n:i + n + step] == b[j + n:j + n + step]:
            n += step
        step >>= 2
    return n


def make(target, src):
    """the ops that rebuild target from src (maker side; standard library too)"""
    index = {}
    for k in range(len(src) - W, -1, -1):   # the first position of each window wins
        index[src[k:k + W]] = k
    out = bytearray()
    lit = bytearray()
    at = 0          # where the last copy ended in src
    p, n_t = 0, len(target)
    expect = None   # the source position that continues the last copy's alignment

    def flush():
        if lit:
            _put(out, len(lit) << 1)
            out.extend(lit)
            lit.clear()

    while p < n_t:
        best_q, best_n = -1, 0
        if p + W <= n_t:
            for q in ((expect,) if expect is not None else ()) + (index.get(target[p:p + W], -1),):
                if q is None or q < 0 or q + W > len(src):
                    continue
                n = _extend(target, p, src, q)
                if n > best_n:
                    best_q, best_n = q, n
        if best_n >= W:
            # take back new bytes just before that also match (the copy grows backwards)
            back = 0
            while back < len(lit) and best_q - back > 0 and lit[-1 - back] == src[best_q - back - 1]:
                back += 1
            if back:
                del lit[len(lit) - back:]
                best_q -= back
                best_n += back
                p -= back
            flush()
            _put(out, best_n << 1 | 1)
            d = best_q - at
            _put(out, (d << 1) ^ (d >> 63) if d >= 0 else ((-d) << 1) - 1)
            at = best_q + best_n
            p += best_n
            expect = at
        else:
            lit.append(target[p])
            p += 1
            if expect is not None:
                expect += 1
    flush()
    return bytes(out)


def literal_runs(ops):
    """the lengths of the inserted (new) byte runs, for audits"""
    runs, i = [], 0
    while i < len(ops):
        h, i = _get(ops, i)
        n = h >> 1
        if h & 1:
            _, i = _get(ops, i)
        else:
            runs.append((i, n))
            i += n
    return runs
