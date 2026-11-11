"""Opt-in standalone delivery, using trusted tool code and repository-scoped identity.

Agent output never selects API endpoints, credentials, merge policy or public prose.
Write intents are durable before transport; uncertain writes require reconciliation.
"""

import base64
import fnmatch
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.request
import urllib.parse

TIERS = ["light", "standard", "domain-critical", "control-critical"]
RELEASE_PATHS = ["changelog*", "*/changelog*", "releases/*", "*/releases/*",
                 "*release-notes*", "*release_notes*"]
REASONS = {
    "publication_failed": "Publishing the reviewed commit or pull request failed; reconcile before retrying.",
    "publication_uncertain": "A publication write may have completed; inspect GitHub before retrying.",
    "stale_head": "The pull request or local head changed; a fresh review is required.",
    "gate_category_higher": "The trusted base rules require a higher review category than declared.",
    "required_check_failed": "A required check failed; inspect the pull request checks before correcting.",
    "merge_conditions_missing": "Merge evidence is missing or contradicted; owner reconciliation is required.",
    "owner_merge_required": "Control-critical changes and releases require the owner to merge.",
    "auto_merge_disable_uncertain": "Auto-merge cancellation could not be confirmed; the owner must inspect the pull request.",
}


class EvidenceMissing(ValueError):
    pass


def options(config):
    value = config.get("integration", {"enabled": False})
    if not isinstance(value, dict) or type(value.get("enabled")) is not bool:
        raise ValueError("Integration requires an explicit enabled boolean")
    if not value["enabled"]:
        return None
    if (not config.get("lifecycle_config") or config.get("pool_slot") or config.get("phase_context")
            or value.get("declared_tier") not in TIERS
            or not isinstance(value.get("public_evidence", ""), str)):
        raise ValueError("Standalone lifecycle integration needs a declared tier and public evidence")
    second = value.get("second_reviewer")
    if (not isinstance(value.get("release_paths", []), list)
            or any(not isinstance(p, str) or not p for p in value.get("release_paths", []))):
        raise ValueError("Release paths must be a list of additional patterns")
    if second is not None and (
        not isinstance(second, dict) or set(second) != {"actor", "provider"}
        or not re.fullmatch(r"[A-Za-z0-9-]+(?:\[bot\])?", second["actor"])
        or not re.fullmatch(r"[A-Za-z0-9_-]+", second["provider"])
        or not isinstance(value.get("reviewer_provider"), str)
        or second["provider"] == value["reviewer_provider"]
    ):
        raise ValueError("Invalid second reviewer configuration")
    return value


def load_gate():
    spec = importlib.util.spec_from_file_location(
        "_integration_gate", Path(__file__).resolve().parents[1] / "gate/review_gate.py")
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    return gate


def client_for(core, cfg):
    class DeliveryGitHub(core.GitHub):
        def write_allowed(self, method, path):
            return super().write_allowed(method, path) or (
                method == "POST" and bool(re.fullmatch(r"/pulls(?:/[1-9]\d*/reviews)?", path))) or (
                method == "PUT" and bool(re.fullmatch(r"/pulls/[1-9]\d*/merge", path)))

        def enable_auto_merge(self, pr):
            return self.auto_merge(pr, True)

        def disable_auto_merge(self, pr):
            return self.auto_merge(pr, False)

        def auto_merge(self, pr, enable):
            self.ensure_actor()
            mutation = "enablePullRequestAutoMerge" if enable else "disablePullRequestAutoMerge"
            query = ("mutation($id:ID!){" + mutation + "(input:{pullRequestId:$id"
                     + (",mergeMethod:MERGE" if enable else "") + "}){pullRequest{id}}}")
            req = urllib.request.Request(
                "https://api.github.com/graphql", method="POST",
                data=json.dumps({"query": query, "variables": {"id": pr["node_id"]}}).encode(),
                headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
            try:
                with self.opener.open(req, timeout=30) as response:
                    result = json.loads(response.read())
                errors = result.get("errors", [])
                if enable and errors and all(e.get("type") == "UNPROCESSABLE"
                                  and "clean status" in e.get("message", "").lower() for e in errors):
                    return False
                if result.get("errors") or not result.get("data", {}).get(mutation):
                    raise core.Unavailable("Auto-merge rejected")
                return True
            except (OSError, ValueError):
                raise core.Uncertain("Auto-merge outcome unknown") from None

        def push(self, runner, config, head):
            self.ensure_actor()
            # Never use origin, a credential helper, user config, hooks or the
            # source repository's executable config for an authenticated push.
            # The independent disposable copy has no remote or includes.
            with tempfile.TemporaryDirectory(prefix="delivery-", dir=config["state_dir"]) as scratch:
                checkout = runner.verification_copy(config, head, Path(scratch))
                env = {k: v for k, v in os.environ.items() if k in {"PATH", "SYSTEMROOT", "LANG"}}
                auth = base64.b64encode(("x-access-token:" + self.token).encode()).decode()
                env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                           HOME=scratch, XDG_CONFIG_HOME=scratch,
                           GIT_TERMINAL_PROMPT="0", GIT_CONFIG_COUNT="1",
                           GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                           GIT_CONFIG_VALUE_0="AUTHORIZATION: basic " + auth)
                subprocess.run(
                    ["git", "-C", str(checkout), "-c", "credential.helper=",
                     "-c", "core.hooksPath=" + os.devnull, "-c", "http.followRedirects=false",
                     "push", "--porcelain", "https://github.com/" + cfg["repository"] + ".git",
                     head + ":refs/heads/" + config["branch"]],
                    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=120)

    return DeliveryGitHub(cfg)


