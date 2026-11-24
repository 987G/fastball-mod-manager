r"""Build the quirk offer scenes (PHASE2.md section 3) into a TEST COPY of the game's DIALOGSCENEFE.DSD.

An offer block is inserted in front of a host scene's closing step(s), the way phase 1 did, so it plays with the host
scene's own speaker and camera. Every link into a closing step is pointed at the block's offer step. One block per
closing step (each continues to its own step). Contents of a block:
  offer step      the speaker's line + one option per quirk (reply 0x5100+bit) + fallback (0x51FF)
  warning step    hidden (nothing links to it): the two-way "few slots left" line the port module shows instead
  confirm steps   one per quirk: the quirk's detail text + Lock it in (0x5200+bit) / Pick something else (0x5300)
                  / Pass this year (0x5301)
  grant steps     one per quirk: game action 0xc (set quirk bit) then the closing step
The port module filters the offer step's options (role, owned, locks, earned, size); without it the game shows the
first 3 options. Texts come from quirks.json.

usage: python -E -s build_offers.py <test copy folder>        e.g. ...\mods\rtts-quirks\testgame
"""
import hashlib
import json
import os
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dsd  # noqa: E402

INSTALL = Path(r'C:\Games\MLB The Show 26')
REL = 'PS4/PRESENTATION/DIALOGSCENEFE.DSD'
END = 0xffffffff
REPLY_OFFER, REPLY_FALLBACK, REPLY_LOCK, REPLY_BACK, REPLY_PASS = 0x5100, 0x51FF, 0x5200, 0x5300, 0x5301

# host scenes: scene id -> (closing steps, speaker index, offer line)
HOSTS = {
    921072: ((9, 11), 1, "Before you go, pick one thing to work on this year."),
}
# the pass button per moment ("this year" fits the yearly offers, not the one-time college one)
PASS_LABEL = {921072: "I'll pass for now.", 948101: "I'm good, Coach.", 948102: "I'll pass this year."}
# New text-message scenes (phase 3): the port module puts them in the RTTS inbox at a new season (college: the first
# season at a school; spring: every pro season). Header copied from 927176 (a head-coach message scene: no actors),
# names replaced (spring: the agent, since the offer is queued at the offseason when the player has no manager). Steps: preview event (0x30, shown in the inbox) -> intro line -> [offer block] -> closing line +
# RemoveMessage (event 0x35, own id) so the offer cannot be replayed.
NEW_SCENES = {
    948101: {'moment': 'college', 'base': 927176, 'names': 927176,
             'preview': "Stop by my office before practice.",
             'intro': "Glad you picked us, [RTTS_PLAYER.PROFILE.FIRST_NAME].",
             'offer': "Pick one thing you want to work on with me.",
             'close': "Good. See you at practice."},
    948102: {'moment': 'spring', 'base': 927176, 'names': 228976,   # the agent: the only contact in the offseason
             'preview': "Spring training starts soon. Call me when you can.",
             'intro': "I looked over your numbers from last season, [RTTS_PLAYER.PROFILE.FIRST_NAME].",
             'offer': "Pick one thing to work on this spring.",
             'close': "Sounds good. Talk soon."},
}
MOMENT_OF_HOST = {921072: 'draft', **{sid: v['moment'] for sid, v in NEW_SCENES.items()}}
SPEAKER = 1

WARNING = "You've got a lot of these already. Room for only a few more."
FALLBACK = "I'm good for now."


def load_romfs_index():
    """romfs_index.py (keeps the game's romfs_list.tsv sizes right) from the first folder that has it, all found from
    this script's own place: <this mod>/_shared (a player's install), <mods>/_shared (the public repo),
    <mods>/rtts-equipment (the development PC)."""
    import importlib.util
    mod = HERE.parent
    for folder in (mod / '_shared', mod.parent / '_shared', mod.parent / 'rtts-equipment'):
        f = folder / 'romfs_index.py'
        if f.is_file():
            spec = importlib.util.spec_from_file_location('romfs_index', f)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise SystemExit('romfs_index.py not found in _shared or rtts-equipment beside this mod')


def text(t):
    return t.encode('utf-8')


def line(speaker, t):
    b = text(t)
    body = struct.pack('<BBHBBHII', speaker, 0, len(b), 0, 0, 0xffff, 0, 0) + b + b'\0'
    return {'kind': 6, 'hdr': bytes([6, 0, 0, 0]) + struct.pack('<I', 0), 'body': body}


