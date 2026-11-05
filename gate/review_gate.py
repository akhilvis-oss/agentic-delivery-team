"""Review gate: checks a PR description's structure against its review category.

Four categories, lowest to highest: light, standard, domain-critical and
control-critical. The minimum category comes from the PR's changed file paths
(rules in the project's review-gate config); a PR may declare a higher
category but never a lower one. Without a changed-file list the strictest
category applies.

This is a structural check only. It does not prove that evidence is true, that
tests are sufficient, or that a review is authentic, and it confers no merge
authority. It reads the PR body as data and never executes it.

Usage (trusted CI, from the base checkout):
    python -I review_gate.py EVENT_JSON --config .github/review-gate.json --github-api

Local structural feedback (not revision-bound):
    python review_gate.py EVENT_JSON --config .github/review-gate.json \
        --changed-files CHANGED_FILES_TXT
"""

import argparse
import fnmatch
import json
import os
from pathlib import Path
import re
import unicodedata
import urllib.request

COMMON_SECTIONS = ("Review tier", "Maker summary", "Test evidence", "Assumptions")
STANDARD_SECTIONS = ("Original acceptance criteria", "Acceptance evidence", "Security review")
CONTROL_SECTIONS = (
    "Original acceptance criteria", "Acceptance and evidence matrix", "Security review",
    "Risk and scope", "Independent Checker",
)
# Default domain-critical evidence (financial projects); override with
# "domain_evidence": {"section": ..., "labels": [...]} in the config.
DEFAULT_DOMAIN_EVIDENCE = {"section": "Numeric verification",
                           "labels": ["Hand-worked", "Point-in-time", "Missing data"]}
SECURITY_LABELS = ("Secrets", "External access/cost", "Data/provenance")
RESULTS = ("PASS", "FAIL", "XFAIL", "SKIP", "NOT RUN")
MAKER_STATES = {"NOT VERIFIED", "IMPLEMENTED", "VERIFIED", "BLOCKED"}
VERDICTS = {"PENDING", "PASS", "CHANGES REQUIRED", "BLOCKED"}
DEFAULT_CONFIG = Path(".github/review-gate.json")
DEFAULT_GATE_PATHS = {
    "code": ["gate/review_gate.py"],
    "config": [".github/review-gate.json"],
    "workflows": [".github/workflows/ci.yml", ".github/workflows/pr-review-gate.yml"],
    "tests": ["tests/test_review_gate.py"],
}
API_LIMIT = 3000
PAGE_SIZE = 100
STATUSES = {"added", "removed", "modified", "renamed", "copied", "changed", "unchanged"}
SHA = re.compile(r"[0-9a-f]{40}")


# ---------- configuration ----------

def load_config(path=DEFAULT_CONFIG):
    """Load and sanity-check a review-gate config; raise ValueError if invalid."""
    config = json.loads(Path(path).read_text())
    tiers = config["tiers"]
    order = tiers["order"]
    if order != ["light", "standard", "domain-critical", "control-critical"]:
        raise ValueError("tiers.order must be light, standard, domain-critical, control-critical")
    if tiers["default"] not in order or tiers["default"] == "light":
        raise ValueError("tiers.default must be a known category other than light")
    if not isinstance(tiers["rules"], list) or not tiers["rules"]:
        raise ValueError("tiers.rules must be a nonempty list")
    for rule in tiers["rules"]:
        if (not isinstance(rule, dict) or rule.get("tier") not in order
                or not isinstance(rule.get("reason"), str) or not rule["reason"].strip()
                or not isinstance(rule.get("patterns"), list) or not rule["patterns"]
                or any(not isinstance(p, str) or not p.strip() for p in rule["patterns"])):
            raise ValueError("Each rule needs a known category, reason and nonempty patterns")
    if not re.fullmatch(r"https://github\.com/[\w.-]+/[\w.-]+", config["repo_url"]):
        raise ValueError("repo_url must be https://github.com/OWNER/REPO")
    if (not isinstance(config.get("allowed_bases"), list) or not config["allowed_bases"]
            or any(not isinstance(base, str) or not base.strip() for base in config["allowed_bases"])):
        raise ValueError("allowed_bases must list at least one nonempty branch")
    req = config.get("requirement_ids")
    if req is not None:
        re.compile(req["pattern"])
        if req.get("allowed") is not None and not all(
                re.fullmatch(req["pattern"], item) for item in req["allowed"]):
            raise ValueError("requirement_ids.allowed must match requirement_ids.pattern")
    if (type(config.get("scope_acknowledgements", 0)) is not int
            or config.get("scope_acknowledgements", 0) < 0):
        raise ValueError("scope_acknowledgements must be an integer zero or more")
    evidence = config.setdefault("domain_evidence", DEFAULT_DOMAIN_EVIDENCE)
    if (not isinstance(evidence.get("section"), str) or not evidence["section"].strip()
            or not isinstance(evidence.get("labels"), list) or not evidence["labels"]
            or any(not isinstance(label, str) or not label.strip() for label in evidence["labels"])
            or len(evidence["labels"]) != len(set(evidence["labels"]))):
        raise ValueError("domain_evidence needs a section and unique nonempty labels")
    protected = config.setdefault("gate_paths", DEFAULT_GATE_PATHS)
    if not isinstance(protected, dict) or set(protected) != set(DEFAULT_GATE_PATHS):
        raise ValueError("gate_paths must list code, config, workflows and tests")
    for paths in protected.values():
        if not isinstance(paths, list) or not paths:
            raise ValueError("Each gate_paths group must be a nonempty list")
        if any(file_tier(checked_path(path), config) != "control-critical" for path in paths):
            raise ValueError("Gate code, config, workflows and tests must remain control-critical")
    return config


