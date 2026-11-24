import unittest

from app import features

QUIRKS_STYLE = """\
**When you get a pick**
- **After the draft:** your new manager offers a pick (3 choices; 2 if you went after round 1)
- **Every spring:** your agent offers a pick before camp, sized by last season
  - Hitters: .800+ OPS = 3 choices
  - Pitchers: 3.00 ERA or better = 3

**Earn your spot** (an earned quirk is always in the offer)
- Table Setter: 30+ SBs
"""


class ParseTests(unittest.TestCase):
    def test_sections_bullets_sub_bullets_and_title_notes(self):
        self.assertEqual(features.parse(QUIRKS_STYLE), [
            {"title": "When you get a pick", "blocks": [
                {"bullet": "**After the draft:** your new manager offers a pick (3 choices; 2 if you went after "
                           "round 1)", "sub": []},
                {"bullet": "**Every spring:** your agent offers a pick before camp, sized by last season",
                 "sub": ["Hitters: .800+ OPS = 3 choices", "Pitchers: 3.00 ERA or better = 3"]}]},
            {"title": "Earn your spot", "blocks": [
                {"para": "(an earned quirk is always in the offer)"},
                {"bullet": "Table Setter: 30+ SBs", "sub": []}]},
        ])

    def test_hash_titles_paragraphs_and_links(self):
        text = "Intro line one\nline two\n\n## Install #\n* See [the guide](https://example.com/x) first\n"
        self.assertEqual(features.parse(text), [
            {"title": None, "blocks": [{"para": "Intro line one line two"}]},
            {"title": "Install", "blocks": [{"bullet": "See the guide first", "sub": []}]},
        ])

    def test_sub_bullet_without_a_bullet_is_a_bullet(self):
        self.assertEqual(features.parse("**T**\n  - lone\n"),
                         [{"title": "T", "blocks": [{"bullet": "lone", "sub": []}]}])

    def test_html_stays_text(self):
        [section] = features.parse("- <img src=x onerror=alert(1)>\n")
        self.assertEqual(section["blocks"][0]["bullet"], "<img src=x onerror=alert(1)>")

    def test_empty_file_is_empty(self):
        self.assertEqual(features.parse("\n\n"), [])

    def test_valid_accepts_what_parse_gives_and_nothing_else(self):
        self.assertTrue(features.valid(features.parse(QUIRKS_STYLE)))
        self.assertTrue(features.valid([]))
        for bad in (None, {}, [1], [{"title": 5, "blocks": []}], [{"title": None}],
                    [{"title": None, "blocks": [{"html": "<b>x</b>"}]}],
                    [{"title": None, "blocks": [{"bullet": "x", "sub": [5]}]}],
                    [{"title": None, "blocks": [{"para": "x", "extra": 1}]}]):
            self.assertFalse(features.valid(bad), bad)


if __name__ == "__main__":
    unittest.main()
