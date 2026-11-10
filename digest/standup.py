"""A factual stand-up survives model failure; digest replies are never inputs."""

from __future__ import annotations

import argparse
import datetime as dt
from decimal import Decimal, ROUND_CEILING
import json
import os
from pathlib import Path
import re
import urllib.request
from zoneinfo import ZoneInfo

from lifecycle import core

SECTION_NAMES = [
    "Needs your decision",
    "Meaningful assumptions",
    "Ready to demo",
    "Done since the last digest",
    "In progress and at risk",
    "Usage and cost",
]
INTRO = [
    "Please review the items that need your decision.",
    "The linked records contain the supporting evidence.",
    "You can act on the linked items when you are ready.",
    "No reply to this digest is needed.",
]
REFUSALS = {
    "outside_or_invalid_path",
    "git_metadata",
    "denied_path",
    "external_or_invalid_symlink",
    "symlink_parent",
    "denied_symlink_target",
    "submodule_not_supported",
    "special_file_not_supported",
    "file_count_limit",
    "file_size_limit",
    "total_size_limit",
    "history_changed",
    "concurrent_change",
    "preexisting_staged_changes",
    "preexisting_worktree_changes",
    "configure_runner_identity",
    "no_committable_changes",
    "git_lock_busy",
    "git_operation_failed",
    "git_configuration_unavailable",
    "worktree_must_be_repository_root",
    "unreadable_or_changed_path",
    "submodule_or_directory_not_supported",
}


def load_config(path):
    cfg = core.read(path)
    for field in ("state_dir", "lifecycle_config"):
        if not Path(cfg.get(field, "")).is_absolute():
            raise ValueError("Absolute private digest paths required")
    cfg["lifecycle"] = core.load_config(cfg["lifecycle_config"])
    cadence = cfg["cadence"]
    ZoneInfo(cadence["timezone"])
    if (
        not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", cadence["time"])
        or not cadence["weekdays"]
        or any(type(n) is not int or not 0 <= n <= 6 for n in cadence["weekdays"])
        or type(cadence["overdue_after_skips"]) is not int
        or cadence["overdue_after_skips"] < 1
    ):
        raise ValueError("Invalid digest cadence")
    for value in cfg.get("pause_dates", []):
        dt.date.fromisoformat(value)
    if type(cfg.get("paused", False)) is not bool:
        raise ValueError("Pause must be boolean")
    for location in cfg["runner_state_dirs"]:
        if not Path(location).is_absolute():
            raise ValueError("Absolute runner state paths required")
    for location in cfg.get("pool_configs", []):
        if not Path(location).is_absolute():
            raise ValueError("Absolute private pool config paths required")
    model = cfg.get("model", {})
    if type(model.get("enabled", False)) is not bool:
        raise ValueError("Model enabled must be boolean")
    if model.get("enabled", False):
        validate_model(model)
    for code, assumption in cfg.get("public_assumptions", {}).items():
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code) or set(assumption) != {
            "summary",
            "view",
        }:
            raise ValueError("Assumptions require approved public summary/view entries")
        for text in assumption.values():
            if (
                not isinstance(text, str)
                or not text.strip()
                or len(text) > 240
                or any(c in text for c in "\n\r[]<>\\")
            ):
                raise ValueError("Public assumption text must be short plain text")
    for number, url in cfg.get("previews", {}).items():
        parsed = core.urllib.parse.urlsplit(url)
        if (
            not number.isdigit()
            or parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "Previews must be explicit HTTPS links without credentials/query data"
            )
    return cfg


def validate_model(model):
    if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", model["key_env"]):
        raise ValueError("Explicit model-key environment name required")
    if (
        not isinstance(model["name"], str)
        or not model["name"].strip()
        or "<" in model["name"]
    ):
        raise ValueError("Populate the Product model identifier")
    for field in (
        "monthly_budget_usd",
        "input_usd_per_million",
        "output_usd_per_million",
    ):
        amount = Decimal(str(model[field]))
        if not amount.is_finite() or amount < 0:
            raise ValueError("Budget/prices must be finite nonnegative amounts")
    if (
        type(model["max_output_tokens"]) is not int
        or not 16 <= model["max_output_tokens"] <= 1024
    ):
        raise ValueError("Bounded summary output required")


