#!/usr/bin/env python3
"""Extracts a zero-token cross-agent handoff tail from local transcripts and git state.

When a coding-agent session ends abruptly (a 5-hour or weekly quota HTTP 429, a timeout, a
crash, or a manual agent switch between Claude Code, Codex, Antigravity and OpenCode), the
incoming agent starts with an empty prompt cache and a stale `HANDOFF.md`. It cannot call
`--resume` on a quota-locked account to ask the previous model to summarise its work.

`handoff-tail.py` reads the local JSONL transcript written on disk by the last session plus
the live git working tree, and formats or updates the `## Live tail (auto-captured)` section
of `HANDOFF.md` using zero LLM tokens:

  python3 scripts/handoff-tail.py [--repo <dir>] [--transcript <file>] [--max-commands 5]
  python3 scripts/handoff-tail.py --repo <dir> --write [<handoff-file>]
  python3 scripts/handoff-tail.py --self-test

Supported transcript formats (auto-detected by content or discovered under standard local
session folders for `<repo>`):
  - Claude Code (`~/.claude/projects/<project-slug>/*.jsonl`)
  - Codex (`$CODEX_HOME/sessions/**/*.jsonl` or `~/.codex/sessions/**/*.jsonl`)
  - Antigravity (`~/.gemini/antigravity-cli/brain/*/.system_generated/logs/transcript.jsonl`)
  - OpenCode / generic JSONL (`tool_use` / `tool_result` or `function_call` events)

Standard library Python only.
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SECTION_HEADER = "## Live tail (auto-captured)"
QUOTA_RE = re.compile(
    r"(usage_limit_reached|rate_limit_error|quota|credit balance is too low|HTTP 429|\b429\b.*limit)",
    re.IGNORECASE,
)
ERROR_LINE_RE = re.compile(
    r"(^FAIL\b|^ERROR\b|Traceback \(most recent|AssertionError|panicked at|error\[E\d+\]|exit code [1-9])",
    re.IGNORECASE,
)


def git_info(repo: Path):
    """Returns (branch, head_line, status_lines, diff_stat_lines) for `repo`."""
    def run_git(*args):
        res = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
        )
        return res.stdout.strip() if res.returncode == 0 else ""

    branch = run_git("rev-parse", "--abbrev-ref", "HEAD") or "detached"
    head = run_git("log", "-1", "--format=%h %s")
    status_raw = run_git("status", "--short")
    status = [line for line in status_raw.splitlines() if line.strip()][:25]
    diff_raw = run_git("diff", "--stat", "HEAD")
    if not diff_raw:
        diff_raw = run_git("diff", "--stat")
    diff_stat = [line.strip() for line in diff_raw.splitlines() if line.strip()][:15]
    return branch, head, status, diff_stat


def claude_project_slug(repo: Path) -> str:
    """Claude Code turns `/a/b/c_d` into `-a-b-c-d` under `~/.claude/projects/`."""
    resolved = str(repo.resolve())
    return re.sub(r"[^A-Za-z0-9]", "-", resolved)


def discover_transcript(repo: Path, home: Path = None):
    """Finds the newest transcript file matching `repo` across local agent stores."""
    home = home or Path.home()
    repo_resolved = repo.resolve()
    repo_str = str(repo_resolved)
    candidates = []

    # 1. Claude Code: ~/.claude/projects/<slug>/*.jsonl
    claude_dir = home / ".claude" / "projects" / claude_project_slug(repo_resolved)
    if claude_dir.is_dir():
        for p in claude_dir.glob("*.jsonl"):
            if p.is_file():
                candidates.append((p.stat().st_mtime, p))

    # 2. Codex: $CODEX_HOME/sessions/**/*.jsonl or ~/.codex/sessions/**/*.jsonl
    codex_roots = []
    if os.environ.get("CODEX_HOME"):
        codex_roots.append(Path(os.environ["CODEX_HOME"]) / "sessions")
    codex_roots.append(home / ".codex" / "sessions")
    for root in codex_roots:
        if root.is_dir():
            for p in root.rglob("*.jsonl"):
                if not p.is_file():
                    continue
                try:
                    head_chunk = p.read_text(encoding="utf-8", errors="replace")[:8192]
                except OSError:
                    continue
                if repo_str in head_chunk:
                    candidates.append((p.stat().st_mtime, p))

    # 3. Antigravity: ~/.gemini/antigravity-cli/brain/*/.system_generated/logs/transcript.jsonl
    agy_root = home / ".gemini" / "antigravity-cli" / "brain"
    if agy_root.is_dir():
        for p in agy_root.glob("*/.system_generated/logs/transcript.jsonl"):
            if not p.is_file():
                continue
            try:
                head_chunk = p.read_text(encoding="utf-8", errors="replace")[:16384]
            except OSError:
                continue
            if repo_str in head_chunk:
                candidates.append((p.stat().st_mtime, p))

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def compact_error_snippet(text: str, max_lines: int = 4) -> str:
    """Extracts up to `max_lines` high-signal error lines from tool output."""
    if not text:
        return ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""
    matched = [ln for ln in lines if ERROR_LINE_RE.search(ln)]
    chosen = matched[: max_lines - 1] + ([lines[-1]] if lines[-1] not in matched[: max_lines - 1] else [])
    if not chosen:
        chosen = lines[-max_lines:]
    return " | ".join(chosen[:max_lines])[:280]


def unwrap_str(val) -> str:
    if val is None:
        return ""
    s = str(val).strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        try:
            unwrapped = json.loads(s)
            if isinstance(unwrapped, str):
                return unwrapped
        except json.JSONDecodeError:
            return s[1:-1]
    return s


def normalise_path(path_str: str, repo: Path = None) -> str:
    cleaned = unwrap_str(path_str)
    if not cleaned:
        return ""
    if repo is not None:
        p = Path(cleaned)
        if p.is_absolute():
            try:
                rel = p.resolve().relative_to(repo.resolve())
                return str(rel)
            except (ValueError, OSError):
                return ""
    return cleaned


def parse_transcript(path: Path, repo: Path = None, max_commands: int = 5):
    """Parses a JSONL transcript from Claude Code, Codex, or Antigravity."""
    harness = "unknown"
    session_id = path.stem
    if session_id in ("transcript", "transcript_full") and path.parent.name == "logs":
        try:
            session_id = path.parent.parent.parent.name
        except IndexError:
            pass
    last_timestamp = None
    stop_reason = "normal"
    files_edited = []
    files_read = []
    commands = []  # list of dicts: {cmd, status, snippet}
    pending_calls = {}  # tool_id -> {kind, cmd, path}
    last_note = ""

    def record_edit(f):
        norm = normalise_path(f, repo)
        if norm and norm not in files_edited:
            files_edited.append(norm)

    def record_read(f):
        norm = normalise_path(f, repo)
        if norm and norm not in files_read:
            files_read.append(norm)

    raw_lines = [ln.strip() for ln in path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
    total_lines = len(raw_lines)
    for idx, raw in enumerate(raw_lines):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue

        ts = obj.get("timestamp") or obj.get("created_at")
        if isinstance(ts, str) and ts:
            last_timestamp = ts
        if obj.get("sessionId"):
            session_id = str(obj["sessionId"])

        # Detect quota / rate-limit errors in the tail of the session (last 3 events)
        if idx >= max(0, total_lines - 3):
            is_error_event = bool(
                obj.get("isApiErrorMessage")
                or obj.get("error")
                or obj.get("status") == "ERROR"
                or obj.get("type") in ("error", "turn.failed")
            )
            if is_error_event and QUOTA_RE.search(raw):
                stop_reason = "quota/rate-limit (HTTP 429)"

        # Format A: Antigravity transcript.jsonl (`step_index`, `type`, `tool_calls`)
        if "step_index" in obj and "type" in obj:
            harness = "antigravity"
            step_type = obj.get("type")
            if step_type == "PLANNER_RESPONSE":
                content = obj.get("content")
                if isinstance(content, str) and content.strip():
                    last_note = content.strip()
                for tc in obj.get("tool_calls") or []:
                    if not isinstance(tc, dict):
                        continue
                    tname = tc.get("name") or tc.get("tool_name") or ""
                    args = tc.get("arguments") or tc.get("args") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}
                    if tname == "run_command":
                        cmd = unwrap_str(args.get("CommandLine") or args.get("command") or "")
                        if cmd:
                            commands.append({"cmd": cmd, "status": "ran", "snippet": ""})
                    elif tname in ("edit_file", "write_file"):
                        record_edit(args.get("Path") or args.get("path") or "")
                    elif tname == "read_file":
                        record_read(args.get("Path") or args.get("path") or "")
            continue

        # Format B: Codex session JSONL (`type: "response_item"` / `function_call` / `function_call_output`)
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else obj
        ptype = payload.get("type") or obj.get("type")
        if ptype in ("function_call", "custom_tool_call"):
            harness = "codex"
            call_id = payload.get("call_id") or payload.get("id") or ""
            name = payload.get("name") or ""
            raw_args = payload.get("arguments") or payload.get("input") or "{}"
            args = {}
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args)
                except json.JSONDecodeError:
                    args = {"raw": raw_args}
            elif isinstance(raw_args, dict):
                args = raw_args
            if name in ("shell", "exec_command", "container.exec", "bash"):
                cmd = args.get("cmd") or args.get("command") or ""
                if isinstance(cmd, list):
                    cmd = " ".join(str(x) for x in cmd)
                entry = {"cmd": str(cmd), "status": "ran", "snippet": ""}
                commands.append(entry)
                if call_id:
                    pending_calls[call_id] = entry
            elif name == "apply_patch":
                patch_text = str(args.get("raw") or args.get("patch") or raw_args)
                for m in re.finditer(r"\*\*\* (?:Update|Add|Delete) File: ([^\n]+)", patch_text):
                    record_edit(m.group(1).strip())
            continue

        if ptype in ("function_call_output", "custom_tool_call_output"):
            harness = "codex"
            call_id = payload.get("call_id") or ""
            out_text = str(payload.get("output") or "")
            entry = pending_calls.get(call_id)
            if entry is not None:
                exit_m = re.search(r"Process exited with code (\d+)", out_text)
                if exit_m:
                    code = int(exit_m.group(1))
                    entry["status"] = "ok" if code == 0 else f"exit {code}"
                    if code != 0:
                        entry["snippet"] = compact_error_snippet(out_text)
                elif ERROR_LINE_RE.search(out_text):
                    entry["status"] = "error"
                    entry["snippet"] = compact_error_snippet(out_text)
                else:
                    entry["status"] = "ok"
            continue

        # Format C: Claude Code JSONL (`type: "assistant"` / `"user"` / `"result"`)
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        role = msg.get("role") or obj.get("type")
        if role == "assistant":
            if harness == "unknown":
                harness = "claude"
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text" and block.get("text", "").strip():
                        last_note = block["text"].strip()
                    elif btype == "tool_use":
                        tid = block.get("id") or ""
                        tname = block.get("name") or ""
                        inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                        if tname in ("Bash", "PowerShell"):
                            cmd = inp.get("command") or ""
                            entry = {"cmd": cmd, "status": "ran", "snippet": ""}
                            commands.append(entry)
                            if tid:
                                pending_calls[tid] = entry
                        elif tname in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
                            record_edit(inp.get("file_path") or inp.get("path") or "")
                        elif tname in ("Read", "Glob", "Grep"):
                            record_read(inp.get("file_path") or inp.get("path") or "")
        elif role == "user":
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    tid = block.get("tool_use_id") or ""
                    is_err = bool(block.get("is_error"))
                    raw_c = block.get("content")
                    if isinstance(raw_c, list):
                        text_c = "\n".join(
                            item.get("text", "") for item in raw_c if isinstance(item, dict)
                        )
                    else:
                        text_c = str(raw_c or "")
                    entry = pending_calls.get(tid)
                    if entry is not None:
                        if is_err or ERROR_LINE_RE.search(text_c):
                            entry["status"] = "error" if is_err else "check"
                            entry["snippet"] = compact_error_snippet(text_c)
                        else:
                            entry["status"] = "ok"
        elif obj.get("type") == "result":
            harness = "claude"
            if obj.get("is_error") and stop_reason == "normal":
                stop_reason = "error (" + str(obj.get("subtype") or "result") + ")"

    if stop_reason == "normal" and commands and commands[-1]["status"] in ("error", "check"):
        stop_reason = "last command failed"

    return {
        "harness": harness,
        "session_id": session_id,
        "last_timestamp": last_timestamp,
        "stop_reason": stop_reason,
        "files_edited": files_edited,
        "files_read": files_read,
        "commands": commands[-max_commands:],
        "last_note": last_note,
    }


def render_tail_section(repo: Path, transcript_info, max_commands: int = 5) -> str:
    """Renders the `## Live tail (auto-captured)` Markdown block."""
    branch, head, status, diff_stat = git_info(repo)
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        SECTION_HEADER,
        f"- Captured: `{now}` · branch `{branch}`" + (f" · HEAD `{head}`" if head else ""),
    ]
    if transcript_info:
        t_ts = transcript_info.get("last_timestamp") or "unknown"
        lines.append(
            f"- Previous session: `{transcript_info['harness']}` (`{transcript_info['session_id']}`)"
            f" · last event `{t_ts}` · stop `{transcript_info['stop_reason']}`"
        )
        edited = transcript_info.get("files_edited") or []
        if edited:
            lines.append("- Files edited in session: " + ", ".join(f"`{p}`" for p in edited[:15]))
        cmds = transcript_info.get("commands") or []
        if cmds:
            lines.append(f"- Last {len(cmds[-max_commands:])} command(s):")
            for c in cmds[-max_commands:]:
                one_line_cmd = " ".join(c["cmd"].strip().splitlines())
                if len(one_line_cmd) > 110:
                    one_line_cmd = one_line_cmd[:107] + "..."
                line = f"  - `[{c['status']}]` `{one_line_cmd}`"
                if c.get("snippet"):
                    line += f" → `{c['snippet']}`"
                lines.append(line)
        note = (transcript_info.get("last_note") or "").strip()
        if note:
            note_lines = [ln.strip() for ln in note.splitlines() if ln.strip()][:4]
            summary = " / ".join(note_lines)
            if len(summary) > 260:
                summary = summary[:257] + "..."
            lines.append(f"- Last agent note: {summary}")
    else:
        lines.append("- Previous session: no local transcript found; captured git work-tree state only")

    if status:
        lines.append("- Working tree (`git status -s`): `" + "`, `".join(status[:12]) + "`")
    else:
        lines.append("- Working tree (`git status -s`): clean")
    if diff_stat:
        lines.append("- Diff summary (`git diff --stat`): `" + "; ".join(diff_stat[-4:]) + "`")
    return "\n".join(lines) + "\n"


def update_handoff_text(existing: str, tail_section: str) -> str:
    """Replaces or appends `## Live tail (auto-captured)` without touching other sections."""
    tail_block = tail_section.rstrip() + "\n"
    if not existing.strip():
        return "# Handoff\n\n" + tail_block
    pattern = re.compile(
        r"^## Live tail \(auto-captured\)\n(?:.*\n)*?(?=^## |\Z)",
        re.MULTILINE,
    )
    if pattern.search(existing):
        updated = pattern.sub(tail_block + "\n", existing)
        return updated.rstrip() + "\n"
    return existing.rstrip() + "\n\n" + tail_block


def write_atomic(target: Path, content: str):
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".handoff-", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_name, target)
    except Exception:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def self_test() -> int:
    with tempfile.TemporaryDirectory(prefix="1prompt-handoff-") as tmp:
        root = Path(tmp)
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
        (repo / "app.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "app.py"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "initial"], check=True)
        (repo / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

        # 1. Synthetic Claude Code transcript ending with a failed test + 429 quota stop
        home = root / "home"
        slug = claude_project_slug(repo)
        cdir = home / ".claude" / "projects" / slug
        cdir.mkdir(parents=True)
        tfile = cdir / "sess-claude-1.jsonl"
        events = [
            {
                "type": "assistant",
                "sessionId": "sess-claude-1",
                "timestamp": "2026-10-26T04:00:01Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Fixing add() in app.py and running pytest."},
                        {"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": str(repo / "app.py")}},
                        {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "pytest -q"}},
                    ],
                },
            },
            {
                "type": "user",
                "timestamp": "2026-10-26T04:00:02Z",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
                        {
                            "type": "tool_result",
                            "tool_use_id": "t2",
                            "is_error": True,
                            "content": "FAIL test_app.py::test_edge\nAssertionError: expected 5 got 4\nexit code 1",
                        },
                    ],
                },
            },
            {
                "type": "assistant",
                "isApiErrorMessage": True,
                "timestamp": "2026-10-26T04:00:03Z",
                "error": "rate_limit",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "HTTP 429: usage_limit_reached"}],
                },
            },
        ]
        tfile.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")

        found = discover_transcript(repo, home=home)
        assert found == tfile, f"expected {tfile}, got {found}"
        info = parse_transcript(found, repo=repo)
        assert info["harness"] == "claude", info
        assert info["stop_reason"] == "quota/rate-limit (HTTP 429)", info
        assert info["files_edited"] == ["app.py"], info
        assert len(info["commands"]) == 1 and info["commands"][0]["status"] == "error", info
        assert "AssertionError" in info["commands"][0]["snippet"], info

        # 2. Preserve existing sections (`## Goal`, `## Dead ends`) and update `## Live tail` idempotently
        handoff_path = repo / "HANDOFF.md"
        handoff_path.write_text(
            "# Handoff\n\n## Goal\nShip feature X.\n\n## Dead ends (do not retry)\n- Regex parser for nested shells\n",
            encoding="utf-8",
        )
        section1 = render_tail_section(repo, info)
        merged1 = update_handoff_text(handoff_path.read_text(encoding="utf-8"), section1)
        write_atomic(handoff_path, merged1)
        assert "## Dead ends (do not retry)" in handoff_path.read_text(encoding="utf-8")
        assert handoff_path.read_text(encoding="utf-8").count(SECTION_HEADER) == 1

        # Second update replaces the live tail section instead of duplicating it
        info["stop_reason"] = "normal"
        section2 = render_tail_section(repo, info)
        merged2 = update_handoff_text(handoff_path.read_text(encoding="utf-8"), section2)
        write_atomic(handoff_path, merged2)
        text2 = handoff_path.read_text(encoding="utf-8")
        assert text2.count(SECTION_HEADER) == 1, text2
        assert "stop `normal`" in text2 and "stop `quota/rate-limit" not in text2, text2
        assert "Regex parser for nested shells" in text2, text2

        # 3. Codex & Antigravity formats
        codex_file = root / "codex.jsonl"
        codex_events = [
            {
                "timestamp": "2026-10-26T05:00:00Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "c1",
                    "arguments": json.dumps({"cmd": "cargo test"}),
                },
            },
            {
                "timestamp": "2026-10-26T05:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "c1",
                    "output": "Process exited with code 0\ntest result: ok",
                },
            },
        ]
        codex_file.write_text("\n".join(json.dumps(e) for e in codex_events) + "\n", encoding="utf-8")
        c_info = parse_transcript(codex_file, repo=repo)
        assert c_info["harness"] == "codex" and c_info["commands"][0]["status"] == "ok", c_info

        agy_file = root / "agy.jsonl"
        agy_events = [
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "created_at": "2026-10-26T06:00:00Z",
                "content": "Updated app.py and verified.",
                "tool_calls": [
                    {"name": "edit_file", "arguments": {"Path": str(repo / "app.py")}},
                    {"name": "run_command", "arguments": {"CommandLine": "python3 -m unittest"}},
                ],
            }
        ]
        agy_file.write_text("\n".join(json.dumps(e) for e in agy_events) + "\n", encoding="utf-8")
        a_info = parse_transcript(agy_file, repo=repo)
        assert a_info["harness"] == "antigravity" and a_info["files_edited"] == ["app.py"], a_info

    print("handoff-tail self-test: 3 transcript formats and idempotent HANDOFF.md merge passed")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Extract a zero-token cross-agent handoff tail.")
    parser.add_argument("--self-test", action="store_true", help="Run unit self-tests and exit.")
    parser.add_argument("--repo", default=".", help="Target git repository directory (default: .)")
    parser.add_argument("--transcript", default=None, help="Explicit JSONL transcript path.")
    parser.add_argument("--max-commands", type=int, default=5, help="Max recent commands to list.")
    parser.add_argument(
        "--write",
        nargs="?",
        const="HANDOFF.md",
        default=None,
        help="Update `## Live tail (auto-captured)` in HANDOFF.md (or the given path).",
    )
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()

    repo = Path(args.repo).resolve()
    tpath = Path(args.transcript).resolve() if args.transcript else discover_transcript(repo)
    info = parse_transcript(tpath, repo=repo, max_commands=args.max_commands) if tpath and tpath.is_file() else None
    section = render_tail_section(repo, info, max_commands=args.max_commands)

    if args.write is not None:
        target = Path(args.write)
        if not target.is_absolute():
            target = repo / target
        existing = target.read_text(encoding="utf-8") if target.is_file() else ""
        write_atomic(target, update_handoff_text(existing, section))
        print(f"updated {target}")
    else:
        sys.stdout.write(section)
    return 0


if __name__ == "__main__":
    sys.exit(main())
