# Autonomous run: one prompt takes a project to done

Status: the guards (`hooks/`, `scripts/github-boundary.py`) are built. The graph conventions, the loop and the
driver below are a candidate design, not built.

The goal is a prompt of this shape that works:

> Build the loop and the graph, use the tools that speed the work up (a code-search tool, for
> example), then carry the project through to done, at a level you are proud to share. Ask
> me only where a human must decide. Report once a day, at the start of the day.

## Problem

A typical Claude Code working loop assumes the owner is at the keyboard: a main session
that dispatches a builder and independent reviewers, and asks the owner at every fork. One
solo owner's time log (365 lines over about a week) shows where the owner's time goes:

| kind / trigger | count | could the loop avoid it? |
|---|---|---|
| decision / agent-asked | 99 | partly: some are human-only, others the agent could decide and log |
| direction / owner | 76 | no: the owner's own steering |
| approval / agent-asked | 42 | mostly: written standing approvals |
| merge (both triggers) | 46 | yes, by the auto-merge decision below |
| status / owner | 35 | yes: a morning report and one state file |
| unblock (both) | 16 | yes: a parked node must not stop the run |
| incident / owner | 11 | the signal that matters; needs gates, not fewer questions |

There are also gaps in the structure:
- **No machine-readable plan.** Plans are prose. No session can tell which steps are ready,
  blocked or done without reading them all over again.
- **No single source of state.** A hand-written handoff file goes stale. In one case a second
  session ran a paid benchmark pilot while the first session's handoff still said "no paid
  run". The first session then told the owner that the pilot was waiting for them.
- **A stop is global.** After two FAIL rounds the loop stops and asks, even when other
  steps are ready.
- **A session ends.** Nothing wakes the work up again, and nothing caps a day's spend.

## Outcome

An opt-in **autonomous mode** for any repo: a graph convention, the loop that drives it, the
human-decision boundary, a daily cost cap and the morning report. It is proved on one pilot
project before it is offered to others.

## Decisions

1. **Auto-merge on green.** In autonomous mode the main session merges its own PR into
   main when the project's whole Verify passes on that branch and the reviewer
   (MEDIUM change) or hard-reviewer (HARD change) returns PASS. Tags, releases, publishing
   and anything users receive still go to the owner. Every merge is listed in the next
   report. Outside autonomous mode, "merges only when asked" stays.
2. **Daily cap: $50 of API spend.** When the cap is reached, the run stops and the
   morning report says so. The cap is never raised by the agent.
3. **Pilot: chosen after a code-search tool being benchmarked is fully done.** Until then
   this stays a design. It does not change how sessions work today.
4. **The morning report is a shared document**, updated each morning. The owner answers the
   pending decisions with comments in it.
5. **The loop and the tool use are not decided.** The graph, the loop and the tool use below
   are a candidate design. They are decided after the code-search benchmark is finished,
   because its result can change them.
