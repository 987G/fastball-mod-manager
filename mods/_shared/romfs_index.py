"""Keeps the install's file index (game/romfs_list.tsv: size, offset, /PATH per line) in step with files a mod changes.

The port only reads the index in its RomFS self-test, but installer and launcher tools check files against it (Mod the
Show Manager rewrites these sizes after every apply for the same reason). The original index is kept once as
romfs_list.tsv.orig.
"""
import shutil
from pathlib import Path


def index_path(game):
    return game / 'game' / 'romfs_list.tsv'


def sync(game, rels):
    """Set the listed size of each romfs-relative path in rels (e.g. 'FRONTEND/RTTS/INVENTORYDATA.XML') to the
    file's current size. Returns the number of lines changed."""
    tsv = index_path(game)
    if not tsv.exists():
        return 0
    orig = tsv.with_name(tsv.name + '.orig')
    if not orig.exists():
        shutil.copy2(tsv, orig)
    romfs = game / 'game' / 'romfs'
    want = {'/' + r.replace('\\', '/').upper(): (romfs / r).stat().st_size for r in rels if (romfs / r).exists()}
    lines = tsv.read_text(encoding='utf-8', newline='').split('\n')
    changed = 0
    for i, line in enumerate(lines):
        p = line.split('\t')
        if len(p) == 3 and p[2].rstrip('\r').upper() in want:
            size = str(want[p[2].rstrip('\r').upper()])
            if p[0] != size:
                p[0] = size
                lines[i] = '\t'.join(p)
                changed += 1
    if changed:
        tmp = tsv.with_name(tsv.name + '.tmp')
        tmp.write_text('\n'.join(lines), encoding='utf-8', newline='')
        tmp.replace(tsv)
    return changed


MOD_FILES = ['FRONTEND/RTTS/INVENTORYDATA.XML', 'FRONTEND/RTTS/INVQUANTITYDATA.XML',
             'FRONTEND/EQUIPMENT/MLBEQUIPMENT.JSON', 'NSW/LIVECONTENT/LIVE.TEX']


if __name__ == '__main__':
    import sys
    game = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(r'C:\Games\MLB The Show 26')
    print(f'{sync(game, MOD_FILES)} index lines updated')