def reviewed(runner, config, job):
    head = job.get("head_commit")
    history = job.get("review_history", [])
    tests = job.get("verification_runs", [])
    if (job["assignment"]["kind"] != "work" or not runner.SHA.fullmatch(head or "")
            or not job["assignment"].get("lifecycle")
            or job["assignment"]["lifecycle"]["issue"] != job.get("github_number")
            or job.get("reviewed_head") != head or not history
            or history[-1].get("head") != head
            or history[-1].get("session") != job.get("review_session")
            or not job.get("review_session")
            or history[-1]["result"] != job.get("review")
            or job["review"]["status"] != "ready_for_integration"
            or any(f["severity"] in {"P0", "P1"} for f in job["review"]["findings"])
            or not tests or tests[-1]["head"] != head or tests[-1]["status"] != "passed"
            or not any(c["state"] == "committed" and c["commit_sha"] == head
                       and c["committed_by"] == "runner" for c in job.get("runner_commits", []))):
        raise EvidenceMissing("Missing exact-head independent review and required-test evidence")
    runner.verify_assignment(config, job)
    try:
        runner.ensure_reviewable(config, job)
    except RuntimeError:
        raise ValueError("Reviewed checkout changed") from None


def criteria(text):
    # Preserve every criterion byte, including continuation lines. The immutable
    # owner-approved story, never a worker paraphrase, is the sole source.
    match = re.search(r"(?m)^- AC-[1-9]\d*:", text)
    if not match:
        raise ValueError("Approved story requires AC-N criteria")
    block = re.split(r"(?m)^##? |^---\s*$", text[match.start():], maxsplit=1)[0].rstrip()
    ids = re.findall(r"(?m)^- (AC-[1-9]\d*):", block)
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate criteria")
    return block, ids


def body_for(runner, config, job, cfg, opts, receipt_url):
    original, ids = criteria(job["assignment"]["task_text"])
    head = job["head_commit"]
    receipt = runner.public_receipt(runner.read(
        Path(config["state_dir"]) / "receipts" / (job["id"] + ".json")))
    # The evidence link contains only the runner's fixed summaries and SHA/count
    # metadata. Raw argv, output, paths and agent prose stay private.
    counts = [r.get("reported_counts") for r in job["verification_runs"][-1].get("results", [])]
    body = (f"## Review tier\nTier: {opts['declared_tier']}\n\n"
            f"## Maker summary\nMaker context: runner job {job['id']}\n"
            f"Closes #{job['github_number']}\n\n"
            f"## Test evidence\nTested head: {head}\n"
            f"Runner required checks: PASS. Reported counts: {json.dumps(counts)}\n"
            f"Sanitized receipt: {receipt_url}\n```json\n{json.dumps(receipt, indent=2)}\n```\n\n"
            "## Assumptions\nNone beyond the immutable approved assignment.\n\n"
            f"## Original acceptance criteria\nIssue: https://github.com/{cfg['repository']}/issues/"
            f"{job['github_number']}\n{original}\n\n"
            "## Acceptance evidence\n| Criterion | Evidence | Result |\n| --- | --- | --- |\n")
    body += "".join(f"| {ac} | {receipt_url}; fresh review {job['review_session']} | PASS: reviewed on {head} |\n"
                    for ac in ids)
    if opts["declared_tier"] == "control-critical":
        body += ("\n## Acceptance and evidence matrix\n"
                 "| Criterion | Changed paths | Commands and results | Evidence link | Maker state | Checker verdict |\n"
                 "| --- | --- | --- | --- | --- | --- |\n")
        body += "".join(f"| {ac} | Reviewed commit diff | Runner checks PASS | {receipt_url} | VERIFIED | PENDING |\n"
                        for ac in ids)
        body += "\n## Independent Checker\nVerdict: PENDING\n"
    return body + "\n" + opts.get("public_evidence", "")