def task_link(cfg, number, pr=False):
    if type(number) is not int or number < 1:
        raise ValueError("Positive issue/PR ID required")
    return f"https://github.com/{cfg['lifecycle']['repository']}/{'pull' if pr else 'issues'}/{number}"


def linked(text, url):
    return f"- {text} ([record]({url}))"


def local_jobs(cfg):
    found = {}
    for directory in cfg["runner_state_dirs"]:
        root = Path(directory)
        for pattern in ("tasks/*.json", "jobs/*/runner-state/tasks/*.json"):
            for path in root.glob(pattern):
                job = core.read(path)
                binding = job.get("assignment", {}).get("lifecycle")
                number = binding["issue"] if binding else job.get("github_number")
                if (
                    not core.ID.fullmatch(job.get("id", ""))
                    or type(number) is not int
                    or number < 1
                ):
                    continue
                identity = (number, job["id"], job.get("base_commit"))
                if identity not in found or core.stamp(job["updated_at"]) > core.stamp(
                    found[identity]["updated_at"]
                ):
                    found[identity] = job
    return list(found.values())


def collect(cfg, client, since, at):
    policy = cfg["lifecycle"]
    sections = [[] for _ in SECTION_NAMES]
    issues = sorted(
        (i for i in client.pages("/issues?state=all") if "pull_request" not in i),
        key=lambda i: i["number"],
    )
    pool_snapshots = []
    pool_ready = {}
    for pool_config in cfg.get("pool_configs", []):
        from runner import pool

        coordinator = pool.Pool(pool.load_config(pool_config))
        snapshot = coordinator.snapshot()
        pool_snapshots.append((coordinator, snapshot))
        for name, entry in snapshot["stories"].items():
            pool_ready[name] = entry["head"] if entry.get("status") == "ready_for_integration" else None
    demo_jobs = {}
    for issue in issues:
        if core.is_report(policy, issue):
            continue
        number = issue["number"]
        url = task_link(cfg, number)
        try:
            current = core.state(issue)
        except ValueError:
            sections[4].append(
                linked(f"Task #{number}: lifecycle status needs reconciliation", url)
            )
            continue
        canonical, label_event = core.canonical_state(policy, client.events(number))
        if canonical != current:
            sections[4].append(
                linked(
                    f"Task #{number}: untrusted status change awaits reconciliation",
                    url,
                )
            )
            continue
        history = core.records(policy, client.comments(number), number)
        dispatches = [r for r in history if r["to"] == "dispatched"]
        if (
            issue["state"] == "open"
            and current in {"in-review", "pr-open"}
            and dispatches
        ):
            demo_jobs[number] = dispatches[-1]["job"]
        recent = [r for r in history if core.stamp(r["timestamp"]) > since]
        if current in {"needs-owner", "blocked", "draft"} and issue["state"] == "open":
            text = (
                "waiting for scope approval"
                if current == "draft"
                else "owner decision required"
            )
            sections[0].append(linked(f"Task #{number}: {text}", url))
        for record in recent:
            if record["kind"] == "assumption" and record["reason"] in cfg.get(
                "public_assumptions", {}
            ):
                approved = cfg["public_assumptions"][record["reason"]]
                sections[1].append(
                    linked(
                        f"Task #{number}: {approved['summary']} Product view: {approved['view']}",
                        url,
                    )
                )
        if (
            issue["state"] == "closed"
            and issue.get("closed_at")
            and core.stamp(issue["closed_at"]) > since
        ):
            sections[3].append(linked(f"Task #{number}: closed", url))
        if issue["state"] == "open":
            changed = core.stamp(label_event["created_at"]) if label_event else None
            if changed is None:
                sections[4].append(
                    linked(f"Task #{number}: state age unavailable", url)
                )
            elif (at - changed).total_seconds() > policy["stale_seconds"][current]:
                sections[4].append(
                    linked(f"Task #{number}: stale {current}; no automatic retry", url)
                )
            elif current in {
                "dispatched",
                "in-progress",
                "committed",
                "in-review",
                "changes-requested",
                "needs-primary",
            }:
                sections[4].append(linked(f"Task #{number}: {current}", url))
        rounds = max((r["round"] for r in history), default=0)
        if rounds > 1:
            sections[4].append(
                linked(f"Task #{number}: {rounds - 1} correction rounds recorded", url)
            )
    for pr in sorted(client.pages("/pulls?state=all"), key=lambda p: p["number"]):
        url = task_link(cfg, pr["number"], True)
        if pr["state"] == "open":
            gate = core.trusted_gate(policy, client, pr)
            sections[0].append(
                linked(
                    f"PR #{pr['number']}: waiting for owner review or merge; trusted gate {gate}",
                    url,
                )
            )
        elif pr.get("merged_at") and core.stamp(pr["merged_at"]) > since:
            sections[3].append(linked(f"PR #{pr['number']}: merged", url))
    outbox = [
        core.read(p) for p in (Path(policy["state_dir"]) / "outbox").glob("*.json")
    ]
    followup_records = [
        core.read(p) for p in (Path(policy["state_dir"]) / "followups").glob("*.json")
    ]
    jobs = local_jobs(cfg)
    committed, refused, demos = set(), set(), set()
    for job in jobs:
        number = (
            job.get("assignment", {})
            .get("lifecycle", {})
            .get("issue", job.get("github_number"))
        )
        url = task_link(cfg, number)
        if (
            job["status"] == "ready_for_integration"
            and (job["id"] not in pool_ready or pool_ready[job["id"]] == job.get("head_commit"))
            and demo_jobs.get(number) == job["id"]
            and number not in demos
            and core.SHA.fullmatch(job.get("head_commit", ""))
            and job.get("reviewed_head") == job["head_commit"]
            and job.get("review", {}).get("status") == "ready_for_integration"
        ):
            text = f"Task #{number}: independently reviewed and ready to demo"
            if str(number) in cfg.get("previews", {}):
                text += f"; [preview]({cfg['previews'][str(number)]})"
            sections[2].append(linked(text, url))
            demos.add(number)
        for item in job.get("runner_commits", []):
            identity = (job["id"], item["round"], item["commit_sha"])
            if (
                not core.SHA.fullmatch(item["commit_sha"])
                or identity in committed
                or core.stamp(item["timestamp"]) <= since
            ):
                continue
            committed.add(identity)
            if item["state"] == "committed" and item["committed_by"] == "runner":
                count = item["file_count"]
                if type(count) is int and count >= 0:
                    sections[3].append(
                        linked(
                            f"Runner commit `{item['commit_sha']}`; job `{job['id']}`, round {item['round']}, {count} files",
                            url,
                        )
                    )
            elif item["state"] == "prepared":
                sections[0].append(
                    linked(
                        f"Task #{number}: runner commit publication uncertain; reconcile privately",
                        url,
                    )
                )
        for item in job.get("commit_refusals", []):
            identity = (job["id"], item["round"], item["reason"], item["timestamp"])
            if identity in refused or core.stamp(item["timestamp"]) <= since:
                continue
            refused.add(identity)
            reason = (
                item["reason"] if item["reason"] in REFUSALS else "validation_refused"
            )
            sections[0].append(
                linked(
                    f"Task #{number}: runner commit refused ({reason}); job `{job['id']}`, round {item['round']}",
                    url,
                )
            )
        relevant = [entry for entry in outbox if entry["record"]["job"] == job["id"]]
        pending_minor = any(
            entry["job"] == job["id"] and entry["status"] != "posted"
            for entry in followup_records
        )
        if (
            job.get("lifecycle_unsynced")
            and not relevant
            or any(
                entry["status"] not in {"synced", "superseded"} for entry in relevant
            )
            or pending_minor
        ):
            sections[4].append(
                linked(f"Task #{number}: lifecycle updates unsynced", url)
            )
        if job["status"] in {
            "blocked",
            "failed",
            "interrupted",
            "needs_owner",
            "needs_primary_review",
        }:
            sections[4].append(
                linked(f"Task #{number}: local job needs attention", url)
            )
    # Outbox failures are visible even when the last job update predates them.
    for path in (Path(policy["state_dir"]) / "outbox").glob("*.json"):
        item = core.read(path)
        if item["status"] not in {"synced", "superseded"}:
            number = item["record"]["issue"]
            sections[4].append(
                linked(
                    f"Task #{number}: lifecycle update unsynced or conflicting",
                    task_link(cfg, number),
                )
            )
    for path in (Path(policy["state_dir"]) / "claims").glob("*.json"):
        claim = core.read(path)
        if claim.get("stage") == "suspended" or claim.get("cleanup_pending"):
            sections[4].append(
                linked(
                    f"Task #{claim['issue']}: dispatch or managed cleanup needs reconciliation",
                    task_link(cfg, claim["issue"]),
                )
            )
    for path in (Path(policy["state_dir"]) / "followups").glob("*.json"):
        followup = core.read(path)
        if followup["status"] != "posted":
            sections[4].append(
                linked(
                    f"Task #{followup['parent_issue']}: minor follow-up publication unsynced",
                    task_link(cfg, followup["parent_issue"]),
                )
            )
    # Every usage item links to the repository's issue list, not private local logs.
    active = [j for j in jobs if core.stamp(j["updated_at"]) > since]
    sections[5].append(
        linked(
            f"Runner jobs observed in this window: {len(active)}. Runner model cost unavailable",
            f"https://github.com/{policy['repository']}/issues",
        )
    )
    # Pool status is fixed local scheduling data; never publish config paths,
    # commands, homes or raw provider replies. Reading it launches no model.
    for coordinator, snapshot in pool_snapshots:
        issue_list = f"https://github.com/{policy['repository']}/issues"
        for slot, entry in snapshot["slots"].items():
            active_phase = entry.get("active")
            status = "busy" if active_phase else "cooldown" if entry["cooldown_until"] > coordinator.clock() else "idle"
            current = active_phase["story"] if active_phase else "none"
            sections[5].append(linked(
                f"Pool slot {slot}: {status}; current story {current}; budget used {entry['used']}/{coordinator.slots[slot]['budget']} owner units; usage unavailable for {entry['unknown_phases']} phases (reserved estimates retained)", issue_list))
        for name, entry in snapshot["stories"].items():
            if entry.get("waiting"):
                sections[4].append(linked(f"Pool story {name}: {entry['waiting']}", issue_list))
    return [sorted(set(items)) for items in sections]


