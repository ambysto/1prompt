# Prior art for the autonomous run

Read 2026-10-22. Ideas only, nothing adopted. The loop and tool use stay undecided until the
code-search benchmark finishes ([design.md](design.md), decision 5). The numbered candidates are
referred to from the design. "Part 1" is Anthropic's own harnesses.

## Sources read
1. `anthropics/claude-quickstarts/autonomous-coding` (last change 2025-11-26). The README
   says "Internal Anthropic use", but the repo LICENSE is MIT. Ideas only, so either way
   nothing is copied.
2. "Effective harnesses for long-running agents", Anthropic Engineering, 2025-11-26.
3. "Harness design for long-running application development", 2026-03-24.
4. "Building a C compiler with a team of parallel Claudes", 2026-02-05.

## What they do
- **Two prompts, one harness.** An initializer turns the spec into `feature_list.json`
  (200+ end-to-end tests, all `passes: false`), `init.sh`, a first commit and
  `claude-progress.txt`. Every later session uses the same coding prompt with a fresh context.
  The driver is a plain Python `while` loop around the Agent SDK.
- **The list is append-only for the agent.** Only `passes` may change: "IT IS
  CATASTROPHIC TO REMOVE OR EDIT FEATURES". They chose JSON because a model is less likely
  to rewrite it than Markdown.
- **Startup routine.** Read pwd, spec, list, progress file and git log. Then **re-run 1-2
  core passing tests before new work**: the previous session may have broken something.
- **One feature per session, then leave a clean state.** Commit, update the progress
  file, leave nothing broken.
- **Failure modes they name.** Trying to finish everything in one go. Declaring the project
  done because some progress is there. Marking a feature done after a unit test or
  `curl` without testing it end to end. Messy handoffs.
- **Planner / generator / evaluator** (2026-03). Self-grading "reliably skews positive".
  "Out of the box, Claude is a poor QA agent". The evaluator was calibrated with few-shot
  scored examples and by reading logs where its verdict differed from a human's.
  **Sprint contracts:** before building, the generator proposes what "done" means and how
  to test it, and the evaluator negotiates it (27 criteria in one sprint). Each criterion has a hard
  threshold; failing one fails the sprint.
- **Scaffolding shrinks as models improve.** With Opus 4.5, context resets were no longer
  needed (compaction was enough). With Opus 4.6, sprints were dropped and the evaluator
  became one pass at the end. "Every component in a harness encodes an assumption about
  what the model can't do on its own."
- **Cost.** Retro game maker: solo 20 min / $9 vs. harness 6 h / $200, with visibly better
  quality. DAW: 3 h 50 min / $125. C compiler: 16 agents, about 2,000 sessions, 2 weeks,
  just under $20,000.
- **Parallel with no orchestrator** (C compiler). `while true` restarts each agent. An agent
  claims a task by committing a lock file to `current_tasks/`; git rejects a second claim.
  "The task verifier is nearly perfect", or the agent "will solve the wrong problem".
  CI was added late to stop regressions. Known-good oracle (GCC) plus delta debugging to split one big task into
  independent ones.
- **Context hygiene.** Tools print a few lines and log the rest. `ERROR` stays on one line
  so grep finds it. Statistics are computed ahead of time. A `--fast` mode runs a fixed 1 % or 10 %
  sample of the tests. There is a running file of failed approaches.
- **Security.** Bash allowlist as a PreToolUse hook, an OS sandbox, file access limited to
  the project.

## What it means for the design (candidates, not decisions)
1. **Confirms:** one state file in JSON, git as the record, a progress file built from
   it, a separate reviewer, the "Dead ends" section (≈ their failed-approaches file).
2. **Add: the node definition is frozen for the agent.** In `graph.json` an agent may
   change only `status`, `evidence`, `cost` and `owner`. A change to a node's scope or gate
   is a spec change, which is human-only. It can be checked mechanically, like `check.py`.
3. **Add: a smoke gate at every start.** Before picking a node, re-run a fast core gate on
   main. If it fails, the next node is the repair.
