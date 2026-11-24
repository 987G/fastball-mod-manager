"""Code patch files (recomp/patches.py format), read only as far as the manager needs.

The manager reads `set <name> <env> <default>` lines (the run-time switches) and nothing else: every other line kind
(`site`, `spin`, `data`, `page`, kinds added later) is left to the merged worktree's recomp/gen.py, which is the real
validator. A file is never rejected here for a kind this module does not know.
"""
import re
from pathlib import Path


class PatchSetError(Exception):
    """A patch file that cannot be read, or two mods claiming one set name or switch;
    the message names the mod(s)."""


def _fields(raw: str) -> list[str]:
    return raw.split("#", 1)[0].split()


def read_sets(text: str) -> list[tuple[str, str, int | str]]:
    """[(name, env, default)] of the `set` lines, in file order. A default that is not
    a number is kept as text (gen.py reports it); a `set` line with the wrong number of
    fields is skipped (gen.py reports that too)."""
    sets = []
    for raw in text.splitlines():
        f = _fields(raw)
        if len(f) == 4 and f[0] == "set":
            try:
                default = int(f[3])
            except ValueError:
                default = f[3]
            sets.append((f[1], f[2], default))
    return sets


def read_text(mod) -> str:
    """The mod's patch file as text; PatchSetError naming the mod if unreadable."""
    try:
        return Path(mod.patches).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        raise PatchSetError(f"mod {mod.id!r}: cannot read its patch file {mod.patches}: {err}") from err


BUILTIN = "built-in"  # the "mod" of the release's own patch sets (tools/data/patches-<version>.txt)


class Builtin:
    """The release's own patch file in the merged worktree: `path` on disk, `label` its
    path in the recomp tree (tools/data/patches-1.0.20.txt). It goes first in the combined
    file; the manager sets no switch for its sets, so their defaults stand."""

    def __init__(self, path: Path, label: str):
        self.path, self.label = Path(path), label

    def read(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as err:
            raise PatchSetError(f"cannot read the built-in patch file {self.path}: {err}") from err


def collect(all_mods, check: bool = True, builtin: Builtin | None = None) -> list[dict]:
    """[{"mod", "name", "env", "default"}] for every set of the built-in file (mod
    "built-in", first) and every mod with a patch file, in mod order. With check, a set
    name or switch used by two mods, or by a mod and the built-in file, is a
    PatchSetError naming both."""
    out, names, envs = [], {}, {}
    sources = [(BUILTIN, builtin.read())] if builtin is not None else []
    sources += [(mod.id, mod) for mod in all_mods if mod.patches]
    for owner, src in sources:
        text = src if isinstance(src, str) else read_text(src)
        for name, env, default in read_sets(text):
            if check:
                for seen, key, what in ((names, name, "set name"), (envs, env, "switch")):
                    other = seen.get(key)
                    if other == BUILTIN and owner != BUILTIN:
                        raise PatchSetError(f"mod {owner!r} uses the code patch {what} {key!r}, which "
                                            f"the built-in patches ({builtin.label}) use already; "
                                            f"the mod must rename it")
                    if other is not None and other != owner:
                        raise PatchSetError(f"mods {other!r} and {owner!r} both use the code patch "
                                            f"{what} {key!r}; one of them must rename it")
                    seen.setdefault(key, owner)
            out.append({"mod": owner, "name": name, "env": env, "default": default})
    return out


def combine(all_mods, builtin: Builtin | None = None) -> tuple[str, list[tuple[str, Path, int, int]]]:
    """(text, spans): the built-in file first (after a `# --- built-in (<label>)` line),
    then every mod's patch file in mod order, each after a `# --- <mod id>: <path>` line;
    spans are (mod id or "built-in", path, first line, last line) of each part in the
    combined text (1-based, header included)."""
    parts, spans, line = [], [], 1
    sources = [(BUILTIN, builtin.path, f"# --- {BUILTIN} ({builtin.label})\n", builtin.read())
               ] if builtin is not None else []
    for mod in all_mods:
        if mod.patches:
            sources.append((mod.id, Path(mod.patches), f"# --- {mod.id}: {mod.patches}\n",
                            read_text(mod)))
    for owner, path, header, text in sources:
        if text and not text.endswith("\n"):
            text += "\n"
        part = header + text
        count = part.count("\n")
        parts.append(part)
        spans.append((owner, path, line, line + count - 1))
        line += count
    return "".join(parts), spans


_MOD_OFFSET = re.compile(r"\b([A-Za-z_]\w*)\+0x([0-9a-fA-F]+)")
_ABSOLUTE = re.compile(r"(?<![+\w])0x([0-9a-fA-F]+)")
_FILE_LINE = re.compile(r"patches\.txt:(\d+):")


def _has_address(text: str, module: str, offset: int) -> bool:
    """Whether a line of the patch file names `module` followed by `offset` (hex)."""
    for raw in text.splitlines():
        f = _fields(raw)
        if module not in f:
            continue
        for word in f[f.index(module) + 1:]:
            try:
                if int(word, 16) == offset:
                    return True
            except ValueError:
                continue
    return False


def locate(error: str, all_mods, spans, layout) -> str | None:
    """Best effort: which mod's patch file a generator error is about, from a line of the
    combined file, a `module+0xoffset` or an absolute address (mapped through layout.json
    entries {"name", "base", "size"}). None when nothing matches."""
    found = []

    def add(mod_id, path, where):
        text = (f"{where} is in the {BUILTIN} patch file {path}" if mod_id == BUILTIN else
                f"{where} is in mod {mod_id!r}'s patch file {path}")
        if text not in found:
            found.append(text)

    for m in _FILE_LINE.finditer(error):
        no = int(m.group(1))
        for mod_id, path, first, last in spans:
            if first <= no <= last:
                add(mod_id, path, f"line {no - first} of its part")
    wanted = [(m.group(1), int(m.group(2), 16)) for m in _MOD_OFFSET.finditer(error)]
    for m in _ABSOLUTE.finditer(error):
        address = int(m.group(1), 16)
        for entry in layout or []:
            try:
                base, size = int(entry["base"], 16), int(entry["size"], 16)
            except (KeyError, TypeError, ValueError):
                continue
            if base <= address < base + size:
                wanted.append((entry["name"], address - base))
    files = [(owner, path) for owner, path, _, _ in spans if owner == BUILTIN]
    files += [(mod.id, mod.patches) for mod in all_mods if mod.patches]
    for module, offset in wanted:
        for owner, path in files:
            try:
                text = Path(path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if _has_address(text, module, offset):
                add(owner, path, f"{module}+{offset:#x}")
    return "; ".join(found) or None
