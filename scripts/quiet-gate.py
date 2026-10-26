#!/usr/bin/env python3
"""Run a verification command quietly, keeping full output in a log file.

Why this exists:
- Global CLI output-rewriting proxies (like rtk) truncate `git diff` and test
  output across all commands, which forces agents to take extra turns to find
  missing lines (costing ~400k cached input tokens per extra turn).
- Project gate commands (test suites, linters, builds), however, routinely dump
  hundreds of passing lines into the conversation window on every iteration.
- `quiet-gate.py` wraps only explicit verification commands:
  - On exit 0: prints a single `PASS [<label>]: ...` line with the log path.
  - On non-zero exit: prints `FAIL [<label>]: ...` plus high-signal error lines
    and the tail of the output, each prefixed with `L<line>:` matching the
    1-based line number in the saved log file so an agent can `Read` the exact
    surrounding context in one turn.

Standard library Python only.

Usage:
  python3 scripts/quiet-gate.py [--name <label>] [--log <path>] [--tail 20] [--] <cmd> [args...]
  python3 scripts/quiet-gate.py --self-test
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
ERROR_SIGNAL_RE = re.compile(
    r"(?:\bFAIL(?:ED|URE)?\b|\bERROR\b|Traceback \(most recent call last\)|"
    r"\bAssertionError\b|\bpanicked at\b|\berror\[E\d+\]|\bSyntaxError\b|"
    r"\bException\b|^E\s+)",
    re.IGNORECASE,
)


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def sanitize_label(label: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", label.strip()).strip("-")
    return cleaned or "gate"


def default_log_path(label: str) -> Path:
    base = Path(tempfile.gettempdir()) / "1prompt-gates"
    base.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return base / f"{sanitize_label(label)}-{stamp}-{os.getpid()}.log"


def select_failure_lines(lines: list[str], max_lines: int) -> list[tuple[int, str]]:
    """Select up to `max_lines` 1-based (line_no, text) pairs with high signal."""
    if not lines or max_lines <= 0:
        return []
    total = len(lines)
    if total <= max_lines:
        return [(idx + 1, lines[idx]) for idx in range(total)]

    tail_budget = max(5, max_lines // 2)
    signal_budget = max(1, max_lines - tail_budget)

    tail_start = max(0, total - tail_budget)
    selected_indices: set[int] = set(range(tail_start, total))

    signal_hits: list[int] = []
    for idx in range(0, tail_start):
        if ERROR_SIGNAL_RE.search(lines[idx]):
            signal_hits.append(idx)
            # Also include the immediately following line for context if space allows
            if idx + 1 < tail_start:
                signal_hits.append(idx + 1)

    for idx in signal_hits:
        if len(selected_indices) >= max_lines:
            break
        selected_indices.add(idx)

    ordered = sorted(selected_indices)
    return [(idx + 1, lines[idx]) for idx in ordered]


def run_gate(
    cmd: list[str],
    label: str | None = None,
    log_path: Path | None = None,
    tail_lines: int = 20,
) -> tuple[int, str, Path]:
    if not cmd:
        raise ValueError("no command specified")
    gate_label = label or Path(cmd[0]).name
    target_log = log_path or default_log_path(gate_label)
    target_log.parent.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
        raw_output = proc.stdout or ""
        rc = proc.returncode
    except FileNotFoundError as exc:
        raw_output = f"ERROR: command not found: {cmd[0]} ({exc})\n"
        rc = 127

    elapsed = time.monotonic() - started
    clean_output = strip_ansi(raw_output)
    target_log.write_text(clean_output, encoding="utf-8")
    lines = clean_output.splitlines()
    line_count = len(lines)

    if rc == 0:
        summary = (
            f"PASS [{gate_label}]: exit 0 in {elapsed:.2f}s "
            f"({line_count} lines -> {target_log})"
        )
        return 0, summary, target_log

    header = (
        f"FAIL [{gate_label}]: exit {rc} in {elapsed:.2f}s "
        f"({line_count} lines, full log: {target_log})"
    )
    chosen = select_failure_lines(lines, tail_lines)
    out_lines = [header]
    prev_lineno = 0
    for lineno, text in chosen:
        if prev_lineno and lineno > prev_lineno + 1:
            out_lines.append(f"  ... ({lineno - prev_lineno - 1} lines omitted) ...")
        out_lines.append(f"  L{lineno}: {text[:240]}")
        prev_lineno = lineno
    return rc, "\n".join(out_lines), target_log


def run_self_test() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # 1. Passing command with 300 lines and ANSI color codes -> 1 line output.
        pass_log = tmp_path / "pass.log"
        pass_script = (
            "import sys\n"
            "for i in range(1, 301):\n"
            "    print(f'\\x1b[32mPASS\\x1b[0m test_case_{i}')\n"
        )
        rc, summary, written_log = run_gate(
            [sys.executable, "-c", pass_script],
            label="unit-suite",
            log_path=pass_log,
            tail_lines=20,
        )
        assert rc == 0, f"expected rc 0, got {rc}"
        assert written_log == pass_log
        assert summary.startswith("PASS [unit-suite]: exit 0"), summary
        assert "\n" not in summary, f"expected single-line PASS output, got:\n{summary}"
        saved_lines = pass_log.read_text(encoding="utf-8").splitlines()
        assert len(saved_lines) == 300, f"expected 300 log lines, got {len(saved_lines)}"
        assert "\x1b[" not in saved_lines[0], "expected ANSI codes stripped in saved log"

        # 2. Failing command with error at line 42 out of 300 lines -> surfaces L42 + tail.
        fail_log = tmp_path / "fail.log"
        fail_script = (
            "import sys\n"
            "for i in range(1, 301):\n"
            "    if i == 42:\n"
            "        print('FAIL: test_tokenizer AssertionError: expected 4 tokens, got 3')\n"
            "    else:\n"
            "        print(f'ok line {i}')\n"
            "sys.exit(1)\n"
        )
        rc2, summary2, _ = run_gate(
            [sys.executable, "-c", fail_script],
            label="unit-suite",
            log_path=fail_log,
            tail_lines=16,
        )
        assert rc2 == 1, f"expected rc 1, got {rc2}"
        assert summary2.startswith("FAIL [unit-suite]: exit 1"), summary2
        assert str(fail_log) in summary2, summary2
        assert "L42: FAIL: test_tokenizer AssertionError" in summary2, summary2
        assert "L300: ok line 300" in summary2, summary2
        assert len(summary2.splitlines()) <= 20, summary2

        # 3. Missing binary returns 127 cleanly.
        missing_log = tmp_path / "missing.log"
        rc3, summary3, _ = run_gate(
            ["nonexistent-binary-1prompt-test"],
            label="missing-bin",
            log_path=missing_log,
        )
        assert rc3 == 127, f"expected rc 127, got {rc3}"
        assert "command not found" in summary3, summary3

    print("PASS: quiet-gate self-test (3 assertions)")
    return 0


def main() -> int:
    if "--self-test" in sys.argv[1:]:
        return run_self_test()

    parser = argparse.ArgumentParser(
        description="Run a verification command quietly, saving full output to a log file."
    )
    parser.add_argument("--name", help="Short gate label for PASS/FAIL line")
    parser.add_argument("--log", type=Path, help="Explicit path for the full log file")
    parser.add_argument(
        "--tail",
        type=int,
        default=20,
        help="Maximum error/tail lines to print on failure (default: 20)",
    )
    parser.add_argument(
        "cmd",
        nargs=argparse.REMAINDER,
        help="Command and arguments to execute (optionally preceded by --)",
    )
    args = parser.parse_args()

    cmd = list(args.cmd)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        parser.error("missing command to execute (pass command or --self-test)")

    rc, output, _ = run_gate(
        cmd=cmd,
        label=args.name,
        log_path=args.log,
        tail_lines=args.tail,
    )
    print(output)
    return rc


if __name__ == "__main__":
    sys.exit(main())