6. **Graph store: beads** (option (a) of prior-art candidate 17). The graph lives in beads
   (`bd`), wrapped with hooks for what beads does not enforce: frozen node definitions and
   the human-only boundary. Installed with `--stealth --skip-agents` (no writes to the
   project's agent files). The owner's own tracker stays the owner-facing one; beads holds
   the agents' working graph.
7. **The human-only boundary is enforced by PreToolUse hooks**, not by prompts or auto mode
   alone. Auto mode covers only what is outside that list. Built in two steps: hooks for the
   builder and reviewers, then `hooks/autonomous-settings.json`, loaded by the autonomous
   run. Protected paths get an after-the-fact watch as well as the command check, because
   three review rounds kept finding command forms that write.

## Design (candidate; the graph store and the hooks are decided)

The numbers in brackets point to the candidates in [prior-art.md](prior-art.md). The graph
store is decided (decision 6). The loop, the driver and the tool use are not.

### Graph (beads, decision 6)
- **One bead per task** [1, 17]. Our fields go in `metadata`: `rating` (EASY/MEDIUM/HARD),
  `gate`, `evidence` (commit, gate output path, review verdict) and `cost`. beads
  provides the deps (`blocks`, `parent-child`, `discovered-from`), the
  `acceptance_criteria`, the claim and the events trail.
- **Status** maps onto beads: `ready` = `bd ready`; `running` = claimed; `review`,
  `parked` and `needs-owner` are labels; `done` = closed with evidence.
- **Frozen definitions** [2]. An agent may change only status, evidence, cost and the
  claim. A change to a bead's title, acceptance criteria, gate or deps is a spec change
  and goes to the owner. beads does not enforce this, so a PreToolUse hook on `bd` and a
  check against the events trail do.
- **Typed gates** [18]: `human` (closed only by the owner), `gate-cmd` (a project script
  that runs the test suites), `pr-merged`, `ci`. A `human` gate blocks only its own branch
  of the graph, so one open question does not stop the run.
- **Gates that cannot be gamed** [6]. A bead whose only proof is the agent's word is
  never closed. It goes to `needs-owner` with a proposed gate.
- **Optional beads** [26] (priority 4 or label `optional`) do not block "done". They are
  listed in the final report.
- **Leases** [21]. A claim carries a heartbeat. The next run resets a bead whose lease
  ran out to `ready`. No watchdog tree beyond that [22].
- **The graph is the only state** [1, 14]. The handoff file and the report are generated
  from it, never edited by hand. Every run appends to the events trail, and the driver
  keeps no state of its own, so a crash is recovered by reading the graph.
- **Attribution** [22]: each event records the session id and the model, so a model-switch
  test can compare them.
- **Metrics off**: `metrics.disabled: true` in `~/.config/bd/config.yaml` and `DO_NOT_TRACK=1`
  for every `bd` call (on by default; see [beads-trial.md](beads-trial.md)).
- **Install**: `bd init --stealth --skip-agents` (no writes to `AGENTS.md`, `CLAUDE.md`
  or hooks), from a release checked against its checksum. One designated clone runs schema
  migrations.

### Loop (one run)
1. **Start.** Take the project's run lock; if it is held, exit without writing. Check the
   day's spend against the cap. Reset expired leases.
2. **Smoke gate** [3]. Run the fast core gate on main. If it fails, the next bead is the
   repair, before anything else.
3. **Pick.** Take the ready beads. By default one session works them in order
   [7]. Run in parallel only when beads are really independent, each in its own
   worktree, never more than the test machine allows.
4. **Contract** [4]. For a MEDIUM or HARD bead, the reviewer approves its acceptance
   criteria and gate before the build starts.
5. **Build and review.** The existing chain: rate, then builder or main session, then
   reviewer or hard-reviewer, then gate. The reviewer's prompt carries worked examples from
   incidents that an earlier PASS missed [5].
6. **Merge queue** [19, 12]. One merger, serial: rebase on main, run the whole Verify on
   the rebased branch, merge (decision 1). The merge is done by a script, not by an agent
   pushing to main, which also avoids auto mode's default block on pushes to main.
7. **Circuit breaker** [24], per bead and per run. It opens after 3 runs with no graph
   change (no bead closed, no evidence, no commit), the same error 5 times, or 2
   permission denials. An open bead is `parked` with its reason. An open run stops until
   the next scheduled run, which tries one bead first (half-open) before going on.
8. **Stop points.** FAIL twice, or a gate that cannot run, parks the bead. A human-only
   question makes it `needs-owner` with a recommendation and a `human` gate. In both cases
   the loop goes on with the other ready beads.
9. **Done** [11, 23]. A command Stop hook of our own decides, not agent text and not
   `/goal` (its judge reads only the transcript). "Done" means every non-optional bead is
   closed with evidence, the gates have run again and passed, and a whole-project
   hard-review has passed.

### Human-only boundary (the agent must ask)
The list: spending above the cap or any new paid service; credentials, keys, tokens and
signing; releases, tags, publishing, making a repo public; anything that cannot be undone
or could lose user data outside a test machine; changing scope, a spec, a threshold, a
public API, a data format, a protected list or a bead's definition; licences.
- **Enforced by hooks, not prompts** [10, 12]. Deterministic PreToolUse hooks block
  the list (tag, release, publish, key use, `bd` edits to frozen fields). Auto mode
  covers the rest; it misses about 17 % of overeager actions, so it is never the only
  guard on this list.
- Everything else is the agent's call, recorded in the bead with a one-line reason, so the
  owner can read it in the report and reverse it.

### Driver and waking up
- **Local, headless** [13]: a systemd user timer starts `claude -p` on the graph every
  few hours, plus one fixed run before the owner's morning to write the report. Cloud
  routines cannot reach a local test machine. `/loop` dies with its session and expires
  after 7 days. Desktop scheduled tasks do not exist on Linux.
- Headless means a question ends the run, so the graph records why each run ended.
- **Resume by explicit session id only** [25], never `--continue`: several sessions can
  run on one machine, and `--continue` can pick up another one's conversation.
- **Usage limit = pause** [27]. It is recorded and the next scheduled run carries on. It
  is told apart from a timeout.
- Context limits: the run ends, and the next one starts fresh from the graph.
- Agent teams are not the driver [15]. They are interactive and experimental; maybe
  later for parallel review inside one bead.

### Morning report (shared document, decision 4)
Done since the last report (with gate evidence) · merged PRs · decisions waiting for the
owner, each with a recommendation · parked beads and why (circuit breaker included) ·
optional beads left · spend vs. cap · risks · owner interventions since the last report
(from the time log). A question left unanswered goes to the top of the next report, in
bold, at most twice; after that the bead stays parked and its dependants are listed [20].
A check that did not run is written "not run: <reason>", never as a pass.

### Cost and model changes
- **Expect a harness to cost many times a solo run** [9] (about 20x in Anthropic's
  example). $50 a day is about one hard day on one project, not a fleet.
- **Each part of the harness is an assumption** [8]. At each model switch, re-run the
  pilot's gates with and without each part (contract, smoke gate, circuit breaker), and
  drop the parts that no longer change the outcome.

### Tools
A code-search tool joins the loop only if its agent benchmark shows lower cost at equal
quality. Then: search to locate, grep or the compiler to prove ("all callers of X" is
never answered by ranked search), and one index per worktree, refreshed between beads.
The project's own test-machine scripts and gate declarations stay as they are.

### Not adopted
gastown [22]: too many standing roles for one owner and one test machine. We borrow its
merge queue, stale re-escalation, leases and attribution. ralph's exit by reading prose [23].

## Success criteria (on the pilot)

| # | Criterion | How it is checked |
|---|---|---|
| 1 | Every non-optional bead is closed with evidence, plus a whole-project hard-review PASS | graph + review log |
| 2 | Owner interventions per finished node at most half of the pilot's own baseline period, with zero `status` and `unblock` | time-log report for the pilot project |
| 3 | No `incident`/`owner` caused by the run | time log |
| 4 | Never over the daily cap | spend ledger |
| 5 | Every report claim traceable to evidence; no "pass" without a run | hard-reviewer reads three reports against the graph |
| 6 | Two sessions on one graph never both write it | a lock test |
| 7 | No agent changes a frozen bead field, and no human-only action passes the hooks | events trail + a hook test that tries each item on the list |

## Out of scope
- Changing the owner's daily decisions into agent decisions where the boundary above says human.
- Platform-specific build work on other machines.
- Rolling out to every repo before the pilot passes.

## Open
- Where the spend comes from for each run (the session cost the CLI reports; API usage for headless runs).
- The driver: the candidate is a local systemd timer running `claude -p` (see Design).
  Decided with the loop (decision 5).
- The beads trial on a scratch repo is done: [beads-trial.md](beads-trial.md). It works for
  the design (`ready`, deps, claims, metadata, two writers, `--readonly`, per-field event
  history). New requirement: beads sends usage metrics by default; the install turns them off.
