"""Perks+ changes to FRONTEND/RTTS/PERKS.XML, made as text edits so the file keeps its layout and CRLF line ends.

Trigger rows of a perk, in order (each holds one Level id per perk level): count, runners, score, outs, game type,
situation, comparison, value. Indexes are the <Triggers> lists at the end of the file, e.g. score 2 = the batting
team trails, 4 = the pitcher is tired; runners 7 = anyone on base; outs 3 = one or two outs; comparison 2 = run
differential at most a value (batting team minus fielding team, signed: f_70000bbe50).
"""
import re

COUNT, RUNNERS, SCORE, OUTS, GAMETYPE, SITUATION, COMPARISON, VALUE = range(8)

EV = 'Gain an exit velocity bonus '
CHANGES = {
    # user 2026-11-17: "Make Fore and Top floor trigger when down at level 2 and when tied or down at 3".
    # Level 2 already fires when the batting team trails; level 3 fired only when tied and becomes run differential at
    # most 0. Vanilla's text named one score state on every level; each level now says its own trigger, in our words.
    'Fore!': {
        'triggers': {3: {SCORE: 0, COMPARISON: 2, VALUE: 0}},
        'desc': [EV + 'when your team is winning with a runner in scoring position and 0 or 1 out.',
                 EV + 'when your team is behind with runner(s) on base.',
                 EV + 'when your team is tied or behind with runner(s) on base.',
                 EV + 'with runner(s) on base.']},
    'Top Floor': {
        'triggers': {3: {SCORE: 0, COMPARISON: 2, VALUE: 0}},
        'desc': [EV + 'when your team is winning with a runner on 2nd and no outs.',
                 EV + 'when your team is behind with runner(s) on base.',
                 EV + 'when your team is tied or behind with runner(s) on base.',
                 EV + 'with runner(s) on base.']},
    # Disabled in vanilla with no unlock goal and placeholder triggers (both a copy of the same 3 rows); user
    # 2026-11-17: "add brick wall and hose piece". Triggers now match their own descriptions.
    'Brick Wall': {
        'enable': True,
        'unlock': ('4', '15', '500.000000', 'Tally 500 Career Putouts'),
        'triggers': {lv: {COUNT: 0, RUNNERS: 7, SCORE: 4, OUTS: 0} for lv in (1, 2, 3)}},
    'Hose Piece': {
        'enable': True,
        'unlock': ('5', '15', '80.000000', 'Reach 80 Arm Strength'),
        'triggers': {lv: {COUNT: 0, RUNNERS: 0, SCORE: 0, OUTS: 3} for lv in (1, 2, 3)}},
}


def _block(text, name):
    i = text.index(f'<Perk name="{name}"')
    return i, text.index('</Perk>', i)


def _edit(block, spec):
    if spec.get('enable'):
        block = block.replace('disabled="1"', 'disabled="0"', 1)
    if 'unlock' in spec:
        t, o, v, s = spec['unlock']
        block, n = re.subn(r'<UnlockCriteria [^>]*/>',
                           f'<UnlockCriteria type="{t}" option="{o}" value="{v}" text="{s}"/>', block, count=1)
        assert n == 1
    if 'desc' in spec:
        descs = list(re.finditer(r'<Desc text="[^"]*"/>', block))
        assert len(descs) == len(spec['desc'])
        for m, d in reversed(list(zip(descs, spec['desc']))):
            block = block[:m.start()] + f'<Desc text="{d}"/>' + block[m.end():]
    rows = list(re.finditer(r'<TriggerParameter>(.*?)</TriggerParameter>', block, re.S))
    assert len(rows) == 8, len(rows)
    for row in reversed(range(8)):
        m = rows[row]
        levels = list(re.finditer(r'<Level id="(\d+)"/>', m.group(1)))
        body = m.group(1)
        for lv, sets in sorted(spec.get('triggers', {}).items(), reverse=True):
            if row in sets:
                lm = levels[lv - 1]
                body = body[:lm.start()] + f'<Level id="{sets[row]}"/>' + body[lm.end():]
        block = block[:m.start(1)] + body + block[m.end(1):]
    return block


def is_modded(text):
    i, j = _block(text, 'Brick Wall')
    return 'Tally 500 Career Putouts' in text[i:j]


def apply(text):
    """The game's PERKS.XML text with Perks+'s changes. Refuses a file that already has them."""
    if is_modded(text):
        raise ValueError('PERKS.XML already has the Perks+ changes')
    for name, spec in CHANGES.items():
        i, j = _block(text, name)
        text = text[:i] + _edit(text[i:j], spec) + text[j:]
    return text
