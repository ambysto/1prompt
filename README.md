# 1prompt

An opt-in autonomous mode for Claude Code: hooks, a server-side boundary script and a design so that one prompt
can carry a project to done, asking the human only where a human must decide (tags, releases, publishing,
signing, force push, protected paths). Standard library Python only.

## Status

Design and guards. The loop, the driver and the morning report are not built yet.

- Built: the hooks that enforce the human-only list, and the script that sets the matching rules on GitHub.
- Designed, not built: the task graph (on beads (`bd`)), the loop, the
  headless driver, the daily cost cap and the morning report. See [docs/design.md](docs/design.md).

## What is in the repo

| Path | What |
|---|---|
| `hooks/agent-guard.py` | The hook script. Modes `build` (a builder subagent), `review` / `review-after` / `review-stop` (reviewer subagents) and `boundary` / `boundary-after` / `boundary-stop` (an autonomous run). `--self-test` runs its cases. |
| `hooks/autonomous-settings.json` | Settings that wire the `boundary*` modes into every tool call and every stop of an autonomous run. |
| `scripts/github-boundary.py` | Writes two GitHub rulesets (default branch: pull request and code-owner review only, no force push or deletion; tags: no creation, update or deletion) and a CODEOWNERS file, from a config you give it. |
| `examples/github-boundary.example.json` | The config format for `github-boundary.py`. |
| `scripts/check.py` | Runs both self-tests, checks the settings file and runs the scrub gate. |
| `docs/design.md` | The design: problem, decisions, graph, loop, boundary, success criteria, open questions. |
| `docs/prior-art.md` | What Anthropic's and others' long-running agent harnesses do, and what it means for the design. |
| `docs/beads-trial.md` | A trial of beads as the graph store. |

## Install

Requires Python 3 and Claude Code.

```sh
mkdir -p ~/.claude/hooks/1prompt
cp hooks/agent-guard.py ~/.claude/hooks/1prompt/
```

For an autonomous run, load the boundary hooks for that run only:

```sh
claude --settings hooks/autonomous-settings.json
```

The file is not loaded in interactive sessions, where you are present, so they behave as before. The hooks apply
to the main session and to every subagent of the run.

## Build and review modes in a subagent

A builder or reviewer subagent can carry the stricter hooks in its own frontmatter. For a builder
(`.claude/agents/builder.md`):

```yaml
---
name: builder
hooks:
  PreToolUse:
    - matcher: "Bash|PowerShell"
      hooks:
        - type: command
          command: 'python3 "$HOME/.claude/hooks/1prompt/agent-guard.py" build'
---
```

`build` blocks commit, push, merge, branch switches, tags, PRs and other GitHub writes. For a reviewer, use `review`
on PreToolUse, `review-after` on PostToolUse and `review-stop` on Stop; they also snapshot the work tree and make the
reviewer report any change to it. The module docstring in `hooks/agent-guard.py` lists every mode.

## Limits

- The hooks are a **mistake guard, not a security boundary**. Commands are checked by reading the command line,
  which catches an agent that slips, not one that tries to get around it. A command the hook cannot parse is
  blocked, but if `python3` is missing the shell exits 127, which Claude Code does not treat as a block.
- The **server-side rulesets are the firm boundary**: an agent that works as a separate account without admin
  rights cannot change them. Set them with `scripts/github-boundary.py` (see its docstring; `--apply` writes as the
  owner account only). GitHub refuses rulesets on private repos without a paid plan; the script reports that.
- Work-tree watches compare git state before and after, so files git ignores (build output) are not covered.

## Check

```sh
python3 scripts/check.py
```

It exits non-zero and prints every failure. The scrub gate reads an optional private term list at
`~/.config/1prompt/scrub-terms.txt` (one regex per line, `#` comments, `allow:` lines exempt a literal string) and
fails on any match in the repo's file names, files, commit messages or commit identities; without the list it is skipped.

## Licence

MIT, see [LICENSE](LICENSE).
