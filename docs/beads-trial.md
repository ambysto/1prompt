# beads trial on a scratch repo

The open item of [design.md](design.md): install beads, check `--stealth --skip-agents`,
and find what a frozen-field hook has to cover. Run on Linux x86_64 in scratch repos with a
separate `HOME`; no real repo was touched.

## Install
- Release v1.3.1 (published 2026-09-30), `beads_1.3.1_linux_amd64.tar.gz`, sha256 checked
  against the release's `checksums.txt`: OK. One static binary, `bd` (154 MB, Dolt inside).
- Embedded mode is the default: the database lives in `.beads/embeddeddolt/`, no server process.

## What `bd init --stealth --skip-agents` writes
- In the repo: `.beads/` (config, metadata, database), `.beads.gate.lock` at the repo root, a
  `[beads] role = maintainer` entry in `.git/config`, and ignore lines in `.git/info/exclude`.
  No `AGENTS.md`, no `CLAUDE.md`, no git hooks, no `.claude/` settings.
- In `$HOME`: `~/.config/bd/config.yaml`, `~/.beads/machine-id`, `~/.beads/eventsData/`
  (the metrics queue) and `~/.dolt/config_global.json`.

## Metrics are on by default (new requirement)
- The default user config has `metrics.disabled: false` with a third-party metrics endpoint.
- Captured on a local listener (`BEADS_METRICS_ENDPOINT` pointed at 127.0.0.1): `bd init`
  sends one event: a hashed client id, app name and version, platform, the command name
  (`init-embedded`) and its duration. No repo name, path or issue content.
- `DO_NOT_TRACK=1` or `metrics.disabled: true` in `~/.config/bd/config.yaml`: zero requests.
- The first trial `init` ran before this was known, so one such event most likely left the
  machine (the queue file was flushed).
- **Requirement for the install:** set `metrics.disabled: true` and run `bd` with
  `DO_NOT_TRACK=1`; the installer checks both, like the other settings it checks.

## Graph features used by the design
- `--acceptance`, `--metadata '{"rating":…,"gate":…}'` and `--deps blocked-by:<id>` are stored
  and returned by `bd show --json`.
- `bd ready` lists only unblocked items; after `bd close A`, B (blocked by A) becomes ready.
- `bd update <id> --claim` sets `in_progress` and the assignee.
- Gates: `human`, `timer`, `gh:run`, `gh:pr`, `bead` (created from formula steps);
  `bd human list|respond|dismiss` for items labelled `human`.
- Two writers at once (2 × 15 `bd create` in parallel, embedded mode): 30 created, 0 failures,
  4.4 s in all.
- `--actor <name>` (or `BEADS_ACTOR`) is stored as `created_by` and in the event history.
  `bd history` (the Dolt log) shows "Author: beads" for every change; use `--events`.
- `bd --readonly …` refuses writes ("operation 'update' is not allowed in read-only mode")
  and still answers `ready`/`show`: a fit for reviewers.
- `bd sql` is not available in embedded mode, so raw SQL is not a way around the hook there.

## Frozen fields: what the hook has to cover
- `bd update <id> --title … --acceptance …` changes a frozen field with no warning.
- `bd history <id> --events` records each change with the actor and the old and new values
  (`New: {"acceptance_criteria":"nothing","title":"… (weakened)"}`), so a change that gets
  past the hook can still be found afterwards.
- The same shape as the builder and reviewer hooks: a PreToolUse rule blocks the usual forms
  (`update` with `--title`/`--acceptance`/`--design`/`--deps`, `edit`, `dep add|remove`,
  `delete`, `purge`, `rename`, `import`, `restore`, `batch`), and a check over
  `bd history --events` (or the events journal, `BD_EVENTS_JOURNAL=1`) finds any frozen field
  changed by an actor other than the owner. The command list comes from `bd help` of the
  pinned version and is re-checked at each beads upgrade.

## Not covered by this trial
- Server mode, Dolt remotes and sync (not planned; one machine).
- Windows and macOS binaries.
- Schema migration across a beads upgrade.
