#!/usr/bin/env python3
"""Checks this repo: all self-tests, the autonomous settings file, and the scrub gate.

Run: python3 scripts/check.py   (exit 0 = PASS, 1 = FAIL). Standard library only.

  1. hooks/agent-guard.py --self-test passes
  2. scripts/github-boundary.py --self-test passes
  3. scripts/handoff-tail.py --self-test passes
  4. scripts/quiet-gate.py --self-test passes
  5. hooks/autonomous-settings.json runs agent-guard.py boundary / boundary-after on matcher "*" (PreToolUse /
     PostToolUse) and boundary-stop on Stop and SubagentStop, from the install path $HOME/.claude/hooks/1prompt/
  6. scrub gate: if ~/.config/1prompt/scrub-terms.txt exists, no file name and no line of any file git lists
     (tracked, or untracked and not ignored; read from the work tree, so stage what you scanned), and no commit
     message, author or committer on any branch, matches it. One entry per line: "#" starts a comment; "allow:X"
     removes the literal string X (case-sensitive, longest first) before matching; any other line is a
     case-insensitive regular expression. A hit is reported as file:line and the term list's line number, never
     the term itself. An unreadable list or a bad regex fails. Without the file the gate is skipped, which is
     not a failure.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUARD_COMMAND = 'python3 "$HOME/.claude/hooks/1prompt/agent-guard.py" {mode}'
TERMS = Path.home() / ".config" / "1prompt" / "scrub-terms.txt"
FAILURES = []


def fail(message):
    FAILURES.append(message)
    print(f"FAIL: {message}")


def read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def self_test(script):
    run = subprocess.run([sys.executable, str(ROOT / script), "--self-test"], capture_output=True, text=True)
    if run.returncode != 0:
        fail(f"{script} --self-test failed:\n" + (run.stdout + run.stderr).strip())
    else:
        print(f"ok: {script} --self-test: {run.stdout.strip().splitlines()[-1] if run.stdout.strip() else 'passed'}")


def check_settings():
    """The settings file an autonomous run loads: the boundary hook before and after every tool, and at each stop."""
    try:
        auto = json.loads(read("hooks/autonomous-settings.json"))
        want = {"PreToolUse": ("*", "boundary"), "PostToolUse": ("*", "boundary-after"),
                "Stop": (None, "boundary-stop"), "SubagentStop": (None, "boundary-stop")}
        if set(auto) != {"hooks"} or set(auto["hooks"]) != set(want):
            raise ValueError(f"events must be exactly {sorted(want)}")
        for event, (matcher, mode) in want.items():
            entries = auto["hooks"][event]
            if len(entries) != 1 or entries[0].get("matcher") != matcher or \
                    [h.get("command") for h in entries[0]["hooks"]] != [GUARD_COMMAND.format(mode=mode)]:
                raise ValueError(f"{event} must run agent-guard.py {mode}" + (f" on matcher \"{matcher}\"" if matcher else ""))
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError) as error:
        fail(f"hooks/autonomous-settings.json: not the expected settings ({type(error).__name__}: {error})")
    else:
        print("ok: hooks/autonomous-settings.json runs the four boundary hooks")


def git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)


def load_terms():
    allow, patterns = [], []
    try:
        lines = TERMS.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        fail(f"scrub gate: the term list cannot be read as UTF-8 text ({type(error).__name__})")
        return None
    for number, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("allow:"):
            if line[6:]:
                allow.append(line[6:])
            continue
        try:
            patterns.append((number, re.compile(line, re.IGNORECASE)))
        except re.error as error:
            fail(f"scrub gate: line {number} of the term list is not a valid regex ({error})")
    allow.sort(key=len, reverse=True)  # the longest first, so a short literal never cuts a longer one apart
    return allow, patterns


def hits(text, allow, patterns):
    """Line numbers in the term list of the patterns that match `text` once the allowed literals are removed.
    Only the line number is printed, never the pattern, so a private term never reaches a log."""
    for literal in allow:
        text = text.replace(literal, "")
    return [pattern for pattern, regex in patterns if regex.search(text)]


def scrub_gate():
    if not TERMS.is_file():
        print("scrub gate: skipped (no term list)")
        return
    terms = load_terms()
    if terms is None:
        return
    allow, patterns = terms
    listed = git("ls-files", "--cached", "--others", "--exclude-standard", "-z")
    if listed.returncode != 0:
        fail("scrub gate: git ls-files failed: " + listed.stderr.strip())
        return
    files = sorted(set(name for name in listed.stdout.split("\0") if name))
    found = 0
    for name in files:
        for pattern in hits(name, allow, patterns):
            fail(f"scrub gate: the file name {name} matches term list line {pattern}")
            found += 1
        path = ROOT / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            fail(f"scrub gate: {name} is not readable as UTF-8 text; it cannot be scanned")
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for pattern in hits(line, allow, patterns):
                fail(f"scrub gate: {name}:{number} matches term list line {pattern}")
                found += 1
    messages = 0
    if git("rev-parse", "--verify", "-q", "HEAD").returncode == 0:
        log = git("log", "--all", "--format=%H%x00%an <%ae> %cn <%ce>%n%B%x01")
        for entry in log.stdout.split("\x01"):
            if "\0" not in entry:
                continue
            sha, body = entry.strip().split("\0", 1)
            messages += 1
            for pattern in hits(body, allow, patterns):
                fail(f"scrub gate: commit {sha[:12]} message or identity matches term list line {pattern}")
                found += 1
    print(f"scrub gate: ran, {len(patterns)} patterns, {len(files)} files, {messages} commit messages, {found} hits")


def main():
    self_test("hooks/agent-guard.py")
    self_test("scripts/github-boundary.py")
    self_test("scripts/handoff-tail.py")
    self_test("scripts/quiet-gate.py")
    check_settings()
    scrub_gate()
    print(f"{'FAIL' if FAILURES else 'PASS'}: {len(FAILURES)} failures")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