class Claude:
    def __init__(self, cfg):
        self.cfg = cfg

    def summarize(self, allowed):
        model = self.cfg["model"]
        api_key = os.environ.get(model["key_env"])
        if not api_key:
            raise core.Unavailable("Product API key unavailable")
        prompt = summary_prompt(allowed)
        body = dict(
            model=model["name"],
            max_tokens=model["max_output_tokens"],
            messages=[{"role": "user", "content": prompt}],
        )
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            method="POST",
            data=json.dumps(body).encode(),
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
        with urllib.request.build_opener(core.NoRedirect).open(
            req, timeout=30
        ) as response:
            result = json.loads(response.read())
        prose = "\n".join(
            block["text"] for block in result["content"] if block["type"] == "text"
        )
        return prose, result.get("usage", {})


def summary_prompt(allowed):
    return (
        "Write a brief stand-up introduction using one to three of the following eligible sentences verbatim, "
        "one per line. No other text, facts, names, numbers, statuses, links or markdown.\n"
        + "\n".join(allowed)
    )


def summary(cfg, model, digest_id, at):
    if not cfg.get("model", {}).get("enabled", False):
        return None, "disabled", {}
    ledger_path = Path(cfg["state_dir"]) / "budget.json"
    with core.lock(Path(cfg["state_dir"]) / "budget.lock") as acquired:
        if not acquired:
            return None, "budget_locked", {}
        ledger = core.read(ledger_path) if ledger_path.exists() else {"months": {}}
        month = at.astimezone(dt.timezone.utc).strftime("%Y-%m")
        calls = ledger["months"].setdefault(month, {})
        if digest_id in calls:
            previous = calls[digest_id]
            return previous.get("summary"), previous["result"], previous
        config = cfg["model"]
        # Conservative byte-based estimate plus protocol overhead; this is an
        # estimate guard, not a guarantee of the provider's eventual invoice.
        tokens = len(summary_prompt(INTRO).encode()) + 1024
        maximum = config["max_output_tokens"]
        cost = int(
            (
                Decimal(tokens) * Decimal(str(config["input_usd_per_million"]))
                + Decimal(maximum) * Decimal(str(config["output_usd_per_million"]))
            ).to_integral_value(rounding=ROUND_CEILING)
        )
        budget = int(Decimal(str(config["monthly_budget_usd"])) * 1_000_000)
        spent = sum(c["estimated_micro_usd"] for c in calls.values())
        if spent + cost > budget:
            return (
                None,
                "monthly_budget_exhausted",
                dict(
                    estimated_input_tokens=tokens,
                    estimated_output_tokens=maximum,
                    estimated_micro_usd=0,
                ),
            )
        record = dict(
            estimated_input_tokens=tokens,
            estimated_output_tokens=maximum,
            estimated_micro_usd=cost,
            result="summary_unavailable",
            attempted=True,
        )
        calls[digest_id] = record
        core.save(
            ledger_path, ledger
        )  # reserve BEFORE the request, including ambiguous failures
        try:
            prose, usage = model.summarize(INTRO)
            lines = [
                line.strip() for line in prose.strip().splitlines() if line.strip()
            ]
            if (
                not 1 <= len(lines) <= 3
                or len(lines) != len(set(lines))
                or any(line not in INTRO for line in lines)
            ):
                raise ValueError(
                    "Model prose outside the non-factual sentence contract"
                )
            record.update(summary=" ".join(lines), result="available")
            if (
                type(usage.get("input_tokens")) is int
                and usage["input_tokens"] >= 0
                and type(usage.get("output_tokens")) is int
                and usage["output_tokens"] >= 0
            ):
                record["reported_usage"] = {
                    k: usage[k] for k in ("input_tokens", "output_tokens")
                }
        except Exception:
            pass  # factual rendering and publication must survive model failure
        core.save(ledger_path, ledger)
        return record.get("summary"), record["result"], record


