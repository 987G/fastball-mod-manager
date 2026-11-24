"""Read and write MLB The Show 26 dialog scene files (romfs PS4/PRESENTATION/DIALOGSCENE*.DSD), game 1.0.20.

Layout (from the game's own readers; addresses are the port's guest addresses):
  file (loader f_700178a5fc): u32 version (16), u32 nScenes, u32 nExtra, nScenes x {u32 sceneId, u32 offset},
       nExtra x u32, then the blob; a scene record sits at blob + offset (lookup f_700178aba0).
  scene (f_70017898f0): 0x2c-byte header {u32 id, u32, u32, u64, u32, u32, u16 len1, u16 len2, u64,
       u8 nA, u8 nB, u8 nC, u8 nSteps}, name1 (len1+1 bytes if len1), name2 (len2+1 if len2), nA x u32,
       nB x 16 bytes, nC x 24 bytes, nSteps steps. Links are indexes into these lists (f_70017895d0).
  step (f_7001788e00): {u32 id, u32 next step index, u16 nNodes, u16 pad}, then nNodes nodes.
  node (f_70017883f0): {u8 kind, u8, u8 flag, u8, u32 id} + a kind-specific payload (see read_node).
     kind 2 = reply option {s16 target step index, u16 reply id, u32 textLen} + text;
     kind 4 = event {u8 len1, u8 type, u8 len2, u8, u32 value} + str1 + str2 (type 0x10 PlayScene: value = scene id);
     kind 6 = option cap etc. (byte 4 = how many options to show);
     kind 7 = game action {u16 action, s16 arg, u32 value} (action 0xc = set quirk bit arg to value != 0).

usage:  python -I dsd.py check <file.DSD>            round-trip every scene, report
        python -I dsd.py dump <file.DSD> <sceneId>   print one scene
"""
import struct
import sys


class Reader:
    def __init__(self, b, p=0):
        self.b, self.p = b, p

    def take(self, n):
        v = self.b[self.p:self.p + n]
        if len(v) != n:
            raise ValueError('read past end')
        self.p += n
        return v

    def u8(self): return self.take(1)[0]
    def u16(self): return struct.unpack('<H', self.take(2))[0]
    def u32(self): return struct.unpack('<I', self.take(4))[0]

    def text(self, n):
        """n chars + NUL when n > 0 (the game copies n + 1 bytes)."""
        return self.take(n + 1) if n else b''


def read_child(r):
    """kind-0 child record (f_7001788050): 40 bytes, optional nested child, then nList x u32."""
    raw = r.take(40)
    n_list = struct.unpack_from('<H', raw, 6)[0]
    has_child = struct.unpack_from('<i', raw, 36)[0]
    child = read_child(r) if has_child >= 1 else None   # the game also needs version >= 8 (always 16 here)
    lst = r.take(4 * n_list) if 1 <= n_list <= 0xfe else b''
    return {'raw': raw, 'child': child, 'list': lst}


def write_child(c):
    return c['raw'] + (write_child(c['child']) if c['child'] else b'') + c['list']


def read_node(r):
    hdr = r.take(8)
    kind = hdr[0]
    start = r.p
    n = {'kind': kind, 'hdr': hdr}
    if kind == 0:
        a, cnt = struct.unpack('<HH', r.take(4))
        n['children'] = [read_child(r) for _ in range(cnt)] if 1 <= cnt <= 0xfe else []
    elif kind == 1:
        cnt = r.u16()
        if 1 <= cnt <= 0xfe:
            r.take(4 * cnt)
    elif kind == 2:
        _, _, ln = struct.unpack('<hHI', r.take(8))
        r.text(ln)
    elif kind == 3:
        _, cnt, _, _ = struct.unpack('<HBBI', r.take(8))
        if 1 <= cnt <= 0xfe:
            r.take(4 * cnt)
    elif kind == 4:
        l1, _, l2, _, _ = struct.unpack('<BBBBI', r.take(8))
        r.text(l1)
        r.text(l2)
    elif kind == 5:
        _, _, l1, l2 = struct.unpack('<hhHH', r.take(8))
        r.text(l1)
        r.text(l2)
    elif kind == 6:
        body = r.take(16)
        r.text(struct.unpack_from('<H', body, 2)[0])
    elif kind == 7:
        r.take(8)
    elif kind in (8, 0xc, 0xd):
        r.take(4)
    elif kind == 9:
        r.take(8)
    elif kind == 10:
        r.take(12)
    elif kind == 0xb:
        ln, _, _ = struct.unpack('<HBB', r.take(4))
        r.text(ln)
    else:
        raise ValueError(f'unknown node kind {kind} at {r.p - 8:#x}')
    n['body'] = r.b[start:r.p]
    return n


def write_node(n):
    return n['hdr'] + n['body']


def read_step(r):
    sid, nxt, cnt, pad = struct.unpack('<IIHH', r.take(12))
    nodes = [read_node(r) for _ in range(cnt)] if 1 <= cnt <= 0xfe else []
    return {'id': sid, 'next': nxt, 'pad': pad, 'nodes': nodes, 'ncount': cnt}


def write_step(s):
    return struct.pack('<IIHH', s['id'], s['next'], s['ncount'], s['pad']) + b''.join(map(write_node, s['nodes']))