def snapshot(pr):
    return (pr["head"]["sha"], pr["base"]["sha"], pr["base"]["ref"], pr.get("body"),
            pr["state"], pr.get("updated_at"), pr.get("changed_files"))


def release_work(runner, config, job, opts):
    paths = runner.git(config, "diff", "--name-only", "-z", job["base_commit"], job["head_commit"]).split("\0")
    return opts.get("release", False) or any(
        fnmatch.fnmatchcase(path.lower(), pattern.lower())
        for path in paths if path for pattern in RELEASE_PATHS + opts.get("release_paths", []))


def bound_pr(client, cfg, config, job, delivery):
    pr = client.request("GET", f"/pulls/{delivery['pr']}")
    if (pr["head"]["sha"] != job["reviewed_head"]
            or pr["head"]["ref"] != config["branch"]
            or pr["head"]["repo"]["full_name"] != cfg["repository"]
            or pr["base"]["repo"]["full_name"] != cfg["repository"]
            or pr["base"]["ref"] != cfg.get("base_branch", "main")
            or pr.get("body") != delivery["body"]):
        raise ValueError("PR binding changed")
    return pr


def category(client, cfg, pr, gate):
    # Config comes from the immutable target BASE, never the story checkout.
    encoded = client.request("GET", "/contents/.github/review-gate.json?ref=" + pr["base"]["sha"])
    rules = json.loads(base64.b64decode(encoded["content"], validate=False))
    if rules["repo_url"] != "https://github.com/" + cfg["repository"] or rules["tiers"]["order"] != TIERS:
        raise ValueError("Unsupported trusted rules")
    def api(path):
        prefix = "/repos/" + cfg["repository"]
        if not path.startswith(prefix + "/"):
            raise ValueError("Foreign API path")
        return client.request("GET", path[len(prefix):])
    errors, receipt = gate.evaluate(
        {"repository": {"full_name": cfg["repository"]}, "pull_request": pr}, api, rules)
    return errors, receipt["minimum_tier"]


def merge_evidence(core, cfg, client, pr, opts, job):
    gate = core.trusted_gate(cfg, client, pr, strict_snapshot=True)
    if gate == "fail":
        return "required_check_failed"
    if gate != "pass":
        return "pending"
    runs = []
    # Use complete, paginated collections; unknown or duplicate identities stop.
    for page in range(1, 1001):
        result = client.request("GET", f"/commits/{pr['head']['sha']}/check-runs?per_page=100&page={page}")
        runs.extend(result["check_runs"])
        if len(result["check_runs"]) < 100:
            break
    if len(runs) != result["total_count"] or len({r['id'] for r in runs}) != len(runs):
        return "merge_conditions_missing"
    # Only trusted Actions checks from the configured workflow count. A matching
    # check name from an App or another workflow cannot authorize a merge.
    workflow_path = opts.get("checks_workflow", ".github/workflows/ci.yml")
    workflow = client.request("GET", "/actions/workflows/" + urllib.parse.quote(Path(workflow_path).name))
    checks = []
    for check in runs:
        if check["name"] != "checks":
            continue
        match = re.fullmatch(re.escape("https://github.com/" + cfg["repository"])
                             + r"/actions/runs/([1-9]\d*)/job/[1-9]\d*", check.get("details_url", ""))
        if not match or check.get("app", {}).get("slug") != "github-actions":
            return "merge_conditions_missing"
        run = client.request("GET", "/actions/runs/" + match[1])
        if (workflow["path"] != workflow_path or run["workflow_id"] != workflow["id"]
                or run["event"] != "pull_request" or run["head_sha"] != pr["head"]["sha"]
                or check.get("head_sha") != pr["head"]["sha"]
                or not any(p["number"] == pr["number"] and p["head"]["sha"] == pr["head"]["sha"]
                           for p in run.get("pull_requests", []))):
            return "merge_conditions_missing"
        checks.append(check)
    if not checks:
        return "pending"
    latest = max(checks, key=lambda r: r["id"])
    if latest["status"] != "completed":
        return "pending"
    if latest["conclusion"] != "success":
        return "required_check_failed"
    reviews = client.pages(f"/pulls/{pr['number']}/reviews")
    comments = client.comments(pr["number"])
    if any(r.get("state") in {"CHANGES_REQUESTED", "PENDING"} for r in reviews):
        return "merge_conditions_missing"
    if any(re.match(r"(?:Checker|Second review): (?:CHANGES REQUIRED|BLOCKED)", c.get("body", ""))
           for c in comments):
        return "merge_conditions_missing"
    if not any(r.get("commit_id") == job["reviewed_head"]
               and r.get("user", {}).get("login") == cfg["actors"]["runner"]
               and r.get("body", "").startswith("Checker: PASS\n")
               and ("Reviewer session: " + job["review_session"]) in r["body"] for r in reviews):
        return "merge_conditions_missing"
    second = opts.get("second_reviewer")
    if opts["declared_tier"] == "domain-critical" and second:
        if not any(r.get("user", {}).get("login") == second["actor"]
                   and r.get("commit_id") == job["reviewed_head"]
                   and r.get("body", "").startswith("Second review: PASS\n")
                   and ("Provider: " + second["provider"]) in r["body"] for r in reviews):
            return "pending"
    if pr.get("draft") or pr.get("mergeable") is not True or pr.get("mergeable_state") not in {"clean", "unstable"}:
        return "pending"
    return "pass"


