#!/usr/bin/env python3
"""The server-side half of the human-only boundary: rulesets and CODEOWNERS on GitHub, which an agent on
this machine cannot change because it works as a separate bot account without admin rights.

  python scripts/github-boundary.py --config PATH              dry run: what would be created or updated, per repo
  python scripts/github-boundary.py --config PATH --apply      create or update the two rulesets on each repo
  python scripts/github-boundary.py --config PATH --check      change nothing; exit 1 if a repo lacks a ruleset or CODEOWNERS
  python scripts/github-boundary.py --config PATH --codeowners print the CODEOWNERS file each repo carries in .github/
  python scripts/github-boundary.py --self-test                the payloads and the comparison, without the network
  --repo NAME (repeatable) limits it to those repos of the config file

--config PATH is a JSON file with "owner" (the GitHub account or organisation), "repos" (repository names under it)
and "codeowners" (the paths only the owner changes); see examples/github-boundary.example.json. It is required for
everything except --self-test.

Per repo, two rulesets, bypassed only by the repository admin role (the owner account; the bot has write access):
  1prompt-boundary-main  default branch: no deletion, no force push, changes only through a pull request, and a
                         code owner's review when a PR touches a CODEOWNERS path (the bot cannot approve those)
  1prompt-boundary-tags  every tag: no creation, update or deletion, so releases stay the owner's
Other rulesets on a repo are left as they are. It runs `gh api` as the owner account and refuses to write as anyone
else. GitHub refuses rulesets on private repos without GitHub Pro; that refusal is reported, not hidden.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

ADMIN_ROLE = 5  # GitHub's built-in repository role id for Admin
BYPASS = [{"actor_id": ADMIN_ROLE, "actor_type": "RepositoryRole", "bypass_mode": "always"}]


def rulesets():
    return [
        {"name": "1prompt-boundary-main", "target": "branch", "enforcement": "active",
         "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
         "rules": [{"type": "deletion"}, {"type": "non_fast_forward"},
                   {"type": "pull_request", "parameters": {
                       "required_approving_review_count": 0, "require_code_owner_review": True,
                       "dismiss_stale_reviews_on_push": True, "require_last_push_approval": True,
                       "required_review_thread_resolution": False}}],
         "bypass_actors": BYPASS},
        {"name": "1prompt-boundary-tags", "target": "tag", "enforcement": "active",
         "conditions": {"ref_name": {"include": ["~ALL"], "exclude": []}},
         "rules": [{"type": "creation"}, {"type": "update"}, {"type": "deletion"}],
         "bypass_actors": BYPASS},
    ]


def codeowners(config):
    lines = ["# Paths only the owner changes. Managed by scripts/github-boundary.py.",
             "# A pull request that touches one needs the owner's review; the bot account cannot give it."]
    return "\n".join(lines + [f"{path} @{config['owner']}" for path in config["codeowners"]]) + "\n"


def normal(ruleset):
    """The fields this script manages, in a comparable form (GitHub adds ids, links and default parameters)."""
    rules = []
    for rule in ruleset.get("rules", []):
        params = rule.get("parameters") or {}
        if rule["type"] == "pull_request":
            params = {k: params.get(k) for k in ("required_approving_review_count", "require_code_owner_review",
                                                  "dismiss_stale_reviews_on_push", "require_last_push_approval",
                                                  "required_review_thread_resolution")}
        rules.append({"type": rule["type"], "parameters": params})
    bypass = [{k: b.get(k) for k in ("actor_id", "actor_type", "bypass_mode")} for b in ruleset.get("bypass_actors") or []]
    return {"target": ruleset.get("target"), "enforcement": ruleset.get("enforcement"),
            "conditions": ruleset.get("conditions"), "rules": sorted(rules, key=lambda r: r["type"]),
            "bypass_actors": sorted(bypass, key=lambda b: (b["actor_type"] or "", b["actor_id"] or 0))}


def gh(*args, data=None, raw=False):
    run = subprocess.run(["gh", "api", *args] + (["--input", "-"] if data is not None else []),
                         input=json.dumps(data) if data is not None else None, capture_output=True, text=True)
    if run.returncode != 0:
        raise RuntimeError((run.stdout + run.stderr).strip().splitlines()[-1] if (run.stdout + run.stderr).strip() else "gh failed")
    if raw:
        return run.stdout
    return json.loads(run.stdout) if run.stdout.strip() else None


def plan(existing, wanted):
    """For each wanted ruleset: ("create", None), ("update", id) or ("unchanged", id)."""
    by_name = {r["name"]: r for r in existing}
    out = []
    for ruleset in wanted:
        have = by_name.get(ruleset["name"])
        if have is None:
            out.append((ruleset, "create", None))
        else:
            out.append((ruleset, "unchanged" if normal(have) == normal(ruleset) else "update", have["id"]))
    return out


def self_test():
    bad = []
    wanted = rulesets()
    if plan([], wanted)[0][1] != "create":
        bad.append("a missing ruleset is not created")
    served = json.loads(json.dumps(wanted[0]))
    served.update({"id": 7, "_links": {}, "source": "owner/x"})
    served["rules"][2]["parameters"]["allowed_merge_methods"] = ["merge"]  # a default GitHub adds
    served["rules"].reverse()
    if plan([served], wanted[:1])[0][1:] != ("unchanged", 7):
        bad.append("a served copy with GitHub's extra fields is not seen as unchanged")
    for field, value in (("enforcement", "disabled"), ("bypass_actors", BYPASS + [{"actor_id": 9, "actor_type": "Team", "bypass_mode": "always"}])):
        changed = dict(served, **{field: value, "id": 7})
        if plan([changed], wanted[:1])[0][1] != "update":
            bad.append(f"a ruleset with another {field} is not updated")
    for label, change in (("a changed condition", lambda r: r["conditions"]["ref_name"].update(include=["refs/heads/x"])),
                          ("code-owner review off", lambda r: next(x for x in r["rules"] if x["type"] == "pull_request")
                           ["parameters"].update(require_code_owner_review=False)),
                          ("last-push approval off", lambda r: next(x for x in r["rules"] if x["type"] == "pull_request")
                           ["parameters"].update(require_last_push_approval=False))):
        weakened = json.loads(json.dumps(served))
        change(weakened)
        if plan([weakened], wanted[:1])[0][1] != "update":
            bad.append(f"a ruleset with {label} is not updated")
    weaker = json.loads(json.dumps(served))
    weaker["rules"] = [r for r in weaker["rules"] if r["type"] != "non_fast_forward"]
    if plan([weaker], wanted[:1])[0][1] != "update":
        bad.append("a ruleset missing the force-push rule is not updated")
    pr = next(r for r in wanted[0]["rules"] if r["type"] == "pull_request")["parameters"]
    if not pr["require_code_owner_review"] or pr["required_approving_review_count"] != 0:
        bad.append("the main ruleset must require code-owner review and no other approval")
    if any(b["actor_type"] != "RepositoryRole" or b["actor_id"] != ADMIN_ROLE for r in wanted for b in r["bypass_actors"]):
        bad.append("only the repository admin role may bypass")
    owners = codeowners({"owner": "owner", "codeowners": ["/docs/spec/", "/.github/CODEOWNERS"]})
    if "/docs/spec/ @owner" not in owners or "/.github/CODEOWNERS @owner" not in owners:
        bad.append("CODEOWNERS lines are wrong")
    for line in bad:
        print(f"- {line}")
    print(f"{'FAIL' if bad else 'PASS'}: github-boundary payloads and comparison, {len(bad)} failures")
    return 1 if bad else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true")
    group.add_argument("--check", action="store_true")
    group.add_argument("--codeowners", action="store_true")
    group.add_argument("--self-test", action="store_true")
    parser.add_argument("--repo", action="append")
    parser.add_argument("--config", metavar="PATH", help="JSON file with owner, repos and codeowners (required except for --self-test)")
    opts = parser.parse_args()
    if opts.self_test:
        return self_test()
    if not opts.config:
        parser.error("--config PATH is required (see examples/github-boundary.example.json)")
    try:
        config = json.loads(Path(opts.config).read_text(encoding="utf-8"))
        if not (isinstance(config.get("owner"), str) and isinstance(config.get("repos"), list)
                and isinstance(config.get("codeowners"), list)):
            raise ValueError("owner, repos and codeowners are required")
    except (OSError, ValueError, AttributeError) as error:
        sys.exit(f"cannot use --config {opts.config}: {error}")
    if opts.codeowners:
        print(codeowners(config), end="")
        return 0
    repos = opts.repo or config["repos"]
    unknown = set(repos) - set(config["repos"])
    if unknown:
        sys.exit(f"not in the config file: {', '.join(sorted(unknown))}")
    login = gh("user")["login"]
    if login != config["owner"]:
        sys.exit(f"gh is signed in as {login}, not {config['owner']}: rulesets are written and checked only as the owner")
    failures = 0
    for name in repos:
        repo = f"{config['owner']}/{name}"
        try:
            existing = [gh(f"repos/{repo}/rulesets/{r['id']}") for r in gh(f"repos/{repo}/rulesets") or []]
        except RuntimeError as error:
            print(f"{repo}: cannot read rulesets ({error})")
            failures += 1
            continue
        for ruleset, action, rid in plan(existing, rulesets()):
            try:
                if opts.apply and action == "create":
                    gh(f"repos/{repo}/rulesets", "-X", "POST", data=ruleset)
                elif opts.apply and action == "update":
                    gh(f"repos/{repo}/rulesets/{rid}", "-X", "PUT", data=ruleset)
            except RuntimeError as error:
                print(f"{repo}: {ruleset['name']} could not be {action}d ({error})")
                failures += 1
                continue
            done = {"create": "created", "update": "updated"}.get(action, action) if opts.apply else action
            print(f"{repo}: {ruleset['name']} {done if opts.apply else ('would ' + action if action != 'unchanged' else action)}")
            failures += action != "unchanged" and opts.check
        if opts.check:
            try:
                served = gh(f"repos/{repo}/contents/.github/CODEOWNERS", "-H", "Accept: application/vnd.github.raw+json", raw=True)
            except RuntimeError:
                served = None
            if served != codeowners(config):
                print(f"{repo}: .github/CODEOWNERS missing or different on the default branch")
                failures += 1
    return 1 if failures else 0  # a repo that could not be read or written fails every mode, the dry run included


if __name__ == "__main__":
    sys.exit(main())