4. **Add: a contract before the build.** For MEDIUM and HARD nodes, the reviewer approves
   the node's "done" criteria before the builder starts, not only the diff afterwards.
   This targets the self-grading bias at its source.
5. **Add: calibrate the reviewer from our own incident logs.** Wherever the owner later found a bug that
   a PASS missed (an `incident` the owner noticed first, in the time log), add that case to the reviewer
   prompt as a worked example.
6. **Add: a gate that cannot be gamed.** Their main lesson is that agents optimise what
   the verifier measures. Real-machine proofs and mutation checks are the strongest gates. A node
   without such a gate should not be marked `done`. Mark it `needs-owner` instead.
7. **Simplify: maybe no orchestrator.** With Opus 5.5 and compaction, one session per
   project plus file locks through git may be enough. Multi-agent only where the work is
   really independent.
8. **Tie to the model-switch test.** Each part of the harness is an assumption. When the
   model changes, re-test which parts still carry weight, and remove the ones that do not.
9. **Cost expectation.** A harness costs many times more than a solo run (20x in their
   game example). $50/day is about one hard day of one project, not a fleet.
10. **Hooks, not prompts.** Auto-merge plus long unattended runs makes limits that live only
   in prompts a blocker: PreToolUse hooks
   should enforce the human-only boundary (no tag, release, publish or key use).

## Part 2: the rest of Anthropic (read 2026-10-22)

