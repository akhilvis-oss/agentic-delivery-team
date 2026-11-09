"""Repository-grounded Product clarifications; never a story/scope approval path."""

from __future__ import annotations

import argparse
import base64
import fnmatch
import hashlib
import json
from pathlib import Path
import urllib.parse

from lifecycle import core

OWNER_DECISIONS = {"scope_change", "acceptance_criteria_change", "story_approval"}


def evidence(cfg, client, citations):
    if not isinstance(citations, list) or not 1 <= len(citations) <= 5:
        raise ValueError("One to five repository citations required")
    rendered = []
    for citation in citations:
        if not isinstance(citation, dict) or set(citation) != {
            "path",
            "ref",
            "start",
            "end",
            "quote",
        }:
            raise ValueError("Exact repository citation fields required")
        path, ref, start, end, quote = (
            citation[k] for k in ("path", "ref", "start", "end", "quote")
        )
        if (
            not isinstance(path, str)
            or path.startswith("/")
            or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or not any(
                fnmatch.fnmatchcase(path, pattern)
                for pattern in cfg["product_policy"]["clarification_source_globs"]
            )
            or not isinstance(ref, str)
            or not core.SHA.fullmatch(ref)
            or type(start) is not int
            or type(end) is not int
            or not 1 <= start <= end <= start + 11
            or not isinstance(quote, str)
            or not quote.strip()
            or len(quote) > 2000
        ):
            raise ValueError("Invalid or unapproved repository citation")
        source = client.request(
            "GET", "/contents/" + urllib.parse.quote(path, safe="/") + "?ref=" + ref
        )
        if source.get("type") != "file" or source.get("encoding") != "base64":
            raise ValueError(
                "Only complete repository text files are clarification sources"
            )
        raw = base64.b64decode("".join(source["content"].split()), validate=True)
        blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        if blob != source["sha"]:
            raise ValueError("Repository source integrity mismatch")
        lines = raw.decode().splitlines()
        if end > len(lines) or quote != "\n".join(lines[start - 1 : end]):
            raise ValueError(
                "Clarification is not supported by its repository citation"
            )
        url = f"https://github.com/{cfg['repository']}/blob/{ref}/{urllib.parse.quote(path, safe='/')}#L{start}-L{end}"
        rendered.append(
            "\n".join("> " + line for line in quote.splitlines())
            + f"\n\n[Repository source]({url})"
        )
    return "\n\n".join(rendered)


def answer(cfg, request, client):
    if client.role != "product" or not core.has_role(
        cfg, cfg["actors"]["product"], "product"
    ):
        raise ValueError("Product publishing identity required")
    if (
        not isinstance(request, dict)
        or set(request) - {"id", "issue", "kind", "citations"}
        or not core.ID.fullmatch(request.get("id", ""))
        or type(request.get("issue")) is not int
        or request["issue"] < 1
    ):
        raise ValueError(
            "Structured Product request required; comments are never commands"
        )
    kind = request.get("kind")
    if kind in OWNER_DECISIONS:
        decision = kind
        body = None
    elif kind == "clarification":
        try:
            body = evidence(cfg, client, request.get("citations"))
            decision = None
        except (
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            UnicodeError,
            core.Unavailable,
        ):
            decision, body = "clarification_unverified", None
    else:
        decision, body = "product_request_unclassified", None
    current = core.state(client.issue(request["issue"]))
    record = core.public_record(
        cfg,
        request["issue"],
        request["id"],
        0,
        "needs-owner" if decision else current,
        kind="transition" if decision else "clarification",
        reason=decision or "repository_evidence",
    )
    public_body = None
    if body is not None:
        record.update({"from": current, "actor": cfg["actors"]["product"]})
        public_body = (
            "Repository evidence only; no scope, criteria or approval decision is made.\n\n"
            + body
            + "\n\n"
            + core.record_body(record)
        )
    path = core.queue(cfg, record, role="product", public_body=public_body)
    core.flush(cfg, client)
    return {
        "route": "owner" if decision else "repository_clarification",
        "status": core.read(path)["status"],
        "issue": request["issue"],
        "key": record["key"],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args(argv)
    cfg = core.load_config(args.config)
    print(json.dumps(answer(cfg, core.read(args.request), core.GitHub(cfg, "product"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
