"""Give whole pitching staffs one delivery in a TEST COPY's rosters (PLAYERS.BIN, _LIVE, _INJ).
    python -I set_styles.py <test copy> <team byte>=<style> [...]      e.g. 27=8763 4=18019 (Dodgers, Blue Jays)
Team byte = SIGNED_PACK & 0xff. Files are replaced (mkmirror.write_new), never written through the install's hard links."""
import os, struct, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'tools'))
import le90
from mkmirror import write_new


def anim_offsets(d):
    """[(player record dict, offset of its D_PLYR_ANIMS record)] — read_tables' walk, keeping offsets."""
    tables, p = le90.parse_desc(d)
    sch = {t[0]: t[2] for t in tables}
    names = {t[0].encode() for t in tables}
    def rec(name, p):
        r = {}
        for fn, fid, ft, cnt, bs in sch[name]:
            raw = d[p:p + bs]; p += bs
            if ft == 1 and cnt > 1 and fn.endswith('NAME'): r[fn] = raw.split(b'\0')[0].decode('latin1')
            elif ft == 2 and cnt == 1: r[fn] = struct.unpack('<H', raw)[0]
            elif ft == 4 and cnt == 1: r[fn] = struct.unpack('<I', raw)[0]
            elif ft == 1 and cnt == 1: r[fn] = raw[0]
        return r, p
    isname = lambda p: d[p:p + 16].split(b'\0')[0] in names
    out = []; seen = set()
    assert d[p:p + 6] == b'[DATA]'; p += 6
    while p < len(d):
        name = d[p:p + 16].split(b'\0')[0].decode(); cnt = struct.unpack_from('<I', d, p + 16)[0]; p += 20
        seen.add(name)
        for i in range(cnt):
            r, p = rec(name, p)
            ch = {}
            while p < len(d) and isname(p):
                cn = d[p:p + 16].split(b'\0')[0].decode()
                if cn in seen or cn == name or cn in ch: break
                cc = struct.unpack_from('<I', d, p + 16)[0]
                if i == cnt - 1 and cn not in ('D_PLYR_CONTRACT', 'D_PLYR_ANIMS', 'D_PLYR_AUDIO', 'D_PLYR_FLAGS', 'D_QUIRKS',
                                               'D_STANCE_DATA', 'D_PLAYER_ACC', 'D_PLAYER_APP'): break
                q = p + 20
                for j in range(cc):
                    if cn == 'D_PLYR_ANIMS' and name == 'PLR_PROFS' and j == 0: ch['anim_off'] = q
                    rr, q = rec(cn, q)
                ch[cn] = True; p = q
            if name == 'PLR_PROFS' and 'anim_off' in ch:
                out.append((r, ch['anim_off']))
    return out


def main():
    game = sys.argv[1]
    want = {int(k): int(v) for k, v in (a.split('=') for a in sys.argv[2:])}
    for fn in ('PLAYERS.BIN', 'PLAYERS_LIVE.BIN', 'PLAYERS_INJ.BIN'):
        path = os.path.join(game, 'game', 'romfs', 'FRONTEND', 'STATS', fn)
        if not os.path.exists(path): continue
        b = bytearray(open(path, 'rb').read())
        n = 0
        for r, off in anim_offsets(bytes(b)):
            team = r['SIGNED_PACK'] & 0xff
            if r['POSITION'] == 0 and team in want:
                old = struct.unpack_from('<H', b, off + 4)[0]
                struct.pack_into('<H', b, off + 4, want[team])
                n += 1
                print('  %s %-22s team %d style %d -> %d' % (fn, r['FIRST_NAME'] + ' ' + r['LAST_NAME'], team, old, want[team]))
        write_new(path, bytes(b))
        check = {(r['FIRST_NAME'], r['LAST_NAME']): s for r, s in
                 ((p, (p.get('_children', {}).get('D_PLYR_ANIMS') or [{}])[0].get('PITCHING_STYLE'))
                  for p in le90.read_tables(open(path, 'rb').read())['PLR_PROFS']) if r['POSITION'] == 0 and (r['SIGNED_PACK'] & 0xff) in want}
        bad = [k for k, s in check.items() if s not in want.values()]
        print(fn, n, 'pitchers set;', 'verify OK' if not bad else 'VERIFY FAILED %s' % bad)


if __name__ == '__main__':
    main()