### Posts
- **Auto mode** (2026-03-25). Read-only tools and edits inside the project run without a check.
  Everything else goes through a classifier that sees user messages and tool calls (not the
  agent's own text). By default it blocks pushes to main, deploys, editing its own permissions, and
  exfiltration. Honest numbers: 0.4 % false positives on real traffic, but **17 % misses on
  real overeager actions** (n=52). In headless `-p`, 3 denials in a row (or 20 in total)
  **end the run**. "Not a drop-in replacement for careful human review on high-stakes
  infrastructure."
- **Managed Agents** (2026-04-08). Three swappable parts: an append-only **session log**
  outside the context, a **stateless harness** that resumes with `wake(sessionId)` by
  replaying the log, and disposable sandboxes. Credentials never enter the sandbox (vault +
  proxy). Compaction cannot be undone, so the full history is kept in the log.
- **Infrastructure noise in evals** (2026-02-05). Resource limits alone move agentic
  coding scores by up to 6 points. Naive confidence intervals already span 1-2 points.
  Differences under about 3 points are suspect unless the setup is matched and documented. Run on
  several days.
- **Demystifying evals** (2026-01-09). Grade the outcome, not the path. Prefer code
  graders, use model graders where needed and calibrate them against humans. pass@k (at least one
  success) vs pass^k (every trial succeeds: 75 % per trial gives 42 % over 3). Start with
  20-50 tasks from real failures, each with a reference solution. 0 % over many trials means a broken
  task.

### Claude Code features (docs, read 2026-10-22)
| Feature | What it gives | Limits that matter here |
|---|---|---|
| `/goal <condition>` | keeps taking turns until the condition holds; works with `claude -p` | it is a Stop hook judged by the **small fast model, from the transcript only**: it runs nothing and reads no files, so it believes what the agent printed |
| Stop hook (command) | the same loop, judged by **our own script** | we write it |
| `/loop`, Cron tools | intervals inside an open session | session-scoped, **7-day expiry**, a self-paced loop is not restored on resume, no catch-up for missed runs |
| Cloud scheduled tasks (routines) | run without our machine, on a schedule, by API call or on GitHub events | a fresh clone in the cloud: **no access to a local test machine or local files**, so no gates that run there |
| Desktop scheduled tasks | local and durable | Desktop app only (not the Linux CLI) |
| Agent teams | lead + teammates, shared task list with deps and file locks, `TaskCompleted` hook can refuse completion (exit 2) | experimental, **interactive only (not `-p`)**, no resume, one team per session, task status can lag |

### What it adds for the design (candidates)
11. **The completion judge must be ours.** `/goal`'s judge reads only the transcript, so
    "tests pass" printed by the agent counts as done. Use a command Stop hook that reads
    `graph.json` and runs the gates itself.
12. **Hard boundary in hooks, the rest in auto mode.** Auto mode misses 17 % of overeager
    actions, so the human-only list (tag, release, publish, keys, spend) needs
    deterministic PreToolUse hooks. Auto mode blocks pushes to main by default, which
    conflicts with decision 1 (auto-merge). It needs an explicit rule, or a merge step run by our
    script rather than by the agent.
13. **Durable driver is local, not cloud.** The gates run on a local test machine, so cloud
    tasks cannot drive the loop. `/loop` expires after 7 days and dies with the session. A
    systemd timer that starts `claude -p` (headless) on the graph is the robust option.
    Headless means an escalation ends the run, so the graph must record why it ended.
14. **Graph + event log, stateless driver** (Managed Agents). Every run appends events.
    A crash means replaying from the log. The driver keeps no state of its own. Credentials stay
    out of agent reach (already the rule for package-feed tokens on a test machine).
15. **Agent teams are not the graph.** Not durable, not headless. They might be useful
    later for parallel review inside one node.
16. **For a code-search tool being benchmarked (not this design's call):** a 4 % cost gap over 2 reps
    is inside the noise Anthropic reports. Reading it needs more reps or days, matched resources,
    and outcome grading (pass^k), before the "tool in the loop" decision of decision 5.

## Still open (Anthropic)
- "How we built our multi-agent research system" (2025-06-13): orchestrator-worker,
  lower priority.

## Part 3: beads and gastown (read 2026-10-22)

Both are MIT, active (commits today), by the same group. Read: READMEs, beads
architecture, `gate`, `human` and `ready` references, gastown overview, glossary,
propulsion principle, escalation and self-managed completion designs.

### beads (`bd`, ★28k, v1.3.x with weekly RCs)
- **What it is:** an issue graph for agents. Storage is **Dolt** (version-controlled SQL,
  embedded single writer, or a server for many writers). Sync goes through `refs/dolt/data` on the
  git remote. Hash IDs (`bd-a1b2`) never collide across branches.
- **Fits our graph well:** `bd ready` (open, no open blockers), `bd ready --claim`
  (atomic claim), claim leases with heartbeats, typed edges (`blocks`, `parent-child`,
  `discovered-from`), `acceptance_criteria` per issue, an events audit trail, `metadata`
  JSON for our own fields (rating, gate, evidence, cost).
- **Gates as nodes:** `human` (closed only by a person), `timer`, `gh:run` (CI success),
  `gh:pr` (merged), `bead` (cross-repo). `bd gate check --escalate` closes the ones that
  resolved and escalates the ones that failed.
- **Human queue:** issues labelled `human`, `bd human list / respond / dismiss / stats`.
  This is close to our "needs-owner" list.
- **Costs and risks:** a Go binary plus Dolt to install and keep current, with schema
  migrations on upgrade ("exactly one designated clone runs `bd migrate schema`").
  The API changes before 1.0. `bd init` **writes `AGENTS.md` and installs Claude Code hooks by
  default** (needs `--skip-agents` / `--stealth`). It enforces nothing on content: an agent
  can edit any field, so a frozen node definition (candidate 2) would still need our hook.
  It also adds a second store of truth next to the owner's tracker.

### gastown (`gt`, ★18k)
- **What it is:** a whole town of agents on beads + tmux. Mayor (coordinator), Deacon
  (watchdog daemon), Boot (checks the Deacon every 5 min), per-repo Witness (watches workers),
  Refinery (merge queue), Polecats (ephemeral workers in worktrees), Crew (long-lived).
- **"If you find something on your hook, YOU RUN IT."** The assignment is the permission
  to start. No asking.
- **Escalation:** severity-routed (MEDIUM → bead + mail, HIGH → + email, CRITICAL → +
  SMS), tracked as beads, **re-escalated when stale** (default 4 h, at most 2 times).
- **Its own design docs show the cost of the many roles:** the single-threaded Witness
  became a bottleneck, and "zombie polecats accumulate in `done` state". Built for many parallel
  agents; we run one test machine and one lock.
- **Attribution:** every commit, issue and event carries the agent's identity, so models can be
  compared by completion time and revisions.

### What it adds for the design (candidates)
17. **Two options for the graph, decided with decision 5:**
    (a) beads as the store, wrapped with our hook for frozen definitions and the human-only boundary;
    (b) a small `graph.json` + git lock files (the Anthropic pattern). (a) brings ready/claim/lease/gates/audit for free,
    at the cost of Dolt, upgrade churn, invasive defaults and a second tracker next to the owner's.
    (b) is a few hundred lines that we own. Test (a) on a scratch repo before choosing, with
    `bd init --stealth --skip-agents`.
18. **Typed gates are a good model even without beads:** a node's gate is `human`,
    `gate-cmd` (our script), `pr-merged` or `ci`. A `human` gate is how a needs-owner question
    blocks just one branch of the graph.
19. **Merge queue as its own step** (Refinery). One merger, serial, with whole Verify on the
    rebased branch. This answers the auto-mode "push to main" conflict (candidate 12).
20. **Stale re-escalation.** An unanswered owner question goes up again in the next report
    (bold, top), at most twice. After that the node stays parked and its dependants are listed.
21. **Watchdog:** a heartbeat per running node. A node whose lease expired is reset to `ready`
    by the next run. That is enough; no Deacon/Witness tree.
22. **Do not adopt gastown.** Too many standing roles for one owner and one test machine. Borrow
    the merge queue, stale re-escalation, leases and attribution.

## Part 4: ralph-claude-code (read 2026-10-22)

`frankbria/ralph-claude-code` (MIT, ★9.7k). A bash loop around Claude Code (Geoffrey
Huntley's "Ralph" technique). It reads `.ralph/PROMPT.md` and `fix_plan.md` (checkboxes), runs
Claude, logs, decides whether to exit, and repeats. Read: README and
`lib/circuit_breaker.sh`.

- **Exit by reading prose.** Exit needs `completion_indicators >= 2` (phrases such as "all tasks
  complete") **and** the agent's own `EXIT_SIGNAL: true`. The changelog is a list of
  wrong exits: exiting after exactly 5 loops in JSON mode, `STATUS: COMPLETE` overriding
  `EXIT_SIGNAL: false`, a timeout (exit 124) read as the 5-hour usage limit. This is the
  self-grading problem Anthropic names, in practice.
- **Circuit breaker** (Nygard's pattern, states CLOSED / HALF_OPEN / OPEN). It opens after
  3 loops with no progress, 5 loops with the same error, output down more than 70 %, or
  2 loops with permission denials. A 30-minute cooldown, then a half-open test run.
- **Limits:** 100 calls an hour (configurable). On the usage limit it waits rather than exits when unattended.
  The session expires after 24 h.
- **Session hijack bug:** `--continue` picked up another session's conversation, so they moved to
  `--resume <session_id>`. That matters for us: several sessions run on one machine.
- **Optional work:** items under an `## Optional` heading do not block completion.
- **Sandboxes:** Docker or E2B, with a cost cap (`--sandbox-max-cost`).

### What it adds for the design (candidates)
23. **Never decide "done" from text.** Done means every non-optional bead is closed with
    evidence and the gates pass (candidate 11). Agent prose and status blocks are only hints.
24. **Circuit breaker per node and per run.** Open on: no graph change (no bead closed,
    no evidence, no commit) for 3 runs, the same error 5 times, or 2 permission denials.
    An open node is parked and listed in the report. An open run stops until the next scheduled run.
    Ralph's thresholds are a reasonable starting point.
25. **Resume by explicit session id only.** Never `--continue` in the driver, so it
    cannot pick up another session's conversation (on a shared machine that is a real risk).
26. **Optional nodes:** beads priority 4 (backlog), or an `optional` label, do not block
    "done". They are listed in the final report.
27. **Usage limit is a pause, not a failure.** Record it, wait for the reset or the next
    scheduled run. Distinguish it from a timeout (Ralph's #183).

## Deep-dive status
Done: Anthropic (parts 1-2), beads/gastown (part 3), ralph (part 4). Graph decided:
beads (design decision 6). Loop and tool use wait for the code-search benchmark (decision 5).