def checked_path(path):
    """Validate repository-relative POSIX names without normalizing untrusted data."""
    if (not isinstance(path, str) or not path or path.startswith("/")
            or re.match(r"^[A-Za-z]:", path) or "\\" in path
            or any(unicodedata.category(c) == "Cc" for c in path)
            or any(part in {"", ".", ".."} for part in path.split("/"))):
        raise ValueError("Invalid repository-relative path")
    return path


def evidence_line(text, label):
    lines = re.findall(r"^" + re.escape(label) + r":[ \t]*([^\n]*)$", text, re.M)
    if len(lines) != 1 or not lines[0].strip():
        return False
    value = lines[0].strip()
    match = re.match(r"^N/A(?=$|[\s:;,.—–-])", value, re.I)
    return not match or bool(value[match.end():].strip(" \t:;,.—–-"))


def file_tier(path, config):
    """Highest category whose patterns match this path (fnmatch; * spans /)."""
    order = config["tiers"]["order"]
    matched = matching_tiers(path, config)
    return max(matched, key=order.index) if matched else config["tiers"]["default"]


def matching_tiers(path, config):
    path = checked_path(path)
    return [rule["tier"] for rule in config["tiers"]["rules"]
            if any(fnmatch.fnmatchcase(path, pattern) for pattern in rule["patterns"])]


def required_tiers(changed_files, config):
    """Return (minimum category, categories present, files forcing the minimum)."""
    order = config["tiers"]["order"]
    if changed_files is None or changed_files == []:
        return order[-1], {order[-1]}, ["changed-file list empty or unavailable"]
    if not isinstance(changed_files, (list, tuple)):
        raise ValueError("Changed files must be a list of repository paths")
    if not changed_files:
        return order[-1], {order[-1]}, ["changed-file list empty"]
    per_file = {path: matching_tiers(path, config) or [config["tiers"]["default"]]
                for path in changed_files}
    present = {tier for tiers in per_file.values() for tier in tiers}
    minimum = max(present, key=order.index)
    return minimum, present, sorted(p for p, tiers in per_file.items() if minimum in tiers)


# ---------- parsing helpers ----------

def split_sections(pr):
    body = re.sub(r"<!--.*?-->", "", pr.get("body") or "", flags=re.S)
    chunks = re.split(r"^## (.+?)\s*$", body, flags=re.M)
    names = chunks[1::2]
    return names, dict(zip(names, chunks[2::2]))


def labelled(text, label):
    return re.search(r"^" + re.escape(label) + r":[ \t]*(\S[^\n]*)$", text, flags=re.M)


def table_rows(text, columns, errors, message):
    rows = []
    for line in text.splitlines():
        if re.match(r"^\|\s*AC-", line):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) != columns or not all(cells):
                errors.append(message)
                continue
            rows.append(cells)
    return rows


