#!/usr/bin/env python3
"""Hooks for the builder and the reviewers: enforce what their prompts forbid.

  python3 agent-guard.py build         PreToolUse (builder): blocks commit, push, merge, branch switches, tags,
                                       PRs and other GitHub writes
  python3 agent-guard.py review        PreToolUse (reviewers): the same, plus git commands that change the tree or
                                       the index; and it snapshots every git work tree the reviewer touches
  python3 agent-guard.py review-after  PostToolUse (reviewers): tells the reviewer at once if a snapshotted work
                                       tree changed
  python3 agent-guard.py review-stop   Stop (reviewers): if a work tree changed, makes the reviewer put a
                                       "TREE CHANGED" line first in its report, so the main session sees it
  python3 agent-guard.py boundary      PreToolUse (autonomous runs, through autonomous-settings.json): the human-only
                                       list, and a snapshot of the protected paths of every tree touched
  python3 agent-guard.py boundary-after PostToolUse (autonomous runs): reports a protected path that changed, by any
                                       writer, and locks the run to reading and bd
  python3 agent-guard.py boundary-stop Stop and SubagentStop (autonomous runs): asks for "PROTECTED CHANGED" first
  python3 agent-guard.py --self-test

Reads the hook's JSON on stdin. A blocked command exits 2 with the reason on stderr, which Claude Code passes
back to the agent; anything else exits 0 and the normal permission flow applies.
Git and gh commands are checked by reading the command line, which catches mistakes, not an agent that tries to
get around it. File changes by a reviewer are not guessed from the command: the work tree is compared before and
after, so any writer is caught (an interpreter, a build script), except files git ignores (build output).
When it cannot parse a command or its input, it blocks it. If `python3` itself is missing the shell exits 127,
which Claude Code does not treat as a block. Standard library only.
"""
import hashlib
import itertools
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from fnmatch import fnmatch

OPERATORS = {"&&", "||", ";", "|", "|&", "&", "(", ")", ";;", "!", "{", "}"}
WRAPPER_OPTS_WITH_VALUE = {"-n", "-u", "-g", "-s", "-k", "-C", "-p", "--signal", "--kill-after", "--user",
                           "--group", "--adjustment", "--unset", "--chdir", "-I", "-L", "-P", "-d", "-E"}
WRAPPERS = {"stdbuf", "setsid", "env", "sudo", "command", "nohup", "time", "exec", "nice", "timeout", "xargs", "builtin"}
SHELLS = {"bash", "sh", "zsh", "dash"}
# shell reserved words that can stand before a command: `if x; then git push; fi` runs git push
RESERVED = {"if", "then", "else", "elif", "fi", "do", "done", "while", "until", "esac", "!", "{", "}"}
GIT_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}

# git subcommands that change history, refs, the remote or the checked-out branch
GIT_HISTORY = {"commit", "push", "merge", "rebase", "tag", "reset", "switch", "cherry-pick", "revert", "am",
               "pull", "fetch", "update-ref", "filter-branch", "filter-repo", "replace", "notes", "worktree",
               "symbolic-ref", "gc", "prune", "reflog", "init", "clone", "remote", "submodule", "bisect",
               "send-pack"}
# git subcommands that change the working tree or the index (allowed for the builder, not for reviewers)
GIT_TREE = {"add", "rm", "mv", "restore", "apply", "clean", "stash", "checkout", "config", "sparse-checkout"}
GIT_READ_ARGS = {"branch": {"--show-current", "-a", "-r", "-v", "-vv", "--list", "--all", "--remotes",
                            "--contains", "--merged", "--no-merged"},
                 "stash": {"list", "show", "-p", "--stat"}, "remote": {"-v", "--verbose", "show", "get-url"},
                 "reflog": {"show"}, "worktree": {"list", "--porcelain"}, "notes": {"list", "show"},
                 "bisect": {"log", "view"}, "submodule": {"status", "--recursive"},
                 "tag": {"-l", "--list", "-n", "--sort"}}
# gh: read-only subcommands only
GH_READ = {("pr", "view"), ("pr", "diff"), ("pr", "list"), ("pr", "checks"), ("pr", "status"),
           ("issue", "view"), ("issue", "list"), ("issue", "status"), ("run", "view"), ("run", "list"),
           ("run", "watch"), ("repo", "view"), ("release", "view"), ("release", "list"), ("auth", "status"),
           ("search",), ("browse",), ("status",), ("api",)}
GH_API_WRITE_FLAGS = {"-f", "-F", "--field", "--raw-field", "--input"}



class Blocked(Exception):
    pass


def strip_heredocs(text):
    """Drops heredoc bodies, which are data (unless a shell reads them), so an apostrophe or a word in them is not read as shell."""
    lines, out, i = text.split("\n"), [], 0
    while i < len(lines):
        out.append(lines[i])
        i += 1
        if "<<" in out[-1] and re.search(r"(^|[;&|(])\s*(sudo\s+)?(bash|sh|zsh|dash|eval)(\s|$)", strip_comments(out[-1])):
            continue  # a heredoc fed to a shell is commands, so its lines are checked like any other
        for m in re.finditer(r"(?<!<)<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1", out[-1]):
            ends = [j for j in range(i, len(lines)) if lines[j].strip() == m.group(2)]
            if ends:  # without its terminator line it is not a heredoc; keep the lines and check them
                i = ends[0] + 1
    return "\n".join(out)


def strip_comments(text):
    """Drops `# ...` to the end of each line when the # starts a word outside quotes, as the shell does."""
    out = []
    for line in text.split("\n"):
        quote, cut = None, len(line)
        for i, c in enumerate(line):
            if c in "'\"" and quote in (None, c) and (i == 0 or line[i - 1] != "\\" or quote == "'"):
                quote = None if quote == c else c
            elif c == "#" and quote is None and (i == 0 or line[i - 1] in " \t;&|("):
                cut = i
                break
        out.append(line[:cut])
    return "\n".join(out)


def substitutions(text):
    """The inner commands of $(...) and `...`, which run too."""
    inner, depth, start, quote, tick = [], 0, None, None, None
    i = 0
    while i < len(text):
        c = text[i]
        if c == "\\" and quote != "'":
            i += 2
            continue
        if c in "'\"" and depth == 0 and tick is None and quote in (None, c):
            quote = None if quote == c else c
        elif quote == "'":
            pass
        elif c == "`" and depth == 0:
            if tick is None:
                tick = i + 1
            else:
                inner.append(text[tick:i])
                tick = None
        elif text.startswith("$(", i):
            if depth == 0:
                start = i + 2
            depth += 1
            i += 2
            continue
        if text[i] == ")" and depth:
            depth -= 1
            if depth == 0:
                inner.append(text[start:i])
        i += 1
    return inner


