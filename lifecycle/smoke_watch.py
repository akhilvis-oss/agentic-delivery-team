"""Bounded automatic smoke selection in the trusted installed framework."""

import datetime as dt
from pathlib import Path
import time

from lifecycle import core, smoke


def framework_client(cfg):
    login = cfg["framework_runner"]
    return core.GitHub({"repository": cfg["framework_repository"],
                        "actors": {"product": login, "runner": login,
                                   "owner": cfg["framework_owner"]},
                        "app_auth": {"product": cfg["app_auth"]["framework_runner"]}}, "product")


def smoke_status(client, sha, agent, actor):
    """Newest matching status wins; another identity cannot suppress a run."""
    return next((row for row in client.pages(f"/commits/{sha}/statuses")
                 if row.get("context") == "smoke/" + agent
                 and row.get("creator", {}).get("login") == actor), None)


def eligible(cfg, pr):
    return (pr.get("state") == "open"
            and pr.get("user", {}).get("login") in {cfg["framework_owner"], cfg["framework_runner"]}
            and (pr.get("head", {}).get("repo") or {}).get("full_name") == cfg["framework_repository"]
            and (pr.get("base", {}).get("repo") or {}).get("full_name") == cfg["framework_repository"])


def candidates(cfg, client):
    main = client.request("GET", "/commits/main")["sha"]
    yield main, None
    for pr in sorted(client.pages("/pulls?state=open"), key=lambda p: p["number"]):
        if eligible(cfg, pr):
            yield pr["head"]["sha"], pr["number"]


def docs_only(client, baseline, sha):
    if not baseline or baseline == sha:
        return False
    diff = client.request("GET", f"/compare/{baseline}...{sha}")
    files = diff.get("files", [])
    # GitHub compare files are capped at 300. Never infer docs-only from a
    # possibly truncated list, or a comparison that did not reach the head.
    return (diff.get("status") == "ahead" and 0 < len(files) < 300
            and all(isinstance(f.get("filename"), str)
                    and all(p.startswith("docs/") or p.endswith(".md")
                            for p in [f["filename"]] + ([f.get("previous_filename", "")]
                                                       if f.get("status") == "renamed" else []))
                    for f in files))


def publish_result(cfg, client, result):
    marker = "<!-- agentic-last-smoke-v1 -->"
    issues = [i for i in client.pages("/issues?state=open")
              if i.get("user", {}).get("login") == cfg["framework_runner"]
              and "<!-- agentic-service-status-v1 -->" in (i.get("body") or "")]
    number = (max(issues, key=lambda i: i["number"])["number"] if issues else
              client.request("POST", "/issues", {"title": "Lifecycle service status",
                  "body": "Lifecycle service results.\n<!-- agentic-service-status-v1 -->"})["number"])
    comments = [c for c in client.comments(number)
                if c.get("user", {}).get("login") == cfg["framework_runner"]
                and marker in c.get("body", "")]
    body = (f"Last smoke: {result['sha']}; {result['state']}; {result['description']}\n" + marker)
    if comments:
        client.request("PATCH", f"/issues/comments/{comments[0]['id']}", {"body": body})
    else:
        client.request("POST", f"/issues/{number}/comments", {"body": body})


def watch(cfg, client, *, clock=time.time, run=None, status_client=None):
    run = run or smoke.run
    statuses = status_client or client
    root = Path(cfg["lock_dir"])
    key = core.key([cfg["framework_repository"], cfg["repository"], cfg["agent_name"]])
    with core.lock(root / (key + "-watch.lock")) as acquired:
        if not acquired:
            return "locked"
        path = root / (key + "-watch.json")
        journal = core.read(path) if path.exists() else {"attempts": {}}
        # A result is durable before any status write. Never replay a lost or
        # ambiguous status POST; reconcile it manually instead of rerunning code.
        if journal.get("result_pending"):
            publish_result(cfg, client, journal["last_result"])
            journal["result_pending"] = False
            core.save(path, journal)
        day = dt.datetime.fromtimestamp(clock(), dt.timezone.utc).date().isoformat()
        if journal.get("day") != day:
            journal.update(day=day, count=0)
        if journal["count"] >= cfg.get("daily_cap", 12):
            return "capped"
        for sha, number in candidates(cfg, client):
            if not isinstance(sha, str) or not core.SHA.fullmatch(sha):
                raise core.Unavailable("Invalid candidate commit")
            status = smoke_status(statuses, sha, cfg["agent_name"], cfg["framework_runner"])
            if status and status.get("state") == "success" and not journal.get("last_passed"):
                journal["last_passed"] = sha
                core.save(path, journal)
            if sha in journal["attempts"] or status:
                continue
            if number is not None:
                fresh = client.request("GET", f"/pulls/{number}")
                if not eligible(cfg, fresh) or fresh["head"]["sha"] != sha:
                    continue
            baseline = journal.get("last_passed")
            # Recheck the baseline status: manual reruns may have failed since.
            baseline_status = (smoke_status(statuses, baseline, cfg["agent_name"], cfg["framework_runner"])
                               if baseline else None)
            skip = bool(baseline_status and baseline_status.get("state") == "success"
                        and docs_only(client, baseline, sha))
            journal["attempts"][sha] = "reserved"
            journal["count"] += 1
            core.save(path, journal)

            def completed(commit, state, description):
                if commit != sha:
                    raise core.Unavailable("Candidate changed")
                journal["attempts"][sha] = "completed"
                journal["last_result"] = {"sha": sha, "state": state, "description": description}
                journal["result_pending"] = True
                if state == "success":
                    journal["last_passed"] = sha
                core.save(path, journal)

            if skip:
                description = f"PASS; docs-only; passed commit {baseline}; stage=owner-merge."
                completed(sha, "success", description)
                (status_client or smoke.status_client(cfg)).request("POST", "/statuses/" + sha, {
                    "state": "success", "context": "smoke/" + cfg["agent_name"], "description": description})
            else:
                run(cfg, sha, completed=completed)
                if journal["attempts"][sha] == "reserved":
                    # A normal return without completion means no run started.
                    del journal["attempts"][sha]
                    journal["count"] -= 1
                    core.save(path, journal)
                    return "not-started"
            publish_result(cfg, client, journal["last_result"])
            journal["result_pending"] = False
            core.save(path, journal)
            return "completed"
        return "idle"


def main(config_path):
    try:
        cfg = smoke.load_config(config_path)
        if not {"framework_owner", "framework_runner"} <= cfg.keys():
            raise ValueError("Watcher requires framework identities")
        print(watch(cfg, framework_client(cfg), status_client=smoke.status_client(cfg)))
        return 0
    except Exception:
        print("Smoke watcher stopped: configuration or GitHub prerequisite unavailable.")
        return 1