def render(sections, prose, summary_status, usage, marker):
    parts = [
        "Act on the linked issue or PR. Replies to this digest never approve or instruct automation."
    ]
    parts.append(
        prose
        if prose
        else (
            "Model disabled; factual sections follow."
            if summary_status == "disabled"
            else "Summary unavailable; factual sections follow."
        )
    )
    sections = [list(items) for items in sections]
    usage_link = next(
        (
            item.rsplit("([record](", 1)[1][:-2]
            for item in sections[5]
            if "([record](" in item
        ),
        None,
    )
    if usage_link:
        message = f"Product summary: {summary_status}"
        if usage:
            message += (
                f"; estimated input/output tokens {usage['estimated_input_tokens']}/{usage['estimated_output_tokens']}"
                f"; reserved estimate USD {Decimal(usage['estimated_micro_usd']) / Decimal(1_000_000)}"
            )
        sections[5].append(linked(message, usage_link))
    for name, items in zip(SECTION_NAMES, sections):
        if items:
            parts.append("## " + name + "\n\n" + "\n".join(items))
    parts.append(marker)
    return "\n\n".join(parts) + "\n"


def owner_skipped(policy, client, issue):
    labels = {
        label["name"] if isinstance(label, dict) else label for label in issue["labels"]
    }
    if "digest:skip" not in labels:
        return False
    events = [
        e
        for e in client.events(issue["number"])
        if e.get("event") == "labeled"
        and e.get("label", {}).get("name") == "digest:skip"
    ]
    return bool(
        events
        and max(events, key=lambda e: int(e["id"]))["actor"]["login"]
        == policy["actors"]["owner"]
    )