def tokens(text):
    text = re.sub(r"(['\"])[();<>|&]+\1", "Q", text)  # grep ">" or '|' is an argument, not a redirect or pipe
    lexer = shlex.shlex(text.replace("\n", " ; "), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError as error:
        raise Blocked(f"could not parse the command ({error}); split it or simplify the quoting")


def segments(text, depth=0):
    """Yields (words, redirect targets) for every simple command, nested ones included."""
    if depth > 5:
        raise Blocked("commands nested too deeply to check")
    text = strip_comments(strip_heredocs(text.replace("\\\n", "")))
    for inner in substitutions(text):
        yield from segments(inner, depth + 1)
    words, targets, toks = [], [], tokens(text)
    i = 0
    while i < len(toks):
        tok = toks[i]
        if tok in OPERATORS or (re.fullmatch(r"[();|&!{}]+", tok) and not re.fullmatch(r"&>>?", tok)):
            if words or targets:
                yield words, targets
            words, targets = [], []
        elif re.fullmatch(r"\d*(>>?|>\||>&|&>>?)", tok) or (tok.isdigit() and i + 1 < len(toks) and toks[i + 1].startswith(">")):
            if tok.isdigit():
                i += 1
                tok = toks[i]
            nxt = toks[i + 1] if i + 1 < len(toks) else ""
            if (tok.endswith("&") and (nxt.isdigit() or nxt == "-")) or nxt == "&" or nxt.startswith("&"):
                i += 2 if nxt in ("&",) else 1
                if i < len(toks) and toks[i].isdigit():
                    i += 1
                continue
            targets.append(nxt)
            i += 2
            continue
        elif tok in ("<", "<<", "<<<", "<<-", "<&", "<>"):
            i += 2
            continue
        else:
            words.append(tok)
        i += 1
    if words or targets:
        yield words, targets


def command_words(words):
    """Skips VAR=value prefixes, reserved words and wrappers such as env or sudo; returns the real command and its args."""
    i = 0
    while i < len(words):
        w = words[i]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", w) or w in RESERVED:
            i += 1
        elif program(w) in WRAPPERS:
            wrapper = program(w)
            i += 1
            while i < len(words) and words[i].startswith("-"):
                if wrapper == "env" and words[i] in ("-S", "--split-string"):
                    return ["eval"] + words[i + 1:]  # env -S 'cmd args' runs the string as a command line
                i += 2 if words[i] in WRAPPER_OPTS_WITH_VALUE else 1
            if wrapper == "timeout" and i < len(words) and re.fullmatch(r"[\d.]+[smhd]?", words[i]):
                i += 1
        else:
            break
    rest = words[i:]
    if rest:
        rest = [program(rest[0])] + rest[1:]
    return rest


def program(word):
    """`/usr/bin/git`, `git.exe` and `Git` are all `git` (Windows names are case-insensitive)."""
    name = re.split(r"[\\/]", word)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def git_read_only(sub, rest):
    """True when `git <sub> <rest>` only lists or shows: every flag is a read flag and nothing is created."""
    reads = GIT_READ_ARGS[sub]
    flags = [a for a in rest if a.startswith("-")]
    names = [a for a in rest if not a.startswith("-")]
    if any(f.split("=", 1)[0] not in reads and not f.startswith(("--format", "--sort", "--contains", "--merged",
                                                                   "--no-merged", "--points-at", "--color"))
           and not (sub == "tag" and re.fullmatch(r"-n\d*", f))
           for f in flags):
        return False
    if sub in ("branch", "tag"):  # a name without --list creates a branch or tag
        return not names or bool({"-l", "--list"} & set(flags)) or all(
            rest[rest.index(n) - 1].startswith(("--contains", "--merged", "--no-merged", "--points-at")) for n in names)
    if sub == "stash" and not names:
        return False  # bare `git stash` pushes
    return not names or names[0] in reads


def check_git(args, mode):
    i = 0
    while i < len(args) and args[i].startswith("-"):
        opt = args[i].split("=", 1)[0]
        value = args[i + 1] if opt == "-c" and "=" not in args[i] and i + 1 < len(args) else args[i][2:]
        if opt == "-c" and value.lower().startswith("alias."):
            raise Blocked("`git -c alias.…` can run any git command under another name; only the main session does that")
        i += 2 if opt in GIT_OPTS_WITH_VALUE and "=" not in args[i] else 1
    if i >= len(args):
        return
    sub, rest = args[i], args[i + 1:]
    first = rest[0] if rest else ""
    if sub in GIT_READ_ARGS and git_read_only(sub, rest):
        return
    if sub == "submodule" and mode == "build" and first == "update":
        return
    if sub == "stash" and first == "branch":
        raise Blocked("`git stash branch` creates and checks out a branch; only the main session does that")
    if sub in GIT_HISTORY or sub == "branch":
        raise Blocked(f"`git {sub}` changes history, refs, the remote or the branch; only the main session does that")
    if sub == "checkout" and any(a in ("-b", "-B", "--orphan", "--detach") or re.fullmatch(r"-[a-zA-Z]*[bB]", a) for a in rest):
        raise Blocked("`git checkout -b` creates or resets a branch; only the main session does that")
    if sub == "apply" and any(a in ("--check", "--stat", "--numstat", "--summary") for a in rest) and "--apply" not in rest:
        return
    if sub == "checkout" and mode == "build" and ("--" not in rest or rest.index("--") == len(rest) - 1):
        raise Blocked("`git checkout` without `--` can switch the branch for every agent in this tree; "
                      "use `git checkout -- <path>` or `git restore <path>`")
    if sub == "config" and (not rest or rest[0] in ("--get", "--get-all", "--list", "-l", "--show-origin", "--get-regexp")
                            or (len(rest) == 1 and not rest[0].startswith("-"))):
        return
    if sub in GIT_TREE and mode == "review":
        raise Blocked(f"`git {sub}` changes the working tree, the index or the config; a reviewer never edits")
    if sub == "config" and mode == "build":
        raise Blocked("`git config` writes git settings; only the main session does that")


def check_gh(args):
    while args and args[0] in ("-R", "--repo", "--hostname"):
        args = args[2:]
    while args and args[0].startswith(("--repo=", "--hostname=", "-R")) and args[0] not in ("-R",):
        args = args[1:]
    kept, j = [], 0
    while j < len(args):  # -R/--repo and --hostname may also come after the subcommand
        if args[j] in ("-R", "--repo", "--hostname"):
            j += 2
            continue
        if not args[j].startswith(("--repo=", "--hostname=")):
            kept.append(args[j])
        j += 1
    args = kept
    words = [a for a in args if not a.startswith("-")]
    key = tuple(words[:2])
    if key in GH_READ or tuple(words[:1]) in GH_READ:
        if words[:1] == ["api"]:
            for j, a in enumerate(args):
                flag, _, value = a.partition("=")
                if flag.startswith("-X") and flag != "-X":
                    flag, value = "-X", flag[2:]
                method = value or (args[j + 1] if j + 1 < len(args) else "")
                if flag in ("-X", "--method") and method.upper() != "GET":
                    raise Blocked("`gh api` with a write method; only the main session writes to GitHub")
                if flag in GH_API_WRITE_FLAGS or re.fullmatch(r"-[fF].+", a):
                    raise Blocked("`gh api` with fields sends a POST; only the main session writes to GitHub")
        return
    raise Blocked(f"`gh {' '.join(words[:2])}` is not a read-only gh command; only the main session writes to GitHub")


def check(command, mode):
    for words, targets in segments(command):
        cmd = command_words(words)
        if mode == "boundary":
            for target in targets + (cmd[1:] if cmd[:1] == ["tee"] else []):
                if not target.startswith("-"):
                    boundary_path(target, CONTEXT["cwd"])
        if not cmd:
            continue
        name, args = cmd[0], cmd[1:]
        flag_c = [j for j, a in enumerate(args[:-1]) if re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", a)]
        ps_c = [j for j, a in enumerate(args[:-1]) if a.lower() in ("-c", "-command", "/c")]
        if name in ("pwsh", "powershell", "cmd") and ps_c:
            check(" ".join(args[ps_c[0] + 1:]), mode)  # pwsh -Command git push: the rest is the command
        elif name in SHELLS and flag_c:
            j = flag_c[0] + 1
            j += 1 if args[j] == "--" and j + 1 < len(args) else 0
            check(args[j], mode)
        elif name == "eval":
            check(" ".join(args), mode)
        elif name == "find":
            if mode == "boundary":
                starts = list(itertools.takewhile(lambda w: not w.startswith(("-", "(", "!")), args)) or ["."]
                execs = [args[j + 1] for j, a in enumerate(args[:-1]) if a in ("-exec", "-execdir", "-ok", "-okdir")]
                if "-delete" in args or any(program(e) in ("rm", "mv", "cp", "sed", "perl", "truncate", "shred", "chmod")
                                            for e in execs):
                    for start in starts:
                        boundary_path(start, CONTEXT["cwd"])
            for j, a in enumerate(args):
                if a in ("-exec", "-execdir", "-ok", "-okdir"):
                    inner = list(itertools.takewhile(lambda w: w not in (";", "+", "\\;"), args[j + 1:]))
                    check(" ".join(shlex.quote(w) for w in inner), mode)
        elif mode == "boundary":
            check_boundary(name, args, words, command)
        elif name == "git":
            check_git(args, mode)
        elif name == "gh":
            check_gh(args)


# The human-only boundary (autonomous runs). Loaded only through the settings file an
# autonomous run passes with `claude --settings`, so interactive sessions, where the owner is present, are
# unchanged. It applies to the main session and to every subagent of that run.

CONTEXT = {"cwd": "."}  # the hook's cwd, moved by `cd` in the command being checked
PUBLISH = {"npm": {"publish", "unpublish", "dist-tag", "deprecate"}, "pnpm": {"publish"}, "yarn": {"publish", "npm"},
           "cargo": {"publish", "yank", "owner"}, "twine": {"upload"}, "gem": {"push", "yank", "owner"},
           "docker": {"push"}, "podman": {"push"}, "uv": {"publish"}, "poetry": {"publish"}, "flit": {"publish"},
           "hatch": {"publish"}, "vsce": {"publish", "unpublish"}, "ovsx": {"publish"}, "nuget": {"push", "delete"},
           "swift": {"package-registry"}}
SIGNERS = {"codesign", "signtool", "osslsigncode", "productsign", "jarsigner", "apksigner", "notarytool", "ssh-keygen",
           "ssh-add", "security"}
SECRET_PATH = re.compile(r"(^|[/=])\.ssh/|id_(rsa|ed25519|ecdsa|dsa)\b|\.(pem|p12|pfx|keystore|jks|token)$|"
                         r"(^|/)secrets?/|\.config/gh/hosts\.yml|\.netrc|\.git-credentials|\.npmrc|\.pypirc|"
                         r"\.docker/config\.json|\.beads-credential-key|(^|/)\.env(\.local|\.production|\.development)?$")
# the guard's own wiring: an autonomous run never edits it
GUARD_PATH = re.compile(r"\.claude/(settings[^/]*\.json|hooks(/|$)|agents(/|$))|1prompt/hooks/|"
                        r"(^|/)\.claude/boundary\.json$")
RUNNERS = {"npx": 1, "bunx": 1, "uvx": 1, "xcrun": 1, ("pnpm", "dlx"): 1, ("pipx", "run"): 1, ("uv", "run"): 1,
           ("poetry", "run"): 1, ("npm", "exec"): 1, ("yarn", "dlx"): 1}
RUN_TARGETS = set(PUBLISH) | SIGNERS | {"gpg", "openssl", "git", "gh", "bd", "vercel", "netlify", "dotnet", "rm",
                                      "mv", "cp", "sed", "perl", "twine", "vsce", "ovsx"}
READERS = {"cat", "less", "more", "head", "tail", "grep", "rg", "egrep", "fgrep", "ls", "stat", "wc", "diff", "cmp",
           "sha256sum", "md5sum", "file", "jq", "test", "["}
PROTECTED_DEFAULT = ["docs/spec/*", "LICENSE*", "COPYING*", ".claude/boundary.json", ".claude/settings*.json",
                     ".claude/hooks/*", ".claude/agents/*"]
BD_GLOBAL_WITH_VALUE = {"--actor", "-C", "--directory", "--db", "--database", "--dolt-auto-commit"}
BD_READ = {"ready", "show", "list", "search", "count", "status", "statuses", "types", "history", "diff", "blocked",
           "graph", "info", "where", "version", "help", "prime", "quickstart", "lint", "stale", "find-duplicates",
           "duplicates", "query", "q", "schema", "children", "comments", "orphans", "recall", "context"}
BD_WRITE = {"create", "update", "close", "reopen", "comment", "note", "heartbeat", "unclaim", "reclaim", "defer",
            "undefer", "label", "remember"}
BD_SUB_READ = {"dep": {"list", "cycles", "tree"}, "gate": {"list", "check", "show"}, "human": {"list", "stats"},
               "events": {"list", "show", "tail"}, "label": {"list", "list-all"}}
# bd update flags an agent may use: status, claim, notes, evidence; never title, acceptance, design, type, deps
BD_UPDATE_OK = {"--claim", "-s", "--status", "--notes", "--append-notes", "--set-metadata", "--unset-metadata",
                "--add-label", "--if-status", "--if-assignee", "--session", "-e", "--estimate", "--json", "-q",
                "--quiet", "--actor"}
# bd flags that take a value (bd 1.3.1 help for create, update, close, label, comment); the next word is that value
BD_VALUE_FLAGS = {"--acceptance", "--add-label", "--remove-label", "--set-labels", "-a", "--assignee", "--append-notes",
                  "--await-id", "--body-file", "--defer", "-d", "--description", "--design", "--design-file", "--due",
                  "-e", "--estimate", "--external-ref", "--if-assignee", "--if-status", "--metadata", "--notes",
                  "--parent", "-p", "--priority", "--session", "--set-metadata", "--unset-metadata", "--spec-id",
                  "-s", "--status", "--title", "-t", "--type", "-r", "--reason", "--reason-file", "--actor",
                  "--deps", "-l", "--labels", "--graph", "--mol-type", "--event-actor", "--event-category",
                  "--event-payload", "--event-target", "--id", "--prefix", "-m", "--message"}
BD_FROZEN_META = {"gate", "rating", "acceptance", "deps"}
OWNER_LABELS = {"human", "needs-owner"}


AFTER_CHANGE_OK = {"cat", "less", "head", "tail", "grep", "rg", "ls", "stat", "wc", "diff", "echo", "printf", "pwd", "bd",
                   "true", "test", "["}


def after_change_ok(command):
    """Once a protected path changed, only reading, `git status/diff/log/show` and `bd` (to park the bead) run."""
    for words, targets in segments(command):
        cmd = command_words(words)
        if targets and any(t not in ("/dev/null",) for t in targets):
            return False
        if not cmd:
            continue
        if any(a.startswith("--output") for a in cmd[1:]):
            return False
        if cmd[0] == "git":
            rest, j = cmd[1:], 0
            while j < len(rest) and rest[j].startswith("-"):
                j += 2 if rest[j] in GIT_OPTS_WITH_VALUE else 1
            if (rest[j] if j < len(rest) else "") not in ("status", "diff", "log", "show"):
                return False
        elif cmd[0] not in AFTER_CHANGE_OK:
            return False
    return True


def check_boundary(name, args, words, command):
    if name == "cd":
        target = os.path.expanduser(args[0]) if args else os.path.expanduser("~")
        CONTEXT["cwd"] = os.path.normpath(os.path.join(CONTEXT["cwd"], target))
        return
    if name.startswith("python") and len(args) == 2 and args[1] == "--self-test" and args[0].endswith("agent-guard.py"):
        return  # running the guard's own self-test reads it, nothing more
    message = {j + 1 for j, a in enumerate(args) if a in ("-m", "--message", "-F")} if name == "git" else set()
    for j, w in enumerate([name] + args):
        if j - 1 in message or w.startswith("--message="):
            continue  # a commit message names things; it does not touch them
        if SECRET_PATH.search(w):
            raise Blocked(f"`{w}` is key or credential material; in an autonomous run only the owner handles it")
        if GUARD_PATH.search(w) and name not in READERS:
            raise Blocked(f"`{w}` is part of the guard's own wiring; an autonomous run does not change it")
    first = args[0] if args else ""
    runner = RUNNERS.get(name) or RUNNERS.get((name, first))
    if runner:  # npx vsce publish, xcrun --sdk x codesign, uv run --with twine twine upload: check what it runs
        tail = args[(0 if name in RUNNERS else 1):]
        words_in = [j for j, a in enumerate(tail) if not a.startswith("-")]
        known = [j for j in words_in if program(tail[j]) in RUN_TARGETS]
        for j in (known or words_in[:1]):  # each candidate: an option's value can carry a command's name
            check_boundary(program(tail[j]), tail[j + 1:], words, command)
        return
    if name in ("rm", "rmdir", "unlink", "mv", "truncate", "shred", "touch", "install", "chmod", "chown"):
        for a in args:
            if not a.startswith("-"):
                boundary_path(a, CONTEXT["cwd"])
    if name == "ln" and [a for a in args if not a.startswith("-")]:
        boundary_path([a for a in args if not a.startswith("-")][-1], CONTEXT["cwd"])  # the link written, not its target
    if name in ("cp", "rsync", "dd") and args:
        target = [a for a in args if not a.startswith("-")][-1:] or [""]
        target = [a[3:] for a in args if a.startswith("of=")] or target
        boundary_path(target[0], CONTEXT["cwd"]) if target[0] else None
    if name in ("sed", "perl") and any(re.fullmatch(r"-[a-zA-Z]*i.*|--in-place.*", a) for a in args):
        files = [a for a in args if not a.startswith("-")]
        for a in files[0 if "-e" in args or "-pe" in args else 1:]:
            boundary_path(a, CONTEXT["cwd"])
    if name in PUBLISH and (first in PUBLISH[name] or (name == "swift" and first == "package-registry")):
        raise Blocked(f"`{name} {first}` publishes; releases and publishing are the owner's")
    if name == "dotnet" and first == "nuget" and len(args) > 1 and args[1] in ("push", "delete"):
        raise Blocked("`dotnet nuget push` publishes; releases and publishing are the owner's")
    if name in ("vercel", "netlify") and ({"--prod", "--production"} & set(args) or first in ("promote", "rollback", "alias", "remove", "rm", "env", "secrets", "domains")):
        raise Blocked(f"`{name}` production deploy or settings; publishing is the owner's")
    if name == "certutil" and any(a.lower().lstrip("-/") in ("addstore", "delstore", "importpfx", "exportpfx", "repairstore",
                                                             "setreg", "user") for a in args):
        raise Blocked("`certutil` changes a certificate store; in an autonomous run only the owner does that")
    if name in SIGNERS or (name == "xcrun" and first == "notarytool"):
        raise Blocked(f"`{name}` signs or handles keys; in an autonomous run only the owner does that")
    if name == "gpg" and any(a.split("=")[0] in ("-s", "--sign", "--clearsign", "--detach-sign", "-b", "--gen-key", "--full-generate-key",
                                                 "--quick-generate-key", "--import", "--export-secret-keys", "--delete-secret-keys") for a in args):
        raise Blocked("`gpg` signing or key handling; in an autonomous run only the owner does that")
    if name == "openssl" and first in ("genrsa", "genpkey", "req", "pkcs12", "ca", "x509"):
        raise Blocked(f"`openssl {first}` creates or converts keys or certificates; only the owner does that")
    if name == "git":
        boundary_git(args)
    elif name == "gh":
        boundary_gh(args)
    elif name == "bd":
        boundary_bd(args, words, command)


def git_cwd(args):
    cwd = CONTEXT["cwd"]
    for j, a in enumerate(args[:-1]):
        if a == "-C":
            cwd = os.path.join(cwd, os.path.expanduser(args[j + 1]))
    return cwd


def boundary_git(args):
    i = 0
    while i < len(args) and args[i].startswith("-"):
        opt = args[i].split("=", 1)[0]
        i += 2 if opt in GIT_OPTS_WITH_VALUE and "=" not in args[i] else 1
    if i >= len(args):
        return
    sub, rest = args[i], args[i + 1:]
    if sub == "tag" and not git_read_only("tag", rest):
        raise Blocked("`git tag` creates or deletes a tag; tags and releases are the owner's")
    if sub in ("filter-branch", "filter-repo"):
        raise Blocked(f"`git {sub}` rewrites history for good; only the owner decides that")
    if sub in ("update-ref", "replace", "symbolic-ref"):
        raise Blocked(f"`git {sub}` rewrites refs directly; it could move the base protected paths are checked against")
    if sub == "branch" and any(re.fullmatch(r"-[a-zA-Z]*[fMmDC][a-zA-Z]*|--force|--move|--delete|--copy", a) for a in rest) \
            and any(re.fullmatch(r"(refs/heads/)?(main|master)|origin/.*|refs/remotes/.*", a) for a in rest):
        raise Blocked("`git branch -f/-m/-D` on main or a remote branch moves a ref the owner's base depends on")
    if any(a.lower().startswith("remote.") for j, a in enumerate(args[:i]) if j and args[j - 1] == "-c") or \
            any(a.startswith("-cremote.") for a in args[:i]):
        raise Blocked("`git -c remote.…` redefines a remote for one command; it could move the owner's base")
    if sub == "remote" and (rest[:1] and rest[0] not in ("-v", "--verbose", "show", "get-url")):
        raise Blocked(f"`git remote {rest[0]}` changes a remote or its default branch, which the owner's base depends on")
    if sub in ("fetch", "pull") and any(a.startswith("--refmap") for a in rest):
        raise Blocked(f"`git {sub} --refmap` can overwrite remote-tracking refs, the owner's base included")
    if sub == "pull" and any(":" in a for a in rest if not a.startswith("-")):
        raise Blocked("`git pull` with a refspec can overwrite remote-tracking refs, the owner's base included")
    if sub == "fetch" and any(":" in a for a in rest if not a.startswith("-")):
        raise Blocked("`git fetch` with a refspec can overwrite remote-tracking refs, the owner's base included; fetch without one")
    key = next((a for a in rest if not a.startswith("-")), "").lower()
    if sub == "config" and re.match(r"(remote|branch|include|includeif)\.|core\.hookspath$", key) and \
            not ({"--get", "--get-all", "--list", "-l", "--get-regexp"} & set(rest)):
        raise Blocked("`git config` of remotes, branches, hooks or includes can move the owner's base or run code; only the owner does that")
    if sub == "update-index" and not set(rest) <= {"--refresh", "--really-refresh", "-q"}:
        raise Blocked("`git update-index` can hide changes from git (assume-unchanged, skip-worktree); only the owner does that")
    if sub in ("rm", "mv", "checkout", "restore"):
        after = rest[rest.index("--") + 1:] if "--" in rest else [a for a in rest if not a.startswith("-")]
        for a in after:
            boundary_path(a, git_cwd(args))
    if sub == "apply" and not ({"--check", "--stat", "--numstat", "--summary"} & set(rest)):
        top = work_tree(os.path.abspath(git_cwd(args))) or git_cwd(args)
        patches = [a for a in rest if not a.startswith("-")]
        if not patches:
            raise Blocked("`git apply` from stdin cannot be checked for protected paths; apply a patch file")
        for patch in patches:
            try:
                with open(os.path.join(git_cwd(args), os.path.expanduser(patch)), encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                raise Blocked(f"could not read the patch {patch} to check it for protected paths")
            for target in re.findall(r"^(?:\+\+\+|---) (?:[ab]/)?(\S+)", text, re.M):
                if target != "/dev/null":
                    boundary_path(os.path.join(top, target), top)
    if sub != "push":
        return
    flags = [a for a in rest if a.startswith("-")]
    names = [a for a in rest if not a.startswith("-")]
    for f in flags:
        if f.split("=")[0] in ("--tags", "--follow-tags", "--mirror", "--all", "--delete", "--prune", "--force",
                               "--force-with-lease", "--force-if-includes") or re.fullmatch(r"-[a-zA-Z]*[fd][a-zA-Z]*", f):
            raise Blocked(f"`git push {f}` pushes tags, deletes or overwrites remote refs; only the owner does that")
    def current_branch():
        try:
            out = subprocess.run(["git", "-C", git_cwd(args), "rev-parse", "--abbrev-ref", "HEAD"],
                                 capture_output=True, text=True, timeout=30)
            return out.stdout.strip() if out.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""

    def is_tag(name):
        try:
            return subprocess.run(["git", "-C", git_cwd(args), "show-ref", "--verify", "--quiet", f"refs/tags/{name}"],
                                  capture_output=True, timeout=30).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return True  # cannot tell: treat it as a tag

    for ref in names[1:]:
        src, _, dest = ref.rpartition(":") if ":" in ref else (ref, "", ref)
        if dest in ("HEAD", "@"):
            dest = current_branch() or "HEAD"
        if ref.startswith(("+", ":")) or "refs/tags/" in ref or re.fullmatch(r"(refs/heads/)?(main|master|HEAD)", dest) \
                or (not dest.startswith("refs/") and is_tag(dest)):
            raise Blocked(f"`git push … {ref}` overwrites, deletes, tags or writes the default branch; "
                          "merges go through the merge step, never a push to main")
    if len(names) <= 1:  # pushes the current branch: never main
        branch = current_branch()
        if branch in ("main", "master", "HEAD", ""):
            raise Blocked(f"`git push` from {branch or 'an unknown branch'}; a push to main is never done by an agent")
    if names[:1] == ["."] or any("refs/remotes/" in r for r in names[1:]):
        raise Blocked("`git push` into this repository's own refs can move the owner's base; push to the remote only")
    top = work_tree(os.path.abspath(git_cwd(args)))
    if top:
        sources = [r.lstrip("+").split(":", 1)[0] or "HEAD" for r in names[1:]] or ["HEAD"]
        for src in dict.fromkeys(sources):
            if git(top, "rev-parse", "-q", "--verify", f"{src}^{{commit}}") is None:
                raise Blocked(f"`git push` of {src}: not a commit this hook can check for protected changes")
            try:
                own = own_protected_changes(top, "HEAD" if src in ("HEAD", "@", current_branch()) else src)
            except RuntimeError as error:
                raise Blocked(f"could not check {src} for protected changes before the push ({error})")
            if own:
                raise Blocked(f"{src} changes protected paths ({', '.join(own[:5])}); it is not pushed until the owner has looked")


def boundary_gh(args):
    kept, j = [], 0
    while j < len(args):
        if args[j] in ("-R", "--repo", "--hostname"):
            j += 2
            continue
        if not args[j].startswith(("--repo=", "--hostname=")):
            kept.append(args[j])
        j += 1
    words = [a for a in kept if not a.startswith("-")]
    group, sub = (words + ["", ""])[:2]
    if group == "release" and sub not in ("view", "list", "download"):
        raise Blocked(f"`gh release {sub}`: releases are the owner's")
    if group == "repo" and (sub in ("create", "delete", "archive", "unarchive", "rename", "fork")
                            or (sub == "edit" and any(a.startswith(("--visibility", "--accept-visibility")) for a in kept))):
        raise Blocked(f"`gh repo {sub}` creates, deletes, renames or changes the visibility of a repo; that is the owner's")
    if group in ("secret", "ssh-key", "gpg-key") or (group == "variable" and sub in ("set", "delete")) or \
            (group == "auth" and sub != "status"):
        raise Blocked(f"`gh {group} {sub}` handles credentials; in an autonomous run only the owner does that")
    if group == "workflow" and sub in ("run", "enable", "disable"):
        raise Blocked(f"`gh workflow {sub}` can start a release or deploy; the owner starts workflows")
    if group == "api":
        method, endpoint, k = "GET", "", kept.index("api") + 1
        while k < len(kept):  # the endpoint is the first word that is not a flag or a flag's value
            if kept[k] in ("-X", "--method", "-f", "-F", "--field", "--raw-field", "-H", "--header", "--input", "-q",
                           "--jq", "-t", "--template", "-p", "--preview", "--cache"):
                k += 2
                continue
            if not kept[k].startswith("-"):
                endpoint = kept[k]
                break
            k += 1
        for k, a in enumerate(kept):
            flag, _, value = a.partition("=")
            if flag.startswith("-X") and flag != "-X":
                flag, value = "-X", flag[2:]
            if flag in ("-X", "--method"):
                method = (value or (kept[k + 1] if k + 1 < len(kept) else "")).upper()
            elif method == "GET" and (flag in GH_API_WRITE_FLAGS or re.fullmatch(r"-[fF].+", a)):
                method = "POST"
        query_text = " ".join(kept)
        for a in kept:
            a = re.sub(r"^-[fF](?=\w+=@)", "", a)  # the attached form -Fquery=@file
            if re.match(r"(query|mutation)=@", a.split(" ")[-1]) or a.startswith("query=@"):
                try:
                    with open(os.path.join(CONTEXT["cwd"], os.path.expanduser(a.split("=@", 1)[1])), encoding="utf-8") as f:
                        query_text += " " + f.read()
                except OSError:
                    raise Blocked("could not read the GraphQL query file to check it for owner-only mutations")
        if method != "GET" and endpoint.strip("/") == "graphql" and re.search(
                r"\b(create|delete|update)(Ref|Release|Repository)\b|\b(archive|transfer|clone)Repository\b|\bdeleteBranchProtectionRule\b",
                query_text):
            raise Blocked("a GraphQL mutation on refs, releases or repositories; that is the owner's")
        if method != "GET" and (method == "DELETE" or re.search(r"/releases|/git/refs|/tags/|/secrets|/keys|"
                                                                r"/hooks|/collaborators|/environments|^/?repos/[^/]+/[^/]+/?$|"
                                                                r"^/?(user|orgs/[^/]+)/repos/?$", endpoint)):
            raise Blocked(f"`gh api {method} {endpoint}` deletes, releases, or changes repo settings or credentials; that is the owner's")


def bd_options(rest):
    """Splits bd arguments into (flags as (name, value) pairs, positional words), using the flags that take a value."""
    flags, names, k = [], [], 0
    while k < len(rest):
        a = rest[k]
        if a.startswith("-") and len(a) > 1:
            name, eq, value = a.partition("=")
            if not eq and name in BD_VALUE_FLAGS:
                value = rest[k + 1] if k + 1 < len(rest) else ""
                k += 1
            flags.append((name, value))
        else:
            names.append(a)
        k += 1
    return flags, names


def boundary_bd(args, words, command):
    exported = re.search(r"(^|[\s;&|(])export\s+DO_NOT_TRACK=1\b", command)
    if os.environ.get("DO_NOT_TRACK") != "1" and "DO_NOT_TRACK=1" not in words and not exported:
        raise Blocked("`bd` without DO_NOT_TRACK=1: beads sends usage metrics by default; run it with DO_NOT_TRACK=1")
    i, cwd = 0, CONTEXT["cwd"]
    while i < len(args) and args[i].startswith("-"):
        opt = args[i].split("=", 1)[0]
        if opt in ("-C", "--directory") and "=" not in args[i] and i + 1 < len(args):
            cwd = os.path.join(cwd, args[i + 1])
        if opt in ("--db", "--database", "--global"):
            raise Blocked(f"`bd {opt}` points at another database; the run uses the project's own graph")
        i += 2 if opt in BD_GLOBAL_WITH_VALUE and "=" not in args[i] else 1
    if i >= len(args):
        return
    sub, rest = args[i], args[i + 1:]
    sub = "close" if sub == "done" else sub  # `bd done` is an alias of close
    flags, names = bd_options(rest)
    given = {f for f, _ in flags}
    first = names[0] if names else ""
    if sub in BD_READ or first in BD_SUB_READ.get(sub, ()) or given & {"--help", "-h"}:
        return
    if sub not in BD_WRITE:
        raise Blocked(f"`bd {sub}` is not on the autonomous allowlist (it can change a bead's definition, the "
                      "database or beads' settings); ask the owner")
    if given & {"--force", "-f"}:
        raise Blocked(f"`bd {sub} --force` overrides a claim, a blocker or a gate; ask the owner")
    if sub == "create" and given & {"--graph", "--mol-type"}:
        raise Blocked("`bd create --graph` writes a whole plan; the plan's nodes are the owner's")
    if sub == "create" and any(f == "--deps" and re.search(r"(^|,)\s*blocks:", v) for f, v in flags):
        raise Blocked("`bd create --deps blocks:…` adds a blocker to an existing bead, which changes its definition")
    labels_out = [v for f, v in flags if f in ("--remove-label", "--set-labels")] + (names[1:] if sub == "label" and first in ("remove", "rm") else [])
    labels_in = [v for f, v in flags if f in ("--add-label", "-l", "--labels")] + (names[1:] if sub == "label" and first == "add" else [])
    if "--set-labels" in given or OWNER_LABELS & set(",".join(labels_out).split(",")):
        raise Blocked("removing or replacing labels can answer an owner question (`human`, `needs-owner`); only the owner does that")
    if sub in ("update", "label") and "optional" in ",".join(labels_in).split(","):
        raise Blocked("labelling a bead `optional` takes it off the path to done; that changes the plan, so ask the owner")
    if sub in ("defer", "undefer"):
        raise Blocked(f"`bd {sub}` hides or shows a bead in `bd ready`, which changes the plan; ask the owner")
    if sub == "update":
        for flag, value in flags:
            if flag not in BD_UPDATE_OK:
                raise Blocked(f"`bd update {flag}` changes a bead's definition or its place in the plan (title, "
                              "acceptance, design, type, deps, parent, priority and deferral are the owner's); propose it")
            if flag in ("--set-metadata", "--unset-metadata") and value.split("=", 1)[0] in BD_FROZEN_META:
                raise Blocked(f"metadata `{value.split('=', 1)[0]}` is part of the bead's definition; propose the change to the owner")
        if not any(f in ("-s", "--status") and v in ("closed", "done") for f, v in flags):
            return
    if sub in ("close", "update"):
        if given & {"--claim-next", "--continue"}:
            raise Blocked("`bd close --claim-next/--continue` acts on beads it has not named; close by id")
        if not names:
            raise Blocked(f"`bd {sub}` without a bead id acts on the last touched bead; name the bead")
        for bead in names:
            try:
                out = subprocess.run(["bd", "-C", cwd, "show", bead, "--json"], capture_output=True, text=True,
                                     timeout=60, env=dict(os.environ, DO_NOT_TRACK="1"))
                data = json.loads(out.stdout) if out.returncode == 0 else None
                data = data[0] if isinstance(data, list) else data
                if not isinstance(data, dict) or "issue_type" not in data or not (
                        data.get("id") == bead or str(data.get("id", "")).endswith("-" + bead)):  # bd resolves a short id
                    raise ValueError("not this bead")
            except (OSError, subprocess.SubprocessError, ValueError, IndexError, KeyError, AttributeError):
                raise Blocked(f"could not read bead {bead} to check that it is not an owner's gate; not closing it")
            if data.get("issue_type") == "gate" or OWNER_LABELS & set(data.get("labels") or []):
                raise Blocked(f"bead {bead} is a gate or waits for the owner; only the owner closes it")


def boundary_edit(event):
    """Edit, Write, MultiEdit, NotebookEdit: the guard's wiring and the protected paths are the owner's."""
    tool = event.get("tool_input") or {}
    path = tool.get("file_path") or tool.get("notebook_path") or ""
    if not isinstance(path, str) or not path:
        raise TypeError("no file path in the edit")
    boundary_path(path, event.get("cwd") or ".")


def boundary_config(top):
    """The project's `.claude/boundary.json` (a protected path itself): `protected`, a list of extra patterns, and
    `base`, the ref a long-lived branch is compared with. A broken file raises, and so blocks."""
    try:
        with open(os.path.join(top, ".claude", "boundary.json"), encoding="utf-8") as f:
            config = json.load(f)
    except FileNotFoundError:
        return {}
    extra, base = config.get("protected", []), config.get("base")
    if not isinstance(extra, list) or not all(isinstance(x, str) for x in extra):
        raise ValueError("protected must be a list of strings")
    if base is not None and (not isinstance(base, str) or not re.fullmatch(r"(refs/remotes/)?origin/[\w./-]+", base)):
        raise ValueError("base must be a remote branch such as origin/<branch>: a local branch can be moved by the run")
    return config


def protected_patterns(top):
    """PROTECTED_DEFAULT plus the project's `.claude/boundary.json` list."""
    return list(PROTECTED_DEFAULT) + boundary_config(top).get("protected", [])


def trusted_base(top):
    """The ref a branch's protected paths are compared with: the owner's `base` in boundary.json, else the remote's
    default branch. Never the branch's own upstream or a local branch: the run can push or commit to those."""
    configured = boundary_config(top).get("base")
    names = [configured] if configured else ["origin/HEAD", "origin/main", "origin/master"]
    for base in ("refs/remotes/" + n.removeprefix("refs/remotes/") for n in names):  # full names: no local look-alike
        if git(top, "rev-parse", "-q", "--verify", base + "^{commit}") is not None:
            return base
    if configured:
        raise RuntimeError(f"the base {configured} in .claude/boundary.json does not resolve; fetch it")
    return None


def own_protected_changes(top, rev="HEAD"):
    """Protected paths a branch changes: uncommitted (untracked included, for HEAD only), or committed since it forked
    from the trusted base; a branch with no common history with the base counts as a change. A pull, merge or rebase
    of the owner's base brings none in; a commit of the run's own does."""
    specs = protected_specs(top)
    found = [] if rev != "HEAD" else [e.decode(errors="replace") for e in (git(
        top, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *specs) or b"").split(b"\0") if e]
    base = trusted_base(top)
    fork = git(top, "merge-base", base, rev) if base else None
    if base and not fork:
        found.append(f"{rev} has no common history with {base.removeprefix('refs/remotes/')}")
    if fork:
        names = git(top, "diff", "--name-only", fork.decode().strip(), rev, "--", *specs)
        if names is None:
            raise RuntimeError(f"git diff against {base} failed in {top}")
        found += [f"committed on this branch: {n}" for n in names.decode(errors="replace").splitlines() if n]
    return found


def protected_specs(top):
    specs = []
    for pattern in protected_patterns(top):
        specs.append(f":(glob,icase){pattern}")
        if pattern.endswith("/*"):
            specs.append(f":(glob,icase){pattern}*")  # docs/spec/* also covers deeper files
    return specs


def protected_already_changed(top):
    """At a run's first look at a tree: protected changes this branch already carries, so a change left by an earlier
    run (pushed or not) is not taken as the new normal."""
    return own_protected_changes(top)


def protected_fingerprint(top):
    """The protected paths of one work tree: their status (untracked included), their diff from HEAD and their
    staged blobs, so an edit, a new file, a delete and a commit that changes them all show. Ordinary commits do not."""
    specs = protected_specs(top)
    status = git(top, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *specs) or b""
    digest = hashlib.sha256()
    index = git(top, "ls-files", "-s", "--", *specs)  # the staged blobs: a commit or `git add` of a spec changes them
    if index is None:
        raise RuntimeError(f"git ls-files failed in {top}")  # never a silent empty fingerprint
    digest.update(index)
    digest.update(git(top, "diff", "HEAD", "--binary", "--", *specs) or b"")
    entries = [e.decode(errors="replace") for e in status.split(b"\0") if e]
    for entry in entries:
        path = os.path.join(top, entry[3:])
        if entry.startswith("??") and os.path.isfile(path):
            try:
                with open(path, "rb") as f:
                    digest.update(f.read())
            except OSError:
                st = os.stat(path)
                digest.update(f"unreadable {entry} {st.st_size} {st.st_mtime_ns}".encode())
    return {"status": sorted(entries), "digest": digest.hexdigest()}


GUARD_TREE = "~guard"  # the guard's own wiring in ~/.claude, watched like a protected tree


def guard_fingerprint():
    home = os.path.expanduser("~/.claude")
    digest, names = hashlib.sha256(), []
    files = [os.path.join(home, n) for n in sorted(os.listdir(home))] if os.path.isdir(home) else []
    for folder in ("hooks", "agents"):
        for root, _, found in os.walk(os.path.join(home, folder)):
            files += [os.path.join(root, n) for n in sorted(found)]
    for path in files:
        name = os.path.relpath(path, home)
        if (name.startswith("settings") and name.endswith(".json")) or name.startswith(("hooks", "agents")):
            try:
                with open(path, "rb") as f:
                    digest.update(name.encode() + b"\0" + f.read())
                names.append(name)
            except OSError:
                digest.update(f"unreadable {name}".encode())
    return {"status": names, "digest": digest.hexdigest()}


def boundary_path(path, cwd):
    """A path about to be written (an edit, a redirect, tee): the guard's wiring, key files and protected paths."""
    if path in ("/dev/null", "/dev/stdout", "/dev/stderr") or path.startswith("&"):
        return
    full = os.path.realpath(os.path.join(cwd, os.path.expanduser(path)))
    if GUARD_PATH.search(full):
        raise Blocked(f"{path} is part of the guard's own wiring; an autonomous run does not change it")
    if SECRET_PATH.search(full):
        raise Blocked(f"{path} is key or credential material; in an autonomous run only the owner handles it")
    top = work_tree(full)
    if not top:
        return
    rel = os.path.relpath(full, top).replace(os.sep, "/")
    patterns = protected_patterns(top)
    low = rel.lower()
    for pattern in patterns:
        prefix = re.split(r"[*?\[]", pattern.lower(), maxsplit=1)[0].rsplit("/", 1)[0] if "/" in pattern else ""
        # the path itself, or a folder that holds protected paths (`rm -rf docs`, `cp x docs/spec/`, `checkout -- .`)
        if fnmatch(low, pattern.lower()) or low == "." or (prefix and (low == prefix or prefix.startswith(low + "/"))):
            raise Blocked(f"{rel} is or holds a protected path ({pattern}): a spec, licence or listed path changes only "
                          "with the owner; propose the change in the report")


# The work-tree watch (reviewers). A snapshot per git work tree the reviewer touches, taken before its first
# tool call there; compared after every shell call and at the end.

def git(top, *args):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")  # never take the index lock another agent may need
    run = subprocess.run(["git", "-C", top, *args], capture_output=True, env=env, timeout=60)
    return run.stdout if run.returncode == 0 else None


def work_tree(path):
    folder = path if os.path.isdir(path) else os.path.dirname(path)
    while folder and not os.path.isdir(folder):
        folder = os.path.dirname(folder)
    out = git(folder, "rev-parse", "--show-toplevel") if folder else None
    return os.path.realpath(out.decode().strip()) if out else None


def fingerprint(top):
    """HEAD, `git status` and a digest of every tracked change and every untracked (not ignored) file."""
    status = git(top, "status", "--porcelain=v1", "-z", "--untracked-files=all") or b""
    digest = hashlib.sha256()
    digest.update(git(top, "rev-parse", "-q", "--verify", "HEAD") or b"no HEAD")
    digest.update(git(top, "diff", "HEAD", "--binary") or git(top, "diff", "--cached", "--binary") or b"")
    entries = [e.decode(errors="replace") for e in status.split(b"\0") if e]
    for entry in entries:
        path = os.path.join(top, entry[3:])
        if entry.startswith("??") and os.path.isfile(path):
            try:
                with open(path, "rb") as f:
                    for block in iter(lambda: f.read(1 << 20), b""):
                        digest.update(block)
            except OSError:  # unreadable: its size and time still show a change
                st = os.stat(path)
                digest.update(f"unreadable {entry} {st.st_size} {st.st_mtime_ns}".encode())
    return {"status": sorted(entries), "digest": digest.hexdigest()}


def state_file(event, kind="review"):
    who = f"{event.get('session_id', '')}:{event.get('agent_id') or event.get('agent_type') or 'main'}"
    who += "" if kind == "review" else f":{kind}"
    folder = os.path.join(tempfile.gettempdir(), f"agent-guard-{os.getuid() if hasattr(os, 'getuid') else 'u'}")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, hashlib.sha256(who.encode()).hexdigest()[:24] + ".json")


def load(event, kind="review"):
    try:
        with open(state_file(event, kind), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"trees": {}, "changed": {}}


def save(event, state, kind="review"):
    with open(state_file(event, kind), "w", encoding="utf-8") as f:
        json.dump(state, f)


def tree_fingerprint(top, kind):
    if kind == "review":
        return fingerprint(top)
    return guard_fingerprint() if top == GUARD_TREE else protected_fingerprint(top)


def touched_paths(event):
    """The places a tool call points at: its cwd, its path arguments, and absolute paths or cd targets in a command."""
    cwd = event.get("cwd") or os.getcwd()
    tool_input = event.get("tool_input") or {}
    paths = [cwd] + [tool_input[k] for k in ("file_path", "path", "notebook_path") if isinstance(tool_input.get(k), str)]
    command = tool_input.get("command")
    if isinstance(command, str):
        try:
            for words, targets in segments(command):
                for j, w in enumerate(words + targets):
                    w = os.path.expanduser(w)
                    if w.startswith("/") or (j and words[j - 1:j] == ["cd"]):
                        paths.append(os.path.join(cwd, w))
        except Blocked:
            pass
        # an absolute path anywhere in the text, also inside a string such as python -c "open('/x/y', 'w')"
        paths += [os.path.expanduser(m) for m in re.findall(r"(?<![\w.~/])((?:~|/)[^\s'\"`;|&()<>,]+)", command)]
    seen = list(dict.fromkeys(paths))
    return seen[:60]


def watch(event, kind="review"):
    """Snapshots each work tree this call touches that has no snapshot yet (PreToolUse: before the tool runs).
    kind "boundary" snapshots only the protected paths of each tree, plus the guard's wiring once."""
    state = load(event, kind)
    tops = [work_tree(path) for path in touched_paths(event)] + ([GUARD_TREE] if kind == "boundary" else [])
    for top in tops:
        if top and top not in state["trees"]:
            state["trees"][top] = tree_fingerprint(top, kind)
            if kind == "boundary" and top != GUARD_TREE:
                already = protected_already_changed(top)
                if already:
                    state["changed"][top] = already
    save(event, state, kind)


def matches_base(top):
    """After a change: the protected paths carry nothing of this branch's own, so the change came in from the
    owner's base (a pull, merge, rebase or checkout), not from the run. Without a trusted base that cannot be shown."""
    return trusted_base(top) is not None and not own_protected_changes(top)


def changes(event, kind="review"):
    """Work trees whose fingerprint differs from the snapshot: {top: [status entries that are new or gone]}."""
    state, found = load(event, kind), {}
    for top, before in state["trees"].items():
        now = tree_fingerprint(top, kind)
        if now["digest"] != before["digest"] and kind == "boundary" and top != GUARD_TREE and matches_base(top):
            state["trees"][top] = now  # the specs now match the upstream: brought in, not written
            continue
        if now["digest"] != before["digest"]:
            diff = sorted(set(now["status"]) ^ set(before["status"]))
            if not diff and kind == "boundary" and top != GUARD_TREE:
                try:
                    diff = own_protected_changes(top)  # a commit leaves status clean: name what the branch changed
                except RuntimeError:
                    pass
            diff = diff or ["(the content of a file that was already changed)"]
            found[top] = diff
            state["trees"][top] = now  # report each change once
            state["changed"].setdefault(top, [])
            state["changed"][top] = sorted(set(state["changed"][top]) | set(diff))
    save(event, state, kind)
    return found, state


def describe(found):
    return "; ".join(f"{top}: {', '.join(entries[:10])}" for top, entries in found.items())


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "--self-test":
        return self_test()
    if mode not in ("build", "review", "review-after", "review-stop", "boundary", "boundary-after", "boundary-stop"):
        print(f"agent-guard: unknown mode '{mode}'; blocking", file=sys.stderr)
        return 2
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise TypeError("hook input is not an object")
        if mode == "boundary-after":
            found, _ = changes(event, "boundary")
            if found:
                print(f"agent-guard: a protected path changed. Do not undo it yourself: stop work on this bead, park it "
                      f"as needs-owner, and make the first line of your report: PROTECTED CHANGED: {describe(found)}",
                      file=sys.stderr)
                return 2
            return 0
        if mode == "boundary-stop":
            _, state = changes(event, "boundary")
            said = str(event.get("last_assistant_message") or "").lstrip().startswith("PROTECTED CHANGED")
            if state["changed"] and not said and not event.get("stop_hook_active"):
                print("agent-guard: before you finish, make the first line of your report: PROTECTED CHANGED: "
                      f"{describe(state['changed'])}", file=sys.stderr)
                return 2
            if event.get("hook_event_name") == "SubagentStop" and not event.get("agent_id"):
                return 0  # cannot tell whose state this is: never drop the main session's snapshot
            if state["changed"]:
                return 0  # keep the lock for later turns of this session; a new session takes its own look
            try:
                os.remove(state_file(event, "boundary"))
            except FileNotFoundError:
                pass
            return 0
        if mode == "review-after":
            found, _ = changes(event)
            if found:
                print(f"agent-guard: the work tree changed during this review ({describe(found)}). A reviewer never "
                      "edits: do not try to undo it; say so at the top of your report.", file=sys.stderr)
                return 2
            return 0
        if mode == "review-stop":
            _, state = changes(event)
            if state["changed"] and not event.get("stop_hook_active"):
                print("agent-guard: before you finish, make the first line of your report: TREE CHANGED: "
                      f"{describe(state['changed'])}", file=sys.stderr)
                return 2
            os.remove(state_file(event))
            return 0
        if mode == "review":
            watch(event)
        if mode == "boundary":
            CONTEXT["cwd"] = event.get("cwd") or "."
            watch(event, "boundary")
            if load(event, "boundary")["changed"]:
                command = (event.get("tool_input") or {}).get("command")
                if event.get("tool_name") in ("Read", "Grep", "Glob", "LS"):
                    return 0
                if not (event.get("tool_name") in ("Bash", "PowerShell") and isinstance(command, str) and after_change_ok(command)):
                        raise Blocked("a protected path changed earlier in this run; only reading and `bd` (to park the bead) "
                                  "are allowed until the owner has looked")
                # allowed while locked, and still checked like any other call below (bd rules, secrets)
            if event.get("tool_name") in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                boundary_edit(event)
                return 0
        if event.get("tool_name") not in ("Bash", "PowerShell"):
            return 0
        command = event["tool_input"].get("command") or ""
        if not isinstance(command, str):
            raise TypeError("command is not a string")
        check(command, mode if mode in ("review", "boundary") else "build")
    except Blocked as reason:
        who = "Put it in the report as a decision for the owner" if mode == "boundary" else "Report it to the main session instead"
        print(f"Blocked by agent-guard ({mode}): {reason}. {who}.", file=sys.stderr)
        return 2
    except Exception as error:  # odd input, or a bug here: block rather than let the call through
        print(f"agent-guard: could not check this call ({type(error).__name__}: {error}); blocking", file=sys.stderr)
        return 2
    return 0


CASES = [
    # (mode, command, blocked?)
    ("build", "cargo test --all", False),
    ("build", "git status && git diff --stat", False),
    ("build", "git log --oneline -5 | head", False),
    ("build", "git add -A", False),
    ("build", "git checkout -- src/lib.rs", False),
    ("build", "git branch --show-current", False),
    ("build", "git stash list", False),
    ("build", "cat > notes.txt <<'EOF'\ndon't git push here\nEOF", False),
    ("build", "echo hi > out.txt", False),
    ("build", "gh pr view 8 --json title", False),
    ("build", "gh api repos/o/r/pulls", False),
    ("build", "git commit -m 'x'", True),
    ("build", "cd sub && git commit -am x", True),
    ("build", "git -C ../repo push origin HEAD", True),
    ("build", "git -c user.name=x commit -m y", True),
    ("build", "FOO=1 env git push", True),
    ("build", "bash -c 'git merge main'", True),
    ("build", "echo $(git tag v1)", True),
    ("build", "git switch main", True),
    ("build", "git checkout -b topic", True),
    ("build", "git branch -D old", True),
    ("build", "git reset --hard HEAD~1", True),
    ("build", "git rebase -i main", True),
    ("build", "gh pr create --fill", True),
    ("build", "gh pr merge 8", True),
    ("build", "gh release create v1", True),
    ("build", "gh issue comment 3 -b hi", True),
    ("build", "gh api -X POST repos/o/r/issues", True),
    ("build", "gh api repos/o/r/issues -f title=x", True),
    ("build", "git config user.name x", True),
    ("build", "echo 'unbalanced", True),
    ("build", "if git diff --quiet; then echo clean; else git commit -am wip; fi", True),
    ("build", "for f in a b; do git push origin $f; done", True),
    ("build", "if true; then git push; fi", True),
    ("build", "while true; do sleep 1; done", False),
    ("build", "if git diff --quiet; then echo clean; fi", False),
    ("build", "git checkout main --", True),
    ("build", "gh api repos/o/r/issues -ftitle=x", True),
    ("build", "gh api repos/o/r/issues -Ftitle=x", True),
    ("build", "gh pr -R o/r view 8", False),
    ("build", "gh pr view 8 -R o/r", False),
    ("build", "gh pr -R o/r merge 8", True),
    ("build", "bash <<'EOF'\ngit push\nEOF", True),
    ("build", "bash -s <<EOF\necho hi\nEOF", False),
    ("build", "bash -c -- 'git push'", True),
    ("build", "pwsh -Command git push", True),
    ("build", "cmd /c git push", True),
    ("build", "pwsh -Command Get-ChildItem", False),
    ("build", "cat <<'EOF' | sh -\ngit push\nEOF", True),
    ("build", "bash -o pipefail <<'EOF'\ngit push\nEOF", True),
    ("build", "cat > notes.md <<'EOF' # later: notes.md | bash\ndon't git push here\nEOF", False),
    ("build", "python3 - <<'EOF'\nprint(\"don't\")\nEOF", False),
    ("build", "git -c alias.p=push p", True),
    ("build", "git -c core.pager=cat log -1", False),
    ("build", "git send-pack origin main", True),
    ("build", "git stash branch x", True),
    ("review", "git diff main...HEAD", False),
    ("review", "python3 -m pytest -q 2>&1 | tail -5", False),
    ("review", "grep -rn foo src >/dev/null", False),
    ("review", "ls 2>/dev/null", False),
    ("review", "echo x > /tmp/review-notes.txt", False),
    ("review", "mkdir -p /tmp/rv && cp a.txt /tmp/rv/", False),
    ("review", "sed -n 1,20p file.py", False),
    ("review", "git config --get user.name", False),
    ("review", "git add .", True),
    ("review", "git checkout -- f.py", True),
    ("review", "git stash", True),
    ("review", "git commit -m x", True),
    ("review", "echo err >&2", False),
    ("review", "rm /tmp/rv/x", False),
    ("build", "git checkout main", True),
    ("build", "timeout 30 git push", True),
    ("build", "make test 2>&1 | tee build.log", False),
    ("review", "sed -E -n 's/i/x/p' f", False),
    ("build", "bash -lc 'git push'", True),
    ("build", "gh api -XPOST repos/o/r/issues", True),
    ("build", "gh api -XGET repos/o/r/issues", False),
    ("build", "test -f x ||(git push)", True),
    ("build", "true &&(git commit -m x)", True),
    ("build", "grep x <<< \"git\"\ngit push", True),
    ("build", "git checkout -b topic --", True),
    ("build", "nice -n 10 git push", True),
    ("build", "sudo -u x git push", True),
    ("build", "git.exe push", True),
    ("build", "Git push", True),
    ("build", "git tag", False),
    ("build", "git branch --sort=-committerdate", False),
    ("build", "git config user.name", False),
    ("build", "git submodule update --init", False),
    ("review", "git apply --check p.diff", False),
    ("review", "chmod +x /tmp/x", False),
    ("build", "grep -rn '`git push`' docs/", False),
    ("build", "echo \"`git push`\"", True),
    ("build", "gh api repos/o/r/issues \\\n  -f title=x \\\n  -f body=y", True),
    ("build", "gh api repos/o/r/issues \\\n  --method POST", True),
    ("build", "git \\\n  push origin HEAD", True),
    ("build", "# don't run this yet\ncargo test", False),
    ("build", "cargo test # it's fine", False),
    ("build", "echo '#not a comment' && git push", True),
    ("build", "gh -R o/r pr view 8", False),
    ("build", "gh --repo o/r pr view 8", False),
    ("build", "gh -R o/r pr merge 8", True),
    ("build", "git remote -v", False),
    ("build", "git remote -v add origin x", True),
    ("build", "git branch -r -d origin/x", True),
    ("build", "git branch -a", False),
    ("build", "git branch topic", True),
    ("build", "git branch --contains HEAD", False),
    ("build", "git tag -l 'v*'", False),
    ("build", "git tag v1.0", True),
    ("review", "git tag -n9", False),
    ("build", "git stash show -p", False),
    ("review", "find . -name '*.rs'", False),
    ("review", "curl -s https://example.com", False),
    ("review", "tar tf a.tar", False),
    ("build", "setsid git push", True),
    ("build", "env -S 'git push'", True),
    ("review", "ls | xargs -n 1 echo", False),
    ("review", "sort -o /tmp/s in.txt", False),
    ("review", "curl -o /tmp/x https://example.com", False),
    ("review", "tar xf a.tar -C /tmp/x", False),
    ("review", "unzip -d /tmp/x a.zip", False),
    ("review", "unzip -l a.zip", False),
    ("review", "find . -name '*.py' -exec grep -l foo {} +", False),
    ("build", "find . -exec git add {} ;", False),
    ("review", "ln -s a /tmp/b", False),
    ("review", "perl -Ilib t/x.t", False),
    ("review", "perl -Mstrict -e 1", False),
    ("review", "grep -rn \">\" src", False),
    ("review", "grep -n '|' f", False),
    ("build", "pwsh -c 'git push'", True),
    ("build", "powershell -Command \"git push\"", True),
]

# (command, blocked?) in boundary mode; run with DO_NOT_TRACK=1 set and the cwd on a feature branch
BOUNDARY_CASES = [
    ("git status && git diff --stat", False),
    ("git commit -am wip && git push -u origin topic", False),
    ("gh pr create --fill && gh pr view 3", False),
    ("gh api repos/o/r/issues/3/comments -f body=hi", False),
    ("npm test && cargo build --release", False),
    ("git tag -l", False),
    ("git tag v1.0.0", True),
    ("git push --tags", True),
    ("git push origin v1.0.0:refs/tags/v1.0.0", True),
    ("git push --force origin topic", True),
    ("git push -f", True),
    ("git push origin :old-branch", True),
    ("git push origin HEAD:main", True),
    ("git push origin topic:master", True),
    ("git filter-repo --path x --invert-paths", True),
    ("gh release create v1", True),
    ("gh release view v1", False),
    ("gh repo edit --visibility public", True),
    ("gh repo delete o/r --yes", True),
    ("gh secret set TOKEN", True),
    ("gh auth token", True),
    ("gh auth status", False),
    ("gh workflow run release.yml", True),
    ("gh api -X DELETE repos/o/r/branches/x/protection", True),
    ("gh api repos/o/r/releases -f tag_name=v1", True),
    ("gh api -X PATCH repos/o/r -f private=false", True),
    ("npm publish", True),
    ("cargo publish --dry-run", True),
    ("dotnet nuget push x.nupkg", True),
    ("dotnet build -c Release", False),
    ("docker push img:1", True),
    ("vercel --prod", True),
    ("vercel build", False),
    ("codesign -s 'Developer ID' App.app", True),
    ("gpg --detach-sign file", True),
    ("gpg --verify file.sig", False),
    ("openssl genrsa -out k.pem 2048", True),
    ("openssl sha256 file", False),
    ("cat ~/.ssh/id_ed25519", True),
    ("cp ~/secrets/deploy.token /tmp/x", True),
    ("sed -i s/x/y/ ~/.claude/settings.json", True),
    ("cat ~/.claude/settings.json", False),
    ("echo '{}' > .claude/boundary.json", True),
    ("echo x > docs/spec/a.md", True),
    ("echo x | tee -a docs/spec/a.md", True),
    ("echo x > notes.txt 2>/dev/null", False),
    ("python3 -m pytest -q > /tmp/out.txt 2>&1", False),
    ("bd ready --json", False),
    ("bd show tr-1 --json", False),
    ("bd create 'found bug' -t bug --deps discovered-from:tr-1", False),
    ("bd update tr-1 --claim", False),
    ("bd update tr-1 --append-notes 'evidence: abc' --set-metadata evidence=gates.json", False),
    ("bd update tr-1 --add-label parked", False),
    ("bd update tr-1 --title 'easier'", True),
    ("bd update tr-1 --acceptance 'nothing'", True),
    ("bd update tr-1 --set-metadata gate=none", True),
    ("bd update tr-1 --unset-metadata rating", True),
    ("bd update tr-1 --remove-label needs-owner", True),
    ("bd update tr-1 --set-labels a,b", True),
    ("bd update tr-1 --parent tr-9", True),
    ("bd update tr-1 -a someone --force", True),
    ("bd edit tr-1", True),
    ("bd dep add tr-1 tr-2", True),
    ("bd dep list tr-1", False),
    ("bd delete tr-1", True),
    ("bd gate resolve tr-g1", True),
    ("bd gate list", False),
    ("bd human respond tr-h1", True),
    ("bd human list", False),
    ("bd config set metrics.disabled false", True),
    ("bd --db /tmp/other.db ready", True),
    ("bd create --graph plan.json", True),
    ("bd close --claim-next", True),
    ("bd close tr-does-not-exist --reason done", True),
    ("bd update tr-does-not-exist --status closed", True),
    ("bd init --stealth", True),
    ("bd update tr-gate --status=closed", True),
    ("bd update tr-gate -s=closed", True),
    ("bd update tr-1 --append-notes '-evidence: x'", False),
    ("bd update tr-1 -p 4", True),
    ("bd update tr-1 --add-label optional", True),
    ("bd update tr-1 --defer +1d", True),
    ("bd defer tr-1", True),
    ("bd create x --deps blocks:tr-1", True),
    ("bd done tr-does-not-exist", True),
    ("bd close", True),
    ("DO_NOT_TRACK=1 echo; bd ready", False),
    ("git push origin HEAD:topic", False),
    ("rm docs/spec/a.md", True),
    ("git rm docs/spec/a.md", True),
    ("mv docs/spec/a.md /tmp/a.md", True),
    ("cp x.md docs/spec/a.md", True),
    ("cp docs/spec/a.md /tmp/a.md", False),
    ("git checkout other -- docs/spec/a.md", True),
    ("sed -i s/a/b/ docs/spec/a.md", True),
    ("sed -i s/a/b/ src.py", False),
    ("rm -rf build", False),
    ("echo x > DOCS/SPEC/a.md", True),
    ("npx vsce publish", True),
    ("npx prettier --check .", False),
    ("xcrun codesign -s x App.app", True),
    ("uv run twine upload dist/*", True),
    ("uv run pytest", False),
    ("gh api repos/o/r/git/refs -f ref=refs/tags/v1 -f sha=abc", True),
    ("gh api graphql -f query='mutation { createRef(input: {}) { ref { name } } }'", True),
    ("gh api graphql -f query='query { viewer { login } }'", False),
    ("cat .env", True),
    ("rm -rf docs/spec", True),
    ("rm -rf docs", True),
    ("rm -rf .", True),
    ("mv docs/spec docs/old", True),
    ("cp x.md docs/spec/", True),
    ("cp -r new/ docs/spec", True),
    ("rsync -a --delete new/ docs/spec/", True),
    ("git rm -r docs/spec", True),
    ("git checkout other -- .", True),
    ("git checkout other -- src.py", False),
    ("rm -rf build dist", False),
    ("find docs/spec -name '*.md' -delete", True),
    ("find docs -exec rm {} +", True),
    ("find . -name '*.pyc' -exec grep x {} +", False),
    ("find build -delete", False),
    ("xcrun --sdk macosx codesign -s x App.app", True),
    ("uv run --with twine twine upload dist/*", True),
    ("uv run --with pytest pytest -q", False),
    ("bd label add tr-1 optional", True),
    ("bd label add tr-1 parked", False),
    ("git commit -m 'handle .npmrc and .env files'", False),
    ("certutil -hashfile x.exe SHA256", False),
    ("certutil -addstore Root c.cer", True),
    ("ln -s docs/spec/a.md link.md", False),
    ("ln -sf x docs/spec/a.md", True),
    ("python3 ~/.claude/hooks/1prompt/agent-guard.py --self-test", False),
    ("git apply", True),
    ("git update-index --skip-worktree docs/spec/a.md", True),
    ("git update-index --refresh", False),
    ("git update-ref refs/remotes/origin/main HEAD", True),
    ("git branch -f main HEAD", True),
    ("git branch -f topic2 HEAD", False),
    ("git fetch origin topic:refs/remotes/origin/main", True),
    ("git fetch origin", False),
    ("git config remote.origin.fetch +refs/heads/x:refs/remotes/origin/main", True),
    ("git config user.email a@b", False),
    ("git replace HEAD HEAD~1", True),
    ("git config user.name remote-worker", False),
    ("git config core.editor branchy", False),
    ("git remote set-head origin topic", True),
    ("git remote set-url origin https://example.invalid/x", True),
    ("git remote -v", False),
    ("git push . topic:refs/remotes/origin/main", True),
    ("git pull origin x:refs/remotes/origin/main", True),
    ("git fetch --refmap=+refs/heads/topic:refs/remotes/origin/main origin topic", True),
    ("git -c remote.x.url=/tmp/x -c remote.x.fetch=+refs/heads/*:refs/remotes/origin/* fetch x", True),
    ("echo '{}' > .claude/settings.local.json", True),
    ("bd sql 'select 1'", True),
]


def boundary_test():
    """Boundary mode: the command cases, `bd` without DO_NOT_TRACK, a bare push from main, and protected edits."""
    bad, saved_env, saved_cwd = [], os.environ.get("DO_NOT_TRACK"), CONTEXT["cwd"]
    with tempfile.TemporaryDirectory() as tmp:
        repo = os.path.join(tmp, "repo")
        os.makedirs(os.path.join(repo, "docs", "spec"))
        subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True)
        for rel in ("docs/spec/a.md", "src.py"):
            with open(os.path.join(repo, rel), "w") as f:
                f.write("x\n")
        subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"], check=True)

        def expect(label, call, want):
            try:
                call()
                got = False
            except Blocked:
                got = True
            if got != want:
                bad.append(f"boundary: {label} {'blocked' if got else 'allowed'}, expected {'blocked' if want else 'allowed'}")

        os.environ["DO_NOT_TRACK"] = "1"
        expect("a bare push from main", lambda: (CONTEXT.update(cwd=repo), check("git push", "boundary")), True)
        expect("a push of HEAD from main", lambda: (CONTEXT.update(cwd=repo), check("git push -u origin HEAD", "boundary")), True)
        subprocess.run(["git", "-C", repo, "tag", "v0"], check=True)
        subprocess.run(["git", "-C", repo, "checkout", "-q", "-b", "topic"], check=True)
        with open(os.path.join(repo, "spec.patch"), "w") as f:
            f.write("--- a/docs/spec/a.md\n+++ b/docs/spec/a.md\n@@ -1 +1 @@\n-x\n+y\n")
        with open(os.path.join(repo, "src.patch"), "w") as f:
            f.write("--- a/src.py\n+++ b/src.py\n@@ -1 +1 @@\n-x\n+y\n")
        with open(os.path.join(repo, "m.graphql"), "w") as f:
            f.write("mutation { createRef(input: {}) { ref { name } } }\n")
        with open(os.path.join(repo, "q.graphql"), "w") as f:
            f.write("query { viewer { login } }\n")
        for command, want in (("git apply spec.patch", True), ("git apply src.patch", False), ("git apply missing.patch", True),
                              ("gh api graphql -F query=@m.graphql", True), ("gh api graphql -F query=@q.graphql", False),
                              ("gh api graphql -Fquery=@m.graphql", True)):
            expect(repr(command), lambda: (CONTEXT.update(cwd=repo), check(command, "boundary")), want)
        expect("a push of a tag by its name", lambda: (CONTEXT.update(cwd=repo), check("git push origin v0", "boundary")), True)
        expect("a push of HEAD from a feature branch", lambda: (CONTEXT.update(cwd=repo), check("git push -u origin HEAD", "boundary")), False)
        with open(os.path.join(repo, "docs", "spec", "a.md"), "a") as f:
            f.write("changed\n")
        expect("a push while the branch changes a spec", lambda: (CONTEXT.update(cwd=repo), check("git push -u origin topic", "boundary")), True)
        subprocess.run(["git", "-C", repo, "checkout", "-q", "--", "docs/spec/a.md"], check=True)
        for command, want in BOUNDARY_CASES:
            expect(repr(command), lambda: (CONTEXT.update(cwd=repo), check(command, "boundary")), want)
        expect("a bare push from a feature branch", lambda: (CONTEXT.update(cwd=repo), check("git push", "boundary")), False)
        # a stand-in `bd show` on PATH: a gate, a bead waiting for the owner, an ordinary task
        fake = os.path.join(tmp, "bin")
        os.makedirs(fake)
        with open(os.path.join(fake, "bd"), "w") as f:
            f.write("#!/usr/bin/env python3\nimport json, sys\nbead = sys.argv[4]\n"
                    "if bead == 'tr-err0':\n"
                    "    print(json.dumps({'error': 'database locked'})); sys.exit(0)\n"
                    "if bead == 'tr-err1':\n"
                    "    print(json.dumps([{'id': bead, 'issue_type': 'task', 'labels': []}])); sys.exit(1)\n"
                    "if bead == 'ok':\n"
                    "    print(json.dumps([{'id': 'tr-ok', 'issue_type': 'task', 'labels': []}])); sys.exit(0)\n"
                    "if bead not in ('tr-ok', 'tr-gate', 'tr-human'):\n"
                    "    print(json.dumps({'error': 'no issue found'})); sys.exit(1)\n"
                    "print(json.dumps([{'id': bead, 'issue_type': 'gate' if bead == 'tr-gate' else 'task',"
                    " 'labels': ['human'] if bead == 'tr-human' else []}]))\n")
        os.chmod(os.path.join(fake, "bd"), 0o755)
        saved_path = os.environ["PATH"]
        os.environ["PATH"] = fake + os.pathsep + saved_path
        for bead, want in (("tr-ok", False), ("tr-gate", True), ("tr-human", True), ("tr-missing", True),
                          ("tr-err0", True), ("tr-err1", True), ("ok", False)):
            expect(f"closing {bead}", lambda: check(f"bd close {bead} --reason done", "boundary"), want)
            expect(f"closing {bead} through update", lambda: check(f"bd update {bead} -s closed", "boundary"), want)
        os.environ["PATH"] = saved_path
        os.environ.pop("DO_NOT_TRACK")
        expect("bd without DO_NOT_TRACK", lambda: check("bd ready", "boundary"), True)
        expect("bd with DO_NOT_TRACK=1 in the command", lambda: check("DO_NOT_TRACK=1 bd ready", "boundary"), False)
        for rel, want in (("docs/spec/a.md", True), ("src.py", False), ("LICENSE", True), (".claude/boundary.json", True)):
            expect(f"an edit of {rel}", lambda: boundary_edit({"cwd": repo, "tool_input": {"file_path": rel}}), want)
        expect("an edit of ~/.claude/settings.json",
               lambda: boundary_edit({"cwd": repo, "tool_input": {"file_path": "~/.claude/settings.json"}}), True)
        os.makedirs(os.path.join(repo, ".claude"))
        with open(os.path.join(repo, ".claude", "boundary.json"), "w") as f:
            json.dump({"protected": ["src.py"]}, f)
        expect("an edit of a path listed in .claude/boundary.json",
               lambda: boundary_edit({"cwd": repo, "tool_input": {"file_path": "src.py"}}), True)
    if saved_env is None:
        os.environ.pop("DO_NOT_TRACK", None)
    else:
        os.environ["DO_NOT_TRACK"] = saved_env
    CONTEXT["cwd"] = saved_cwd
    return bad


def protected_watch_test():
    """The protected-path watch on a scratch repo and a scratch ~/.claude: ordinary edits and commits are not
    reported; an interpreter write, a new file, a committed change and a wildcard delete in docs/spec are, and so
    is a change to the guard's settings. After a change only reading and bd pass."""
    bad, saved_home = [], os.environ.get("HOME")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["HOME"] = os.path.join(tmp, "home")
        os.makedirs(os.path.join(tmp, "home", ".claude"))
        with open(os.path.join(tmp, "home", ".claude", "settings.json"), "w") as f:
            f.write("{}")
        repo = os.path.join(tmp, "repo")
        os.makedirs(os.path.join(repo, "docs", "spec"))
        subprocess.run(["git", "init", "-q", "-b", "topic", repo], check=True)
        for rel in ("docs/spec/a.md", "docs/spec/b.md", "src.py"):
            with open(os.path.join(repo, rel), "w") as f:
                f.write("x\n")
        commit = ["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam"]
        subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
        subprocess.run(commit + ["init"], check=True)
        origin = lambda ref: subprocess.run(["git", "-C", repo, "update-ref", "refs/remotes/origin/main", ref], check=True)
        origin("HEAD")  # the owner's base, as a fetch would leave it
        run = [0]

        def step(label, act, want):
            run[0] += 1
            event = {"session_id": f"self-test-b-{os.getpid()}-{run[0]}", "agent_id": "t", "cwd": repo,
                     "tool_name": "Bash", "tool_input": {"command": "true"}}
            watch(event, "boundary")
            act()
            found, _ = changes(event, "boundary")
            if bool(found) != want:
                bad.append(f"protected watch: {label} {'reported' if found else 'not reported'}, expected the opposite")
            os.remove(state_file(event, "boundary"))

        def write(rel, text="y\n"):
            with open(os.path.join(repo, rel), "a") as f:
                f.write(text)

        step("a read", lambda: None, False)
        step("an edit of src.py", lambda: write("src.py"), False)
        step("a commit of src.py", lambda: subprocess.run(commit + ["src"], check=True), False)
        step("an interpreter writing docs/spec/a.md", lambda: write("docs/spec/a.md"), True)
        subprocess.run(["git", "-C", repo, "checkout", "-q", "--", "."], check=True)
        step("a new file in docs/spec", lambda: write("docs/spec/new.md"), True)
        os.remove(os.path.join(repo, "docs", "spec", "new.md"))
        os.makedirs(os.path.join(repo, "docs", "spec", "sub"))
        step("a new file deeper in docs/spec", lambda: write("docs/spec/sub/deep.md"), True)
        os.remove(os.path.join(repo, "docs", "spec", "sub", "deep.md"))
        step("a commit that changes a spec", lambda: (write("docs/spec/b.md"), subprocess.run(commit + ["spec"], check=True)), True)
        step("a wildcard delete in docs", lambda: [os.remove(os.path.join(repo, "docs", "spec", n)) for n in ("a.md", "b.md")], True)
        subprocess.run(["git", "-C", repo, "checkout", "-q", "--", "."], check=True)
        def starts_flagged(label, want):
            event = {"session_id": f"self-test-b-{os.getpid()}-start-{label}", "agent_id": "t", "cwd": repo,
                     "tool_name": "Bash", "tool_input": {"command": "true"}}
            watch(event, "boundary")
            got = bool(load(event, "boundary")["changed"])
            if got != want:
                bad.append(f"protected watch: a run starting {label} {'flagged' if got else 'not flagged'}, expected the opposite")
            os.remove(state_file(event, "boundary"))

        branch = lambda *a: subprocess.run(["git", "-C", repo, "branch", "-q", *a], check=True)
        starts_flagged("on a branch with a committed spec change origin/main lacks", True)
        origin("HEAD")
        starts_flagged("on a clean tree that matches origin/main", False)
        write("docs/spec/a.md")
        starts_flagged("on an uncommitted spec change", True)
        subprocess.run(["git", "-C", repo, "checkout", "-q", "--", "."], check=True)
        # the round-2 abuse: a spec commit pushed to the branch's own upstream is still the branch's own change
        branch("pushed", "HEAD")
        branch("--set-upstream-to=pushed")
        step("a spec commit pushed to the branch's own upstream", lambda: (
            write("docs/spec/b.md"), subprocess.run(commit + ["spec 2"], check=True), branch("-f", "pushed", "HEAD")), True)
        starts_flagged("on a spec change that is only on the branch's own upstream", True)
        branch("--unset-upstream")
        origin("HEAD")
        # a pull, merge or rebase of the owner's base is not a write
        subprocess.run(["git", "-C", repo, "checkout", "-q", "-b", "owner"], check=True)
        write("docs/spec/a.md", "owner\n")
        subprocess.run(commit + ["owner spec"], check=True)
        subprocess.run(["git", "-C", repo, "checkout", "-q", "topic"], check=True)
        origin("owner")  # fetched: origin/main now has the owner's spec change
        step("a pull of the owner's spec change from origin/main", lambda: subprocess.run(
            ["git", "-C", repo, "merge", "-q", "--ff-only", "owner"], check=True), False)
        # a long-lived branch: the owner's base in boundary.json
        os.makedirs(os.path.join(repo, ".claude"), exist_ok=True)
        with open(os.path.join(repo, ".claude", "boundary.json"), "w") as f:
            json.dump({"base": "origin/longbase"}, f)
        write("docs/spec/b.md", "long branch\n")
        subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
        subprocess.run(commit + ["long branch spec"], check=True)
        subprocess.run(["git", "-C", repo, "update-ref", "refs/remotes/origin/longbase", "HEAD"], check=True)
        branch("origin/longbase", "HEAD~1")  # a local look-alike must not shadow the remote ref
        starts_flagged("on a long branch whose spec matches the base in boundary.json", False)
        for config, label in (({"base": "origin/no-such-ref"}, "a base that does not resolve"),
                              ({"base": "longbase"}, "a local branch as base")):
            with open(os.path.join(repo, ".claude", "boundary.json"), "w") as f:
                json.dump(config, f)
            subprocess.run(commit + [label], check=True)
            subprocess.run(["git", "-C", repo, "update-ref", "refs/remotes/origin/longbase", "HEAD"], check=True)
            subprocess.run(["git", "-C", repo, "update-ref", "refs/remotes/longbase", "HEAD"], check=True)  # resolves
            try:
                starts_flagged(f"with {label}", True)
                bad.append(f"protected watch: {label} did not fail closed")
            except (RuntimeError, ValueError):
                pass
        branch("-D", "origin/longbase")
        subprocess.run(["git", "-C", repo, "rm", "-q", "-r", ".claude"], check=True)
        subprocess.run(commit + ["no config"], check=True)
        subprocess.run(["git", "-C", repo, "update-ref", "-d", "refs/remotes/origin/main"], check=True)
        step("a spec commit in a repo with no trusted base", lambda: (
            write("docs/spec/a.md", "no base\n"), subprocess.run(commit + ["no base spec"], check=True)), True)
        origin("HEAD")
        # pushes are checked branch by branch against the base, HEAD or not
        def push_blocked(label, command, want):
            CONTEXT["cwd"] = repo
            try:
                check(command, "boundary")
                got = False
            except Blocked:
                got = True
            if got != want:
                bad.append(f"protected watch: {label} {'blocked' if got else 'allowed'}, expected the opposite")
        subprocess.run(["git", "-C", repo, "checkout", "-q", "-b", "side"], check=True)
        write("docs/spec/a.md", "side\n")
        subprocess.run(commit + ["side spec"], check=True)
        subprocess.run(["git", "-C", repo, "checkout", "-q", "topic"], check=True)
        push_blocked("a push of the clean current branch", "git push -u origin topic", False)
        push_blocked("a push of another branch that changes a spec", "git push origin side", True)
        push_blocked("a push of another branch by refspec", "git push origin side:refs/heads/x", True)
        subprocess.run(["git", "-C", repo, "checkout", "-q", "--orphan", "fresh"], check=True)
        subprocess.run(commit + ["orphan"], check=True)
        push_blocked("a push of an orphan branch", "git push -u origin fresh", True)
        subprocess.run(["git", "-C", repo, "checkout", "-q", "-f", "topic"], check=True)

        # through main(), as Claude Code calls it: a locked run still applies the rules; a SubagentStop with no
        # agent_id keeps the main session's state
        def hook(mode, payload):
            out = subprocess.run([sys.executable, os.path.abspath(__file__), mode], input=json.dumps(payload),
                                 capture_output=True, text=True, env=dict(os.environ, DO_NOT_TRACK="1"))
            return out.returncode

        sid = f"self-test-main-{os.getpid()}"
        call = lambda command: {"session_id": sid, "cwd": repo, "tool_name": "Bash", "tool_input": {"command": command}}
        hook("boundary", call("true"))
        with open(os.path.join(tmp, "home", ".claude", "settings.json"), "w") as f:
            f.write('{"hooks": {}}')  # a guard change: unlike a spec change, a new snapshot would not flag it again
        for mode, payload, want, label in (
                ("boundary-after", call("true"), 2, "the change reported"),
                ("boundary", call("git status"), 0, "git status while locked"),
                ("boundary", call("bd update tr-1 --priority 0"), 2, "a frozen bd field while locked"),
                ("boundary", call("cat ~/.ssh/id_rsa"), 2, "a key file while locked"),
                ("boundary", call("echo y > src.py"), 2, "a write while locked"),
                ("boundary-stop", {"session_id": sid, "cwd": repo, "hook_event_name": "SubagentStop", "stop_hook_active": True}, 0,
                 "a SubagentStop without agent_id"),
                ("boundary-stop", {"session_id": sid, "cwd": repo, "hook_event_name": "Stop",
                                   "last_assistant_message": "All done."}, 2, "a report without the line"),
                ("boundary", call("echo y > src.py"), 2, "a write after the stop asked (still locked)"),
                ("boundary", call("echo y > src.py"), 2, "a write after that SubagentStop (still locked)")):
            got = hook(mode, payload)
            if got != want:
                bad.append(f"protected watch through main(): {label} exit {got}, expected {want}")
        if hook("boundary-stop", {"session_id": sid, "cwd": repo, "hook_event_name": "Stop",
                                  "last_assistant_message": "PROTECTED CHANGED: ~guard: settings.json\n..."}) != 0:
            bad.append("protected watch through main(): a report that starts with the line was asked again")
        if hook("boundary", call("echo y > src.py")) != 2:
            bad.append("protected watch through main(): the lock did not survive the Stop")
        try:
            os.remove(state_file({"session_id": sid}, "boundary"))
        except FileNotFoundError:
            pass
        with open(os.path.join(tmp, "home", ".claude", "settings.json"), "w") as f:
            f.write("{}")
        step("an edit of ~/.claude/settings.json", lambda: open(os.path.join(tmp, "home", ".claude", "settings.json"), "w").write('{"hooks": {}}'), True)
    if saved_home is None:
        os.environ.pop("HOME", None)
    else:
        os.environ["HOME"] = saved_home
    for command, want in (("git status && git diff HEAD", True), ("bd update tr-1 --add-label needs-owner", True),
                          ("git diff --output=/tmp/x", False),
                          ("cat docs/spec/a.md | head", True), ("echo x > src.py", False), ("python3 fix.py", False),
                          ("git checkout -- docs/spec/a.md", False)):
        if after_change_ok(command) != want:
            bad.append(f"protected watch: after a change {command!r} {'allowed' if not want else 'blocked'}, expected the opposite")
    return bad


def watch_test():
    """The work-tree watch on a scratch repo: a tracked edit, an untracked file and a second tree are caught,
    an ignored file and a read are not. Returns a list of failures."""
    bad = []
    with tempfile.TemporaryDirectory() as tmp:
        repo, other = os.path.join(tmp, "repo"), os.path.join(tmp, "wt")
        for folder in (repo, other):
            os.makedirs(folder)
            subprocess.run(["git", "init", "-q", folder], check=True)
            with open(os.path.join(folder, ".gitignore"), "w") as f:
                f.write("build/\n")
            with open(os.path.join(folder, "a.txt"), "w") as f:
                f.write("a\n")
            subprocess.run(["git", "-C", folder, "add", "-A"], check=True)
            subprocess.run(["git", "-C", folder, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"],
                           check=True)
        event = {"session_id": f"self-test-{os.getpid()}", "agent_id": "t", "cwd": repo, "tool_name": "Bash",
                 "tool_input": {"command": f"python3 -c \"open('{other}/a.txt', 'a')\""}}

        def step(label, write, want):
            watch(event)
            if write:
                os.makedirs(os.path.dirname(write[0]), exist_ok=True)
                with open(write[0], "a") as f:
                    f.write(write[1])
            found, _ = changes(event)
            if bool(found) != want:
                bad.append(f"watch: {label} {'reported' if found else 'not reported'}, expected the opposite")

        step("a read", None, False)
        step("an ignored build file", (os.path.join(repo, "build", "out.o"), "x"), False)
        step("a tracked edit", (os.path.join(repo, "a.txt"), "b\n"), True)
        step("an untracked file", (os.path.join(repo, "new.txt"), "n"), True)
        step("an edit in a second tree named inside a string in the command", (os.path.join(other, "a.txt"), "c\n"), True)
        step("an edit to an already-changed file", (os.path.join(repo, "a.txt"), "d\n"), True)
        if not load(event)["changed"]:
            bad.append("watch: the stop check would find no change")
        locked = os.path.join(repo, "locked.txt")
        with open(locked, "w") as f:
            f.write("x")
        os.chmod(locked, 0)
        try:
            fingerprint(repo)
        except OSError:
            bad.append("watch: an unreadable untracked file makes the snapshot fail, so every call would be blocked")
        os.chmod(locked, 0o600)
        os.remove(state_file(event))
    return bad


def self_test():
    bad = []
    for mode, command, want in CASES:
        try:
            check(command, mode)
            got = False
        except Blocked:
            got = True
        if got != want:
            bad.append(f"{mode}: {command!r} {'blocked' if got else 'allowed'}, expected {'blocked' if want else 'allowed'}")
    bad += watch_test()
    bad += boundary_test()
    bad += protected_watch_test()
    for line in bad:
        print(f"- {line}")
    print(f"{'FAIL' if bad else 'PASS'}: {len(CASES)} command cases, the work-tree watch and {len(BOUNDARY_CASES)} "
          f"boundary cases and the protected-path watch, {len(bad)} failures")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