def option(target, reply, t):
    b = text(t)
    return {'kind': 2, 'hdr': bytes([2, 0, 0, 0]) + struct.pack('<I', 0),
            'body': struct.pack('<hHI', target, reply, len(b)) + b + b'\0'}


def action(act, arg, value):
    return {'kind': 7, 'hdr': bytes([7, 0, 0, 0]) + struct.pack('<I', 0), 'body': struct.pack('<HhI', act, arg, value)}


def event(typ, s2=b'', value=0):
    return {'kind': 4, 'hdr': bytes([4, 0, 0, 0]) + struct.pack('<I', 0),
            'body': struct.pack('<BBBBI', 0, typ, len(s2), 0, value) + s2 + b'\0'}


def new_scene(d, sid, spec):
    """A message scene: preview -> intro -> step 2 (closing); add_block then inserts the offer before step 2."""
    base, names = d.scene(spec['base']), d.scene(spec['names'])
    hdr = bytearray(base['hdr'])
    struct.pack_into('<I', hdr, 0, sid)
    struct.pack_into('<HH', hdr, 0x1c, len(names['name1']) - 1, len(names['name2']) - 1)   # lengths exclude the NUL
    hdr[0x20:0x24] = names['hdr'][0x20:0x24]   # contact id (agent scenes 143, head coach 221): picks the chat photo
    hdr[0x28] = hdr[0x29] = hdr[0x2a] = 0          # no actor / camera tables
    sc = {'hdr': hdr, 'name1': names['name1'], 'name2': names['name2'], 'A': b'', 'B': b'', 'C': b'', 'steps': []}
    sc['steps'] = [step([event(0x30, text(spec['preview']))], 1),
                   step([line(SPEAKER, spec['intro'])], 2),
                   step([line(SPEAKER, spec['close']), event(0x35, str(sid).encode())], END)]
    return sc


def step(nodes, nxt):
    return {'id': 0, 'next': nxt, 'pad': 0, 'nodes': nodes, 'ncount': len(nodes)}


def retarget(sc, old, new):
    n = 0
    for st in sc['steps']:
        if st['next'] == old:
            st['next'] = new
            n += 1
        for nd in st['nodes']:
            b = bytearray(nd['body'])
            if nd['kind'] == 2 and struct.unpack_from('<h', b, 0)[0] == old:
                struct.pack_into('<h', b, 0, new)
            elif nd['kind'] == 9 and struct.unpack_from('<I', b, 4)[0] == old:
                struct.pack_into('<I', b, 4, new)
            else:
                continue
            nd['body'] = bytes(b)
            n += 1
    return n


def add_block(sc, cont, speaker, offer_line, cfg, pass_label=None):
    """Append one offer block continuing to step `cont`; returns the offer step index."""
    quirks = cfg['quirks']
    labels = cfg['confirm']
    base = len(sc['steps'])
    offer_ix, warn_ix = base, base + 1
    confirm_ix = {q['bit']: base + 2 + i for i, q in enumerate(quirks)}
    grant_ix = {q['bit']: base + 2 + len(quirks) + i for i, q in enumerate(quirks)}
    links = retarget(sc, cont, offer_ix)
    sc['steps'].append(step([line(speaker, offer_line)]
                            + [option(confirm_ix[q['bit']], REPLY_OFFER + q['bit'], q['name']) for q in quirks]
                            + [option(cont, REPLY_FALLBACK, FALLBACK)], END))
    sc['steps'].append(step([line(speaker, WARNING)], END))               # hidden: shown by the module
    for q in quirks:
        sc['steps'].append(step([line(speaker, f"{q['name']}: {q['detail']}"),
                                 option(grant_ix[q['bit']], REPLY_LOCK + q['bit'], labels['lock']),
                                 option(offer_ix, REPLY_BACK, labels['back']),
                                 option(cont, REPLY_PASS, pass_label or labels['pass'])], END))
    for q in quirks:
        sc['steps'].append(step([action(0xc, q['bit'], 1)], cont))
    return offer_ix, links


def build(src, cfg):
    d = dsd.Dsd(src)
    replace = {}
    for sid, (closers, speaker, offer_line) in HOSTS.items():
        sc = d.scene(sid)
        for c in closers:
            ix, links = add_block(sc, c, speaker, offer_line, cfg, PASS_LABEL.get(sid))
            print(f'scene {sid}: block before step {c} at step {ix} ({links} link(s) moved), now {len(sc["steps"])} steps')
        if len(sc['steps']) > 0xfe:
            raise SystemExit(f'scene {sid} would have {len(sc["steps"])} steps (max 254)')
        replace[sid] = sc
    add = []
    for sid, spec in NEW_SCENES.items():
        if any(i == sid for i, _ in d.index):
            raise SystemExit(f'scene id {sid} already exists in the shipped file')
        sc = new_scene(d, sid, spec)
        ix, links = add_block(sc, 2, SPEAKER, spec['offer'], cfg, PASS_LABEL.get(sid))
        print(f'new scene {sid} ({spec["moment"]}): offer at step {ix}, {len(sc["steps"])} steps')
        add.append((sid, sc))
    return d.build(replace=replace, add=add)