def acceptance_ids(original, config, errors):
    if not re.search(re.escape(config["repo_url"]) + r"/issues/[1-9]\d*\b", original):
        errors.append("Original criteria need an issue URL in this repository")
    criteria = re.findall(r"^- (AC-[1-9]\d*):[ \t]*(\S[^\n]*)$", original, flags=re.M)
    ids = [key for key, _ in criteria]
    if not ids or len(ids) != len(set(ids)):
        errors.append("Original criteria need unique, nonempty AC-N entries")
    if len(re.findall(r"^- AC-", original, flags=re.M)) != len(criteria):
        errors.append("Malformed or empty original criterion")
    return ids


def check_tested_head(pr, sections, errors):
    head = pr.get("head", {}).get("sha", "")
    tested = re.search(r"^Tested head:\s*`?([0-9a-f]{40})`?\s*$",
                       sections.get("Test evidence", ""), flags=re.M)
    if not re.fullmatch(r"[0-9a-f]{40}", head) or not tested or tested[1] != head:
        errors.append("Tested head must equal the current full PR head SHA")
    return head


def requirement_ids(text, config):
    pattern = config["requirement_ids"]["pattern"]
    return {match.group(0) for match in re.finditer(rf"\b(?:{pattern})\b", text)}


# ---------- category checks ----------

def standard_checks(pr, sections, config, errors):
    criterion_ids = acceptance_ids(sections.get("Original acceptance criteria", ""), config, errors)
    rows = table_rows(sections.get("Acceptance evidence", ""), 3, errors,
                      "Acceptance evidence rows need three nonempty columns")
    for row in rows:
        if not re.match(r"^(?:PASS|FAIL|XFAIL|SKIP|NOT RUN)(?=$|[\s:])", row[2], re.I):
            errors.append("Acceptance result must start with " + ", ".join(RESULTS))
    if sorted(row[0] for row in rows) != sorted(criterion_ids):
        errors.append("Acceptance evidence must cover every original AC exactly once")
    check_tested_head(pr, sections, errors)


def domain_checks(sections, config, errors):
    evidence = config["domain_evidence"]
    text = sections.get(evidence["section"], "")
    for label in evidence["labels"]:
        if not evidence_line(text, label):
            errors.append(f"{evidence['section']} needs {label} exactly once (evidence, or N/A with reason)")


def control_checks(pr, sections, config, errors):
    req = config.get("requirement_ids")
    declared = set()
    if req is not None:
        text = sections.get("Requirement IDs", "")
        declared = requirement_ids(text, config)
        allowed = set(req["allowed"]) if req.get("allowed") is not None else None
        if not declared or (allowed is not None and declared - allowed):
            errors.append("List explicit, valid requirement IDs")
        if re.search(rf"(?:{req['pattern']})\s*(?:\.\.|[-–])\s*(?:(?:{req['pattern']})|\d+)", text):
            errors.append("Requirement ranges are not explicit mappings")
    criterion_ids = acceptance_ids(sections.get("Original acceptance criteria", ""), config, errors)
    columns = 7 if req is not None else 6
    rows = table_rows(sections.get("Acceptance and evidence matrix", ""), columns, errors,
                      f"Evidence matrix rows need all {columns} nonempty columns")
    state_col, verdict_col = columns - 2, columns - 1
    for row in rows:
        if req is not None:
            row_ids = requirement_ids(row[1], config)
            if not row_ids or row_ids - declared:
                errors.append("Each evidence row must map declared requirement IDs")
        if row[state_col] not in MAKER_STATES:
            errors.append("Maker state must distinguish implemented from verified")
        if row[verdict_col] not in VERDICTS:
            errors.append("Each evidence row needs an explicit Checker verdict/state")
    if sorted(row[0] for row in rows) != sorted(criterion_ids):
        errors.append("Evidence matrix must cover every original AC exactly once")
    if req is not None:
        mapped = set().union(*(requirement_ids(row[1], config) for row in rows))
        if mapped != declared:
            errors.append("Every declared requirement ID needs a criterion/evidence mapping")
    head = check_tested_head(pr, sections, errors)
    security = sections.get("Security review", "")
    for label in SECURITY_LABELS:
        if not evidence_line(security, label):
            errors.append(f"Security review needs {label}")
    scope = sections.get("Risk and scope", "")
    for number in range(1, int(config.get("scope_acknowledgements", 0)) + 1):
        if not re.search(rf"^- \[[xX]\] SCOPE-{number}:\s*\S", scope, flags=re.M):
            errors.append(f"Scope acknowledgement SCOPE-{number} incomplete")
    if not scope.strip():
        errors.append("Risk and scope needs content")
    checker_record(pr, sections, rows, verdict_col, head, config, errors)


