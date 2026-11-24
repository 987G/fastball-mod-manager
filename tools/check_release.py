r"""Check a tree before it is published: no game files or game bytes, no personal information, no AI-tool mentions.

    python -I tools/check_release.py <root> [--private FILE]

Prints one finding per line, `<path>:<line>: <reason>` (path relative to <root>, forward slashes; line 0 for a finding
about the whole file or its name), and exits 1; prints "clean" and exits 0 when there is none; exits 2 when it cannot
run. Rules:

  - game files: the suffixes .webp .dsd .xbn .bbs .bc7 .nsp .nca .xci .keys, in any case;
  - compiled Python cache files: every __pycache__ folder (once, for all it holds, even empty) and every .pyc or .pyo
    file outside one, in any case; bytecode holds the absolute path of the folder it was built in;
  - files over 2 MB, unless <root>/.release-allow lists them with their sha256, one per line as
    `<sha256>  <relative path>` (a listed file whose bytes changed is a finding again, so it must be re-approved), or
    as `text  <relative path>` for a text file regenerated each release (runtime/mods.patch): exempt from the size
    rule only, every text rule still applies, and a `text` entry naming a binary file is a finding;
  - game bytes in a patch file: a `data` line (also as a +/- line of a diff) whose original field (the fifth) is raw
    hex bytes instead of `fnv64:<16 hex digits>`, whatever the other fields hold; `site` lines carry instruction
    words and are allowed;
  - user paths: a drive letter followed by a Users folder (any slashes, any case), and a Mac home folder path;
  - e-mail addresses, except GitHub's relay form `<digits>+<handle>@users.noreply.github.com`;
  - the names of the AI tool and of its maker, in any case;
  - each word of the private list (--private FILE: one per line, `#` comments and blank lines skipped). A word is
    matched whole (not inside a longer run of letters and digits) in any case; an entry with `@` (an e-mail address)
    anywhere, in any case. Findings never repeat a private word, only its number in the list, and print a path
    holding one with `***` in its place.

The text rules read every file's path, and the contents of text files: a file starting with a UTF-16 byte-order mark
is UTF-16; the rest is UTF-8, or Latin-1 where it is not valid UTF-8 (so a text file in an old code page is still read).
Any other file holding a NUL byte is binary: besides the suffix and size rules (a file approved in .release-allow by
its sha256 is still read), it is read as `strings` reads it: runs of at least 6 printable ASCII characters, one byte
each (Latin-1) or two (UTF-16LE, at either byte alignment). A user path or a private word inside such a run, the run
longer than what was found, is reported once at line 0 as "in its bytes": a library or exe built on this PC holds its
folders in long strings (debug info), while a short word between non-printable bytes of packed data is chance. E-mail
addresses and the AI-tool words are not looked for in binary files: compiled code and data hold too many of them by
chance. `.git` folders are skipped.

scan()'s `exempt` ({relative path: sha256}) names the files that are byte-for-byte those of a pinned third-party
download (the embeddable Python in the manager zip): when a file's sha256 is the one given, only the path rules (its
name, its suffix) apply to it, since nothing in it comes from this PC. Standard library only.
"""
import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

GAME_SUFFIXES = (".webp", ".dsd", ".xbn", ".bbs", ".bc7", ".nsp", ".nca", ".xci", ".keys")
MAX_BYTES = 2 * 1024 * 1024
ALLOW_FILE = ".release-allow"
SKIPPED_DIRS = {".git"}
CACHE_DIR, CACHE_SUFFIXES = "__pycache__", (".pyc", ".pyo")
CACHE_REASON = "compiled Python cache file (holds build paths); never publish it"

