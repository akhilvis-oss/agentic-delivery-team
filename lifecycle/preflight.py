"""One recoverable, fixed comment per App on an explicitly selected test issue."""

import argparse
import json
from pathlib import Path

from lifecycle import app_tokens, core


def run(cfg, issue):
    if type(issue) is not int or issue < 1:
        raise ValueError("Explicit positive test issue required")
    root = core.private(Path(cfg["state_dir"]) / "identity-check")
    results = []
    with core.lock(root / "check.lock") as acquired:
        if not acquired:
            raise core.Unavailable("Identity check already running")
        for role in ("product", "runner"):
            token, _ = app_tokens.mint(cfg, role, comments_only=True)
            client = core.GitHub(cfg, role, token=token)
            client.ensure_actor()
            target = client.issue(issue)
            if "pull_request" in target or target["state"] != "open":
                raise core.Unavailable("Identity test target must be an open issue")
            identity = core.key([cfg["repository"], issue, role, cfg["actors"][role]])
            marker = "<!-- agentic-app-identity-v1:" + identity + " -->"
            body = (
                "App identity preflight: PASS ("
                + role
                + "). No task transition requested.\n\n"
                + marker
            )
            path = root / (identity + ".json")
            prior = core.read(path) if path.exists() else {}
            existing = [
                c
                for c in client.comments(issue)
                if c.get("user", {}).get("login") == cfg["actors"][role]
                and c.get("body") == body
            ]
            if len(existing) > 1:
                raise core.Unavailable(
                    "Duplicate identity receipts require reconciliation"
                )
            if existing:
                comment = existing[0]
            elif prior.get("attempted"):
                raise core.Unavailable(
                    "Identity comment outcome unknown; reconcile without retrying"
                )
            else:
                core.save(path, {"attempted": True, "role": role, "issue": issue})
                comment = client.request(
                    "POST", f"/issues/{issue}/comments", {"body": body}
                )
            if comment.get("user", {}).get("login") != cfg["actors"][role]:
                raise core.Unavailable(
                    "Identity comment attribution mismatch: "
                    + str(comment.get("user", {}).get("login"))
                )
            result = {
                "role": role,
                "identity": cfg["actors"][role],
                "preflight": "PASS",
                "comment": f"https://github.com/{cfg['repository']}/issues/{issue}#issuecomment-{comment['id']}",
            }
            core.save(path, {"attempted": True, **result})
            results.append(result)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--issue", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        result = run(core.load_config(args.config), args.issue)
    except (core.Unavailable, app_tokens.AuthenticationError) as error:
        print(json.dumps({"status": "STOPPED", "reason": str(error)}))
        return 1
    except Exception:
        # Never expose HTTP/subprocess bodies or tracebacks from secret handling.
        print(json.dumps({"status": "STOPPED", "reason": "Identity check unavailable"}))
        return 1
    print(json.dumps({"status": "PASS", "checks": result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