def checker_record(pr, sections, rows, verdict_col, head, config, errors):
    checker = sections.get("Independent Checker", "")
    verdict = labelled(checker, "Verdict")
    state = verdict[1].strip() if verdict else ""
    if state not in VERDICTS:
        errors.append("Checker state must be PENDING, PASS, CHANGES REQUIRED or BLOCKED")
        return
    if state == "PENDING":
        if any(row[verdict_col] != "PENDING" for row in rows):
            errors.append("Pending independent review cannot contain completed row verdicts")
        return
    maker = labelled(sections.get("Maker summary", ""), "Maker context")
    actor = re.search(r"^Checker:\s*@([A-Za-z0-9-]+)\s*$", checker, flags=re.M)
    context = labelled(checker, "Checker context")
    if (not actor or not context or not maker
            or context[1].strip().lower() in {"not assigned", maker[1].strip().lower()}):
        errors.append("Completed verdict needs an attributed Checker and distinct review context")
    if not re.search(r"^Reviewed head:\s*`?" + re.escape(head) + r"`?\s*$", checker, flags=re.M):
        errors.append("Completed verdict must reference the current head")
    number = pr.get("number")
    link = re.escape(config["repo_url"] + f"/pull/{number}#pullrequestreview-") + r"\d+\b"
    if not isinstance(number, int) or number < 1 or not re.search(link, checker):
        errors.append("Completed verdict needs an attributed GitHub review link")
    if state == "PASS" and any(row[verdict_col] != "PASS" for row in rows):
        errors.append("Overall PASS requires per-criterion PASS")


# ---------- entry points ----------

def validate(pr, changed_files, config):
    """Return field-level errors; never echo the submitted body."""
    order = config["tiers"]["order"]
    errors = []
    if pr.get("base", {}).get("ref") not in config["allowed_bases"]:
        errors.append("Target must be one of: " + ", ".join(config["allowed_bases"]))
    names, sections = split_sections(pr)
    if len(names) != len(set(names)):
        errors.append("Duplicate section headings")

    minimum, present, _ = required_tiers(changed_files, config)
    match = labelled(sections.get("Review tier", ""), "Tier")
    declared = match[1].strip().lower() if match else ""
    if declared not in order:
        errors.append("Review tier must declare Tier: " + " | ".join(order))
        declared = minimum
    elif order.index(declared) < order.index(minimum):
        errors.append(f"Declared tier {declared} is below required {minimum}")
        declared = minimum
    domain = "domain-critical" in present or declared == "domain-critical"

    required = list(COMMON_SECTIONS)
    if declared == "control-critical":
        required += list(CONTROL_SECTIONS)
        if config.get("requirement_ids") is not None:
            required.append("Requirement IDs")
    elif declared in ("standard", "domain-critical"):
        required += list(STANDARD_SECTIONS)
    if domain:
        required.append(config["domain_evidence"]["section"])
    for heading in dict.fromkeys(required):
        if not sections.get(heading, "").strip():
            errors.append(f"Missing section/content: {heading}")
        elif "REQUIRED:" in sections[heading]:
            errors.append(f"Unfilled REQUIRED: template marker in {heading}")
    if not labelled(sections.get("Maker summary", ""), "Maker context"):
        errors.append("Maker summary needs a task/person context reference")

    if declared == "control-critical":
        control_checks(pr, sections, config, errors)
    elif declared in ("standard", "domain-critical"):
        standard_checks(pr, sections, config, errors)
    if domain:
        domain_checks(sections, config, errors)
    return list(dict.fromkeys(errors))


def read_changed_files(path):
    if path is None:
        return None
    # Only LF is a record delimiter. Never normalize or silently discard a path.
    text = Path(path).read_bytes().decode("utf-8")
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return [checked_path(line) for line in lines]


# ---------- trusted GitHub metadata ----------

