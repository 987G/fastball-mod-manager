"""A mod's feature list: a small Markdown file -> sections for the Home detail card.

Only this much Markdown: a line that starts with a bold phrase (**When you get a pick**) or with # is a section title,
and any words after the bold phrase are a note under it; "- " or "* " starts a bullet, indented two or more spaces it
is a sub-bullet of the one before; any other line is part of a paragraph. Inline **bold** stays in the text for the
page to draw; a link [text](url) keeps only its text. Nothing in the file is ever used as HTML.
"""
import re

MAX_BYTES = 32 * 1024

_HASH_TITLE = re.compile(r"^#{1,6}\s+(.+?)\s*#*$")
_BOLD_TITLE = re.compile(r"^\*\*([^*]+?)\*\*\s*[:\-]?\s*(.*)$")
_BULLET = re.compile(r"^(\s*)[-*]\s+(.+)$")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")


def _clean(text: str) -> str:
    return _LINK.sub(r"\1", text.strip())


def parse(text: str) -> list[dict]:
    """[{"title": str | None, "blocks": [{"bullet": str, "sub": [str, ...]} | {"para": str}]}], empty sections
    dropped. Blank lines end a paragraph."""
    sections = [{"title": None, "blocks": []}]
    para: list[str] = []

    def end_para():
        if para:
            sections[-1]["blocks"].append({"para": " ".join(para)})
            para.clear()

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            end_para()
            continue
        bullet = _BULLET.match(line)
        if bullet:
            end_para()
            indent, body = len(bullet.group(1).expandtabs(4)), _clean(bullet.group(2))
            blocks = sections[-1]["blocks"]
            if indent >= 2 and blocks and "bullet" in blocks[-1]:
                blocks[-1]["sub"].append(body)
            else:
                blocks.append({"bullet": body, "sub": []})
            continue
        stripped = line.strip()
        title = _HASH_TITLE.match(stripped) or _BOLD_TITLE.match(stripped)
        if title:
            end_para()
            sections.append({"title": _clean(title.group(1)), "blocks": []})
            note = title.group(2) if title.re is _BOLD_TITLE else ""
            if note:
                para.append(_clean(note))
            continue
        para.append(_clean(stripped))
    end_para()
    return [s for s in sections if s["title"] or s["blocks"]]


def _texts(value) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def valid(sections) -> bool:
    """Whether `sections` has the shape parse() returns (a release's index carries it parsed)."""
    if not isinstance(sections, list):
        return False
    for section in sections:
        if not (isinstance(section, dict) and set(section) == {"title", "blocks"}
                and (section["title"] is None or isinstance(section["title"], str))
                and isinstance(section["blocks"], list)):
            return False
        for block in section["blocks"]:
            if not isinstance(block, dict):
                return False
            if set(block) == {"para"} and isinstance(block["para"], str):
                continue
            if set(block) == {"bullet", "sub"} and isinstance(block["bullet"], str)                     and _texts(block["sub"]):
                continue
            return False
    return True