LINE_MAX = 255   # f_7001783860 copies a line into a 256-byte buffer: longer text is cut (seen in game)


def check_offer_text(cfg):
    """The port module appends "^n^Name: short" per offered quirk (up to 3) to the offer line: worst case must fit."""
    worst = sum(sorted((len(q['name']) + 2 + len(q['short']) + 3 for q in cfg['quirks']), reverse=True)[:3])
    lines = [h[2] for h in HOSTS.values()] + [v['offer'] for v in NEW_SCENES.values()] + [WARNING]
    for t in lines:
        assert len(t) + worst <= LINE_MAX, f'offer text too long ({len(t)} + {worst} > {LINE_MAX}): {t!r}'
    print(f'offer text fits: longest line {max(map(len, lines))} + worst 3 quirks {worst} <= {LINE_MAX}')


def is_modded(data):
    """True when a DIALOGSCENEFE.DSD already carries the quirk scenes."""
    ids = {sid for sid, _ in dsd.Dsd(data).index}
    return any(sid in ids for sid in NEW_SCENES)


def pristine(game):
    """The shipped scene file of an install: its .orig backup when one exists, else the file itself (refused when that
    file already has the quirk scenes, so a mod is never built on top of itself)."""
    f = game / 'game' / 'romfs' / REL
    orig = f.with_name(f.name + '.orig')
    data = orig.read_bytes() if orig.exists() else f.read_bytes()
    if is_modded(data):
        raise SystemExit(f'{orig if orig.exists() else f} already has the quirk scenes: no clean source to build from')
    return data


def verify(data, cfg, src):
    """Re-read the built file; check every inserted link points inside its scene and the reply ids are ours."""
    check_offer_text(cfg)
    d = dsd.Dsd(data)
    for sid in list(HOSTS) + list(NEW_SCENES):
        sc = d.scene(sid)
        n = len(sc['steps'])
        for si, st in enumerate(sc['steps']):
            assert st['next'] == END or st['next'] < n, (sid, si, st['next'])
            for nd in st['nodes']:
                if nd['kind'] == 2:
                    tgt, rid, ln = struct.unpack_from('<hHI', nd['body'])
                    assert 0 <= tgt < n, (sid, si, tgt)
                    assert nd['body'][8 + ln] == 0
                if nd['kind'] == 9:
                    assert struct.unpack_from('<I', nd['body'], 4)[0] < n
        mine = [nd for st in sc['steps'] for nd in st['nodes'] if nd['kind'] == 2
                and struct.unpack_from('<H', nd['body'], 2)[0] >= 0x5100]
        print(f'scene {sid}: {n} steps, {len(mine)} quirk-mod options, all links in range')
    # every other scene must be byte-identical to the shipped file
    orig = dsd.Dsd(src)
    idx = dict(d.index)
    same = sum(1 for sid, off in orig.index if sid not in HOSTS
               and dsd.write_scene(dsd.read_scene(orig.blob, off)) == dsd.write_scene(dsd.read_scene(d.blob, idx[sid])))
    print(f'other scenes unchanged: {same}/{len(orig.index) - len(HOSTS)}')
    assert same == len(orig.index) - len(HOSTS)


def write_new(path, data):
    tmp = path.with_name(path.name + '.quirks-new')
    tmp.write_bytes(data)
    os.replace(tmp, path)


def main():
    game = Path(sys.argv[1]).resolve()
    if game == INSTALL.resolve():
        raise SystemExit('refusing to write into the install')
    cfg = json.loads((HERE.parent / 'quirks.json').read_text(encoding='utf-8'))
    src = pristine(INSTALL)
    out = build(src, cfg)
    verify(out, cfg, src)
    dst = game / 'game' / 'romfs' / REL
    write_new(dst, out)
    print('romfs_list.tsv lines updated:', load_romfs_index().sync(game, [REL]))
    print('test copy', len(out), 'bytes, built from the shipped file (md5', hashlib.md5(src).hexdigest()[:12] + ')')


if __name__ == '__main__':
    main()