def snapshot(pr, config):
    """Bind evidence and paths to one head/base/body/count and repository identity."""
    repository = config["repo_url"].removeprefix("https://github.com/")
    if (type(pr) is not dict or type(pr.get("number")) is not int or pr["number"] < 1
            or type(pr.get("changed_files")) is not int
            or not 0 <= pr["changed_files"] <= API_LIMIT
            or pr.get("body") is not None and not isinstance(pr["body"], str)):
        raise ValueError("Invalid or unsupported PR metadata")
    head, base = pr["head"], pr["base"]
    if (not isinstance(head["sha"], str) or not SHA.fullmatch(head["sha"])
            or not isinstance(base["sha"], str) or not SHA.fullmatch(base["sha"])
            or base["ref"] not in config["allowed_bases"]
            or base["repo"]["full_name"] != repository
            or not isinstance(head["repo"]["full_name"], str)
            or not re.fullmatch(r"[\w.-]+/[\w.-]+", head["repo"]["full_name"])):
        raise ValueError("Invalid PR binding")
    return (pr["number"], head["sha"], base["sha"], base["ref"],
            head["repo"]["full_name"], pr["changed_files"], pr.get("body"))


def evaluate(event, api, config):
    """Reject incomplete, duplicate, over-limit or drifting API evidence."""
    repository = config["repo_url"].removeprefix("https://github.com/")
    if event["repository"]["full_name"] != repository:
        raise ValueError("Unsupported event repository")
    pr = event["pull_request"]
    expected = snapshot(pr, config)
    prefix = f"/repos/{repository}/pulls/{expected[0]}"

    def unchanged():
        if snapshot(api(prefix), config) != expected:
            raise ValueError("PR changed; trigger a fresh check")

    unchanged()
    count, seen, paths = expected[5], set(), []
    # Even an empty diff must have an empty API response, then takes strictest tier.
    for page in range(1, max(1, (count + PAGE_SIZE - 1) // PAGE_SIZE) + 1):
        rows = api(f"{prefix}/files?per_page={PAGE_SIZE}&page={page}")
        if type(rows) is not list or len(rows) != min(PAGE_SIZE, count - len(seen)):
            raise ValueError("Incomplete changed-file response")
        for row in rows:
            if type(row) is not dict or row.get("status") not in STATUSES:
                raise ValueError("Malformed changed-file row")
            name = checked_path(row.get("filename"))
            if name in seen:
                raise ValueError("Duplicate changed-file response")
            seen.add(name)
            paths.append(name)
            if row["status"] == "renamed" and not row.get("previous_filename"):
                raise ValueError("Rename lacks original path")
            if "previous_filename" in row:
                paths.append(checked_path(row["previous_filename"]))
    if len(seen) != count:
        raise ValueError("Incomplete changed-file response")
    unchanged()
    errors = validate(pr, paths, config)
    unchanged()  # Detect a body edit, base advance or force-push during validation.
    minimum, _, _ = required_tiers(paths, config)
    return errors, {"head_sha": expected[1], "base_sha": expected[2],
                    "changed_files": count, "minimum_tier": minimum}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Unexpected API redirect")


def github_api(path):
    token = os.environ.get("GH_TOKEN")
    if not token:
        raise ValueError("Read-only GitHub token unavailable")
    request = urllib.request.Request("https://api.github.com" + path, headers={
        "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
        return json.load(response)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("event", type=Path, help="GitHub pull_request event JSON")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="review-gate config JSON")
    parser.add_argument("--changed-files", type=Path, default=None,
                        help="newline-separated changed paths; omitted means strictest category")
    parser.add_argument("--github-api", action="store_true",
                        help="collect complete revision-bound metadata using a read-only token")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        event = json.loads(args.event.read_text())
        if args.github_api:
            if args.changed_files is not None:
                raise ValueError("API mode cannot accept an unbound file list")
            errors, receipt = evaluate(event, github_api, config)
            minimum = receipt["minimum_tier"]
            print("INFO: revision-bound metadata " + json.dumps(receipt, sort_keys=True))
        else:
            changed = read_changed_files(args.changed_files)
            minimum, _, _ = required_tiers(changed, config)
            errors = validate(event["pull_request"], changed, config)
    except Exception:
        # No untrusted PR text, API response, credential or traceback reaches logs.
        print("FAIL: malformed or unavailable event, changed-file list or review-gate config")
        return 2
    print(f"INFO: minimum review category from changed paths: {minimum}")
    for error in errors:
        print(f"FAIL: {error}")
    if not errors:
        print("PASS: PR structure; independent review and owner merge authority still required")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