def advance(runner, config, path, job, client=None):
    opts = options(config)
    if not opts or job["status"] != "ready_for_integration":
        return
    runner.integration_api()  # Register fixed public reasons before persistence.
    delivery = job.setdefault("integration", {"stage": "new"})
    if delivery["stage"] in {"merged", "owner", "blocked"}:
        return
    core = runner.lifecycle_api()
    try:
        cfg = core.load_config(config["lifecycle_config"])
        client = client or client_for(core, cfg)
    except Exception:
        delivery.update(stage="blocked", reason="publication_failed")
        job.update(status="blocked", updated_at=runner.now(), blocked_reason={
            "reason_code": "publication_failed", "reason": REASONS["publication_failed"]})
        runner.save_job(config, path, job)
        return

    def persist():
        job["updated_at"] = runner.now()
        runner.save_job(config, path, job)

    def stop(reason, owner=False):
        if (delivery.get("stage") == "auto_merge_enabled" or delivery.get("intent") == "auto_merge"):
            if delivery.get("disable_attempted"):
                reason, owner = "auto_merge_disable_uncertain", True
            else:
                delivery["disable_attempted"] = True
                persist()
                try:
                    client.disable_auto_merge({"node_id": delivery["node_id"]})
                except Exception:
                    reason, owner = "auto_merge_disable_uncertain", True
        delivery.update(stage="owner" if owner else "blocked", reason=reason)
        job.update(status="needs_owner" if owner else "blocked", blocked_reason={
            "reason_code": reason, "reason": REASONS[reason]})
        persist()
        if not delivery.get("reason_notice_attempted"):
            try:
                delivery["reason_notice_attempted"] = True
                persist()
                client.request("POST", f"/issues/{job['github_number']}/comments", {
                    "body": REASONS[reason] + "\nReviewed head: " + job.get("reviewed_head", "unavailable")})
            except Exception:
                job["lifecycle_unsynced"] = True
                runner.save(path, job)
        try:
            client.notify(job["github_number"])
            if delivery.get("pr"):
                client.notify(delivery["pr"])
        except Exception:
            job["lifecycle_unsynced"] = True
            runner.save(path, job)

    def write(label, operation):
        if delivery.get("intent"):
            raise core.Uncertain("Unreconciled write intent")
        delivery["intent"] = label
        persist()
        value = operation()
        delivery.pop("intent")
        return value

    try:
        reviewed(runner, config, job)
        if delivery.get("disable_attempted"):
            stop("auto_merge_disable_uncertain", True)
            return
        second = opts.get("second_reviewer")
        if second and second["actor"] in {cfg["actors"]["runner"], cfg["actors"]["owner"]}:
            stop("merge_conditions_missing", True)
            return
        if delivery.get("intent"):
            stop("publication_uncertain")
            return
        policy_hash = runner.digest(opts)
        if delivery["stage"] == "new" and not delivery.get("options_sha256"):
            delivery["options_sha256"] = policy_hash
            persist()
        if delivery.get("options_sha256") != policy_hash:
            stop("merge_conditions_missing", True)
            return
        if delivery["stage"] == "new":
            if job.get("lifecycle_unsynced"):
                stop("merge_conditions_missing", True)
                return
            if core.admit_phase(config["lifecycle_config"], job) is not True:
                stop("merge_conditions_missing", True)
                return
            issue = client.issue(job["github_number"])
            if issue.get("body") != job["assignment"]["task_text"] or core.state(issue) != "in-review":
                stop("merge_conditions_missing", True)
                return
            write("push", lambda: client.push(runner, config, job["head_commit"]))
            delivery["stage"] = "pushed"
            persist()
        if delivery["stage"] == "pushed":
            receipt = runner.public_receipt(runner.read(
                Path(config["state_dir"]) / "receipts" / (job["id"] + ".json")))
            posted = write("receipt", lambda: client.request(
                "POST", f"/issues/{job['github_number']}/comments",
                {"body": "Sanitized task receipt\n```json\n" + json.dumps(receipt, indent=2) + "\n```"}))
            delivery["receipt_url"] = posted["html_url"]
            delivery["body"] = body_for(runner, config, job, cfg, opts, posted["html_url"])
            # Persist body before creating; never regenerate from mutable config.
            delivery["stage"] = "receipted"
            persist()
        if delivery["stage"] == "receipted":
            pr = write("create_pr", lambda: client.request("POST", "/pulls", {
                "title": f"Implement approved story #{job['github_number']}",
                "head": config["branch"], "base": cfg.get("base_branch", "main"), "body": delivery["body"]}))
            delivery.update(pr=pr["number"], node_id=pr["node_id"], stage="created")
            persist()
        pr = bound_pr(client, cfg, config, job, delivery)
        if pr.get("merged"):
            delivery["stage"] = "merged"
            persist()
            return
        if pr["state"] != "open":
            stop("merge_conditions_missing", True)
            return
        if delivery["stage"] == "created":
            write("checker", lambda: client.request("POST", f"/pulls/{pr['number']}/reviews", {
                "commit_id": job["reviewed_head"], "event": "COMMENT",
                "body": "Checker: PASS\nReviewed head: " + job["reviewed_head"]
                        + "\nReviewer session: " + job["review_session"]
                        + "\nReceipt: " + delivery["receipt_url"]}))
            delivery["stage"] = "checking"
            persist()
            pr = bound_pr(client, cfg, config, job, delivery)
        gate = load_gate()
        errors, minimum = category(client, cfg, pr, gate)
        if TIERS.index(minimum) > TIERS.index(opts["declared_tier"]):
            stop("gate_category_higher", True)
            return
        if opts["declared_tier"] == "control-critical" or release_work(runner, config, job, opts):
            stop("owner_merge_required", True)
            return
        if errors:
            stop("merge_conditions_missing", True)
            return
        evidence = merge_evidence(core, cfg, client, pr, opts, job)
        if evidence == "pending":
            if delivery["stage"] == "auto_merge_enabled":
                stop("merge_conditions_missing", True)
                return
            persist()
            return
        if evidence != "pass":
            stop(evidence, evidence == "merge_conditions_missing")
            return
        fresh = bound_pr(client, cfg, config, job, delivery)
        if snapshot(fresh) != snapshot(pr):
            stop("stale_head", True)
            return
        # Final complete recheck on the same snapshot; a new push/body requires
        # new evidence. GitHub branch protection remains the atomic merge guard.
        if merge_evidence(core, cfg, client, fresh, opts, job) != "pass":
            stop("merge_conditions_missing", True)
            return
        if delivery["stage"] == "auto_merge_enabled":
            return
        enabled = write("auto_merge", lambda: client.enable_auto_merge(fresh))
        delivery["stage"] = "auto_merge_enabled" if enabled else "direct_merge_recheck"
        persist()
        if not enabled:
            final = bound_pr(client, cfg, config, job, delivery)
            errors, minimum = category(client, cfg, final, gate)
            if (snapshot(final) != snapshot(fresh) or errors
                    or minimum == "control-critical"
                    or merge_evidence(core, cfg, client, final, opts, job) != "pass"):
                stop("merge_conditions_missing", True)
                return
            merged = write("direct_merge", lambda: client.request(
                "PUT", f"/pulls/{final['number']}/merge",
                {"sha": job["reviewed_head"], "merge_method": "merge"}))
            if merged.get("merged") is not True:
                stop("publication_failed")
                return
            delivery["stage"] = "merged"
            persist()
            write("merge_record", lambda: client.request(
                "POST", f"/issues/{final['number']}/comments", {
                    "body": "Verified direct merge of " + job["reviewed_head"]
                            + "\nReviews relied on: Checker session " + job["review_session"]
                            + "\nReceipt: " + delivery["receipt_url"]
                            + ("\nSecond reviewer: " + opts["second_reviewer"]["actor"]
                               if opts.get("second_reviewer") else "")}))
            persist()
    except core.Uncertain:
        stop("publication_uncertain")
    except EvidenceMissing:
        stop("merge_conditions_missing", True)
    except ValueError:
        stop("stale_head", True)
    except Exception:
        stop("publication_failed")