# The brackets keep this file from matching its own rules.
USER_PATH = re.compile(r"(?i:[a-z]:[\\/]+user[s][\\/])|/User[s]/")
AI_WORDS = re.compile(r"cl[a]ude|anthr[o]pic", re.IGNORECASE)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
RELAY = re.compile(r"[0-9]+\+[A-Za-z0-9-]+@users\.noreply\.github\.com", re.IGNORECASE)
HEX = re.compile(r"[0-9A-Fa-f]+")
TEXT_ENTRY = "text"
ALLOW_LINE = re.compile(r"([0-9A-Fa-f]{64}|text)\s+(\S.*)")
MIN_RUN = 6  # a binary file's printable run is read when it is at least this long
RUN_LATIN1 = re.compile(rb"[\t\x20-\x7e]{%d,}" % MIN_RUN)
RUN_UTF16 = re.compile(rb"(?:[\t\x20-\x7e]\x00){%d,}" % MIN_RUN)


def _word_pattern(word: str) -> re.Pattern:
    if "@" in word:
        return re.compile(re.escape(word), re.IGNORECASE)
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(word) + r"(?![A-Za-z0-9])", re.IGNORECASE)


def _text(data: bytes) -> str | None:
    """The file's text, or None for a binary file (see the module docstring)."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _raw_patch_data(line: str) -> bool:
    """A patch file's `data` line that names the bytes it replaces by those bytes, not by their hash."""
    text = line.split("#", 1)[0].strip()
    if text[:1] in ("+", "-"):
        text = text[1:].strip()
    f = text.split()
    return len(f) >= 5 and f[0] == "data" and HEX.fullmatch(f[4]) is not None  # f[4]: the original


def _runs(data: bytes):
    """The printable runs of a binary file, as `strings` reads them: at least MIN_RUN printable ASCII characters
    (tab included) in a row, one byte each (Latin-1) or two (UTF-16LE, at either byte alignment)."""
    for match in RUN_LATIN1.finditer(data):
        yield match.group().decode("ascii")
    for match in RUN_UTF16.finditer(data):
        yield match.group().decode("utf-16-le")


def _byte_findings(data: bytes, words: list[re.Pattern]) -> list[str]:
    """The reasons a binary file's bytes are a finding, each once: a user path or a private word inside one of its
    printable runs, the run longer than what was found (a leak sits in a longer string, such as a path in debug
    info; a short word between non-printable bytes is chance)."""
    rules = [(USER_PATH, "a Windows or Mac user path")]
    rules += [(pattern, f"word {n} of the private word list") for n, pattern in enumerate(words, 1)]
    found = set()
    for run in _runs(data):
        for pattern, reason in rules:
            if reason not in found and any(len(m.group()) < len(run) for m in pattern.finditer(run)):
                found.add(reason)
        if len(found) == len(rules):
            break
    return [reason for _, reason in rules if reason in found]


def _line_findings(text: str, words: list[re.Pattern]) -> list[str]:
    """The reasons one line (or a file's path) is a finding, each once."""
    reasons = []
    if USER_PATH.search(text):
        reasons.append("a Windows or Mac user path")
    if any(not RELAY.fullmatch(m.group()) for m in EMAIL.finditer(text)):
        reasons.append("an e-mail address (only the GitHub relay address may be published)")
    if AI_WORDS.search(text):
        reasons.append("names the AI tool or its maker")
    for n, pattern in enumerate(words, 1):
        if pattern.search(text):
            reasons.append(f"word {n} of the private word list")
    return reasons


def _read_allow(root: Path) -> tuple[dict[str, str], list[str]]:
    """({relative path: sha256 or "text"}, findings for lines that are neither `<sha256>  <path>` nor
    `text  <path>`)."""
    allowed, findings = {}, []
    path = root / ALLOW_FILE
    if not path.is_file():
        return allowed, findings
    for n, line in enumerate(path.read_text(encoding="utf-8-sig", errors="replace").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = ALLOW_LINE.fullmatch(line)
        if not m:
            findings.append(f"{ALLOW_FILE}:{n}: not a `<sha256>  <relative path>` or `text  <relative path>` line")
            continue
        allowed[m.group(2).strip().replace("\\", "/")] = m.group(1).lower()  # a sha256, or TEXT_ENTRY
    return allowed, findings


def _files(root: Path):
    """(relative posix path, path) of every file under `root`, `.git` folders left out, in a stable order."""
    for folder, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIPPED_DIRS)
        for name in sorted(names):
            path = Path(folder) / name
            if path.is_file():
                yield path.relative_to(root).as_posix(), path