def run_once(cfg, client, model=None, *, at=None, request_id=None):
    at = at or dt.datetime.now(dt.timezone.utc)
    if at.tzinfo is None:
        raise ValueError("Digest clock needs timezone")
    local = at.astimezone(ZoneInfo(cfg["cadence"]["timezone"]))
    period = local.date().isoformat()
    if request_id is not None and not core.ID.fullmatch(request_id):
        raise ValueError("Invalid on-demand request ID")
    root = core.private(cfg["state_dir"])
    identity = {
        "repository": cfg["lifecycle"]["repository"],
        "product": cfg["lifecycle"]["actors"]["product"],
    }
    core.create(root / "identity.json", identity)
    if core.read(root / "identity.json") != identity:
        raise ValueError(
            "Digest state belongs to another repository or Product identity"
        )
    with core.lock(root / "digest.lock") as acquired:
        if not acquired:
            return {"status": "locked"}
        path = root / "digest.json"
        journal = (
            core.read(path)
            if path.exists()
            else dict(history=[], skips=0, skipped_dates=[])
        )
        policy = cfg["lifecycle"]
        # Immediate urgency is independent of cadence, pauses and summary budget.
        for issue in client.pages("/issues?state=open"):
            if "pull_request" in issue or core.is_report(policy, issue):
                continue
            try:
                current = core.state(issue)
                canonical, _ = core.canonical_state(
                    policy, client.events(issue["number"])
                )
                if canonical == current and current in {"blocked", "needs-owner"}:
                    client.notify(issue["number"])
            except ValueError:
                continue
        for previous in journal["history"]:
            if previous.get("issue") and not previous.get("skip_observed"):
                if owner_skipped(policy, client, client.issue(previous["issue"])):
                    previous.update(skipped=True, skip_observed=True)
                    journal["skips"] = max(
                        journal["skips"], previous.get("carry_skips", 0) + 1
                    )
        since = max(
            (
                core.stamp(p["cutoff"])
                for p in journal["history"]
                if p.get("issue") and not p.get("skipped")
            ),
            default=dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc),
        )
        scheduled_due = (
            local.weekday() in cfg["cadence"]["weekdays"]
            and local.strftime("%H:%M") >= cfg["cadence"]["time"]
        )
        if cfg.get("paused") or period in cfg.get("pause_dates", []):
            if (
                request_id is None
                and scheduled_due
                and period not in journal["skipped_dates"]
            ):
                journal["skipped_dates"].append(period)
                journal["skips"] += 1
            core.save(path, journal)
            return {"status": "skipped"}
        digest_id = (
            core.key(["manual", request_id])
            if request_id
            else core.key(["scheduled", period])
        )
        prior = next((p for p in journal["history"] if p["id"] == digest_id), None)
        if prior and prior.get("issue"):
            core.save(path, journal)
            return {"status": "already_posted", "issue": prior["issue"]}
        if request_id is None and not scheduled_due:
            core.save(path, journal)
            return {"status": "not_due"}
        marker = "<!-- agentic-digest-v1:" + digest_id + " -->"
        # Reconcile an ambiguous create before considering another POST.
        existing = [
            i
            for i in client.pages("/issues?state=all")
            if i.get("user", {}).get("login") == policy["actors"]["product"]
            and marker in (i.get("body") or "")
        ]
        if existing:
            if prior is None:
                prior = dict(id=digest_id, cutoff=at.isoformat())
                journal["history"].append(prior)
            prior["issue"] = existing[0]["number"]
            core.save(path, journal)
            return {"status": "already_posted", "issue": prior["issue"]}
        if prior and prior.get("attempted"):
            core.save(path, journal)
            return {"status": "uncertain_requires_reconciliation"}
        sections = collect(cfg, client, since, at)
        if not any(sections[:3]):
            core.save(path, journal)
            return {"status": "quiet"}
        prose, summary_status, usage = summary(cfg, model or Claude(cfg), digest_id, at)
        title = (
            (
                "[Overdue] "
                if journal["skips"] >= cfg["cadence"]["overdue_after_skips"]
                else ""
            )
            + "Stand-up "
            + period
        )
        body = render(sections, prose, summary_status, usage, marker)
        if prior is None:
            prior = dict(id=digest_id, cutoff=at.isoformat())
            journal["history"].append(prior)
        prior.update(attempted=True, title=title, carry_skips=journal["skips"])
        core.save(path, journal)
        try:
            created = client.request(
                "POST",
                "/issues",
                {"title": title, "body": body, "labels": ["report:standup"]},
            )
        except core.Unavailable as exc:
            if not isinstance(exc, core.Uncertain):
                prior["attempted"] = False
            core.save(path, journal)
            return {"status": "unsynced"}
        prior["issue"] = created["number"]
        journal["skips"] = 0
        core.save(path, journal)
        return {
            "status": "posted",
            "issue": created["number"],
            "summary": summary_status,
        }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--id", help="Unique on-demand request ID; omit for scheduled cadence"
    )
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    print(
        json.dumps(
            run_once(cfg, core.GitHub(cfg["lifecycle"], "product"), request_id=args.id)
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
