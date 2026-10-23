# 1prompt: an opt-in autonomous mode for Claude Code (GitHub ambysto/1prompt, public, MIT)

## What it does
Hooks, a server-side boundary script and a design so that one prompt can carry a project to done, asking the owner only where a human must decide. Public: anyone may read and use it. Standard library Python only.
It must never let an agent cross the human-only list (tags, releases, publishing, signing, force push, protected paths) without the owner, and must never leak private data (names, accounts, machine paths, private repo names, tracker ids) into this repo.
Cost of a bug: a guard that silently lets a human-only action through, or a leak that cannot be taken back once pushed to a public repo.

## Verify
- `python3 hooks/agent-guard.py --self-test` (command cases and a work-tree watch on a scratch repo)
- `python3 scripts/github-boundary.py --self-test` (payloads and comparison, no network)
- `python3 scripts/check.py`: both self-tests, `hooks/autonomous-settings.json` runs the four boundary hooks, and the scrub gate (no private term from the private list outside this repo, see Rules) at zero hits
- Owner-only: changing the repo's visibility, `github-boundary.py --apply` on real repos, anything run as the bot account.

## HARD: the main session does it directly, then hard-reviewer
- `hooks/agent-guard.py`: what is blocked, the protected-path watch, the trusted base.
- `scripts/github-boundary.py`: the rulesets and CODEOWNERS it writes.
- The scrub gate and anything that is about to be pushed while the repo is public.

## MEDIUM: builder builds, reviewer reviews
- `scripts/check.py`, `hooks/autonomous-settings.json`, examples, the design docs in `docs/`.

## EASY: transcriber or builder, no review
- README wording, typos, examples that do not change a rule.

## Rules
- Nothing private: no person's names or handles, no home-directory paths, no private repo or org names, no ticket ids, no hosts, keys or tokens. A maintainer keeps a private term list outside the repo (`~/.config/1prompt/scrub-terms.txt`, one regex per line, `allow:` lines exempt); `check.py` reads it when present and fails on any hit in file names, tracked files, commit messages or commit identities.
- Never silently change the human-only list, the protected paths or the hook install path (`$HOME/.claude/hooks/1prompt/`). Flag it and ask.
- This repo is the single source of `agent-guard.py`; downstream setups install a pinned copy and check its hash.
- Git: English only; never a push to `main` after the first commit; commit subjects and PR bodies are public, so they carry no ticket id; do not merge without being asked. Maintainer-specific rules (account, author, tracker) live in `CLAUDE.local.md`, which is gitignored.

## Session state
- Work in progress lives in `HANDOFF.md` (gitignored). If it exists at session start, read it before anything else. Update it with `/handoff` before `/clear`; delete it once the goal is finished and recorded.
- Durable facts go in this file or in memory, tasks in the tracker. `HANDOFF.md` holds only where the current goal stands.