def _cache_dirs(root: Path):
    """The relative posix path of every __pycache__ folder under `root`, `.git` folders left out."""
    for folder, dirs, _ in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIPPED_DIRS)
        for name in dirs:
            if name.casefold() == CACHE_DIR:
                yield (Path(folder) / name).relative_to(root).as_posix()


def _shown(rel: str, words: list[re.Pattern]) -> str:
    """The path as printed: never with a private word in it."""
    for pattern in words:
        rel = pattern.sub("***", rel)
    return rel


def scan(root: Path, extra_words: list[str] = (), exempt: dict | None = None) -> list[str]:
    """Every finding in the tree at `root`, as `<path>:<line>: <reason>`; `extra_words` is the private word list,
    `exempt` the files of a pinned download, {relative path: sha256} (see the module docstring)."""
    root = Path(root)
    exempt = {rel: sha.lower() for rel, sha in (exempt or {}).items()}
    words = [_word_pattern(w) for w in extra_words if w]
    allowed, findings = _read_allow(root)
    findings += [f"{_shown(rel, words)}:0: {CACHE_REASON}" for rel in _cache_dirs(root)]
    for rel, path in _files(root):
        shown = _shown(rel, words)
        if rel.lower().endswith(CACHE_SUFFIXES) and CACHE_DIR not in rel.casefold().split("/")[:-1]:
            findings.append(f"{shown}:0: {CACHE_REASON}")  # one inside a __pycache__ folder: named by the folder
        if rel.lower().endswith(GAME_SUFFIXES):
            findings.append(f"{shown}:0: a game file type ({Path(rel).suffix})")
        findings += [f"{shown}:0: in its path: {reason}" for reason in _line_findings(rel, words)]
        data = path.read_bytes()
        if rel in exempt and hashlib.sha256(data).hexdigest() == exempt[rel]:
            continue  # byte-for-byte a file of a pinned download: only the path rules above
        text = _text(data)
        entry = allowed.get(rel)
        if entry == TEXT_ENTRY and text is None:
            findings.append(f"{shown}:0: binary, but {ALLOW_FILE} lists it as text")
        if len(data) > MAX_BYTES and not (entry == TEXT_ENTRY and text is not None):
            sha = hashlib.sha256(data).hexdigest()
            if entry is None:
                findings.append(f"{shown}:0: over 2 MB and not listed in {ALLOW_FILE}")
            elif entry != sha:
                findings.append(f"{shown}:0: over 2 MB and its sha256 ({sha}) is not the one {ALLOW_FILE} approved")
        if text is None:
            findings += [f"{shown}:0: in its bytes: {reason}" for reason in _byte_findings(data, words)]
            continue
        for n, line in enumerate(text.splitlines(), 1):
            reasons = _line_findings(line, words)
            if _raw_patch_data(line):
                reasons.append("game bytes in a patch file (a data line's original must be fnv64:<hash>)")
            findings += [f"{shown}:{n}: {reason}" for reason in reasons]
    return findings


def _read_words(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    return [w.strip() for w in lines if w.strip() and not w.strip().startswith("#")]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Check a tree before it is published.")
    parser.add_argument("root", type=Path, help="the tree to check")
    parser.add_argument("--private", type=Path, metavar="FILE", help="extra words to find, one per line")
    args = parser.parse_args(argv)
    if not args.root.is_dir():
        print(f"check_release: {args.root} is not a folder", file=sys.stderr)
        return 2
    words = []
    if args.private:
        try:
            words = _read_words(args.private)
        except (OSError, UnicodeDecodeError) as err:
            print(f"check_release: cannot read the private word list {args.private}: {err}", file=sys.stderr)
            return 2
    findings = scan(args.root, words)
    for finding in findings:
        print(finding)
    if findings:
        print(f"{len(findings)} finding(s)")
        return 1
    print("clean")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    sys.exit(main())