def read_scene(b, off):
    r = Reader(b, off)
    hdr = r.take(0x2c)
    len1, len2 = struct.unpack_from('<HH', hdr, 0x1c)
    na, nb, nc, ns = hdr[0x28], hdr[0x29], hdr[0x2a], hdr[0x2b]
    sc = {'hdr': bytearray(hdr), 'name1': r.text(len1), 'name2': r.text(len2)}
    sc['A'] = r.take(4 * na) if 1 <= na <= 0xfe else b''
    sc['B'] = r.take(16 * nb) if 1 <= nb <= 0xfe else b''
    sc['C'] = r.take(24 * nc) if 1 <= nc <= 0xfe else b''
    sc['steps'] = [read_step(r) for _ in range(ns)] if 1 <= ns <= 0xfe else []
    sc['end'] = r.p
    return sc


def write_scene(sc):
    h = sc['hdr']
    h[0x2b] = len(sc['steps'])
    return bytes(h) + sc['name1'] + sc['name2'] + sc['A'] + sc['B'] + sc['C'] + b''.join(map(write_step, sc['steps']))


class Dsd:
    def __init__(self, data):
        self.version, n1, n2 = struct.unpack_from('<III', data, 0)
        self.index = [struct.unpack_from('<II', data, 12 + 8 * i) for i in range(n1)]   # (sceneId, offset)
        base = 12 + 8 * n1
        self.extra = [struct.unpack_from('<I', data, base + 4 * i)[0] for i in range(n2)]
        self.blob = data[base + 4 * n2:]

    def scene(self, sid):
        for i, off in self.index:
            if i == sid:
                return read_scene(self.blob, off)
        raise KeyError(sid)

    def build(self, replace=None, add=None):
        """New file bytes. replace: {sceneId: scene dict}; add: [(sceneId, scene dict)] appended to the blob.
        Untouched scenes keep their bytes; replaced ones are re-written at the end of the blob."""
        replace, add = replace or {}, add or []
        blob = bytearray(self.blob)
        index = []
        for sid, off in self.index:
            if sid in replace:
                index.append((sid, len(blob)))
                blob += write_scene(replace[sid])
            else:
                index.append((sid, off))
        for sid, sc in add:
            index.append((sid, len(blob)))
            blob += write_scene(sc)
        out = struct.pack('<III', self.version, len(index), len(self.extra))
        out += b''.join(struct.pack('<II', *e) for e in index)
        out += b''.join(struct.pack('<I', e) for e in self.extra)
        return out + bytes(blob)


def describe(sc):
    lines = [f"scene name1={sc['name1'][:-1]!r} name2={sc['name2'][:-1]!r} A={len(sc['A'])//4} "
             f"B={len(sc['B'])//16} C={len(sc['C'])//24} steps={len(sc['steps'])}"]
    for si, st in enumerate(sc['steps']):
        lines.append(f"  step {si}: id {st['id']} next {st['next']:#x} nodes {len(st['nodes'])}")
        for n in st['nodes']:
            k, body = n['kind'], n['body']
            info = ''
            if k == 2:
                tgt, rid, ln = struct.unpack_from('<hHI', body)
                info = f'option -> step {tgt} reply {rid}: {body[8:8+ln].decode("utf-8", "replace")!r}'
            elif k == 4:
                l1, typ, l2, b3, val = struct.unpack_from('<BBBBI', body)
                s1 = body[8:8 + l1].decode('utf-8', 'replace') if l1 else ''
                s2 = body[8 + (l1 + 1 if l1 else 0):][:l2].decode('utf-8', 'replace') if l2 else ''
                info = f'event type {typ:#x} value {val} b3 {b3} s1 {s1[:60]!r} s2 {s2[:60]!r}'
            elif k == 7:
                act, arg, val = struct.unpack_from('<HhI', body)
                info = f'action {act:#x} arg {arg} value {val}'
            elif k == 6:
                info = f'cap/body {body[:16].hex()} show {body[4]}'
            elif k == 0xb:
                ln = struct.unpack_from('<H', body)[0]
                info = repr(body[4:4 + ln].decode('utf-8', 'replace')[:90])
            elif k == 5:
                _, _, l1, l2 = struct.unpack_from('<hhHH', body)
                info = repr(body[8:8 + l1].decode('utf-8', 'replace')[:90])
            else:
                info = body[:16].hex()
            lines.append(f"    k{k:<2} id {struct.unpack_from('<I', n['hdr'], 4)[0]:<6} hdr {n['hdr'][:4].hex()} {info}")
    return '\n'.join(lines)


def check(path):
    data = open(path, 'rb').read()
    d = Dsd(data)
    offs = sorted(o for _, o in d.index)
    nxt = {o: (offs[i + 1] if i + 1 < len(offs) else len(d.blob)) for i, o in enumerate(offs)}
    bad = gap = 0
    for sid, off in d.index:
        try:
            sc = read_scene(d.blob, off)
        except Exception as e:
            bad += 1
            print('parse fail', sid, e)
            continue
        if write_scene(sc) != d.blob[off:sc['end']]:
            bad += 1
            print('round-trip mismatch', sid)
        if sc['end'] != nxt[off]:
            gap += 1
    print(f'{path}: {len(d.index)} scenes, {bad} bad, {gap} with bytes after the parsed end')
    print('rebuild identical:', d.build() == data)


if __name__ == '__main__':
    if sys.argv[1] == 'check':
        check(sys.argv[2])
    elif sys.argv[1] == 'dump':
        print(describe(Dsd(open(sys.argv[2], 'rb').read()).scene(int(sys.argv[3]))))
