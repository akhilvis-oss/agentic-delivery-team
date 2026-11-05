# Review categories

Review evidence should be proportional to risk. The review gate gives every pull request one of four categories, from the files it changes, and requires the matching evidence in the PR description.

## The four categories

| Category | Risk it addresses | Typical paths | Evidence the gate requires |
| --- | --- | --- | --- |
| **Light** | Little | Docs, tests not covering domain or control code, static assets | Summary with a Maker context, tests run, assumptions |
| **Standard** (default) | Ordinary defects | Application code that isn't domain- or control-critical | Light, plus an issue link, verbatim `AC-N` criteria, a three-column acceptance-evidence table, and the tested head SHA |
| **Domain-critical** | **Correctness**: the logic users rely on might be wrong | Your core calculations or decisions, and their tests | Standard, plus a domain-evidence section whose labels you configure |
| **Control-critical** | **Consequence**: a change might do something unsafe or irreversible, or loosen the review itself | Credentials, external writes, dependency locks, CI, the gate and its config, agent rules | The full contract: optional requirement IDs, an evidence matrix with Maker state and Checker verdict, three labelled security lines, scope acknowledgements and an attributed independent Checker record on the exact head |

Splitting correctness risk from consequence risk is deliberate. Without it, most real application logic lands in the top category and every change pays for full ceremony.

## How the category is decided

1. Each changed file takes the **highest** category whose patterns match it, using `fnmatch` patterns where `*` also matches `/`.
2. A file that matches nothing takes the default, which must be standard or higher, never light.
3. The PR takes the **highest** category among its files. Unrelated sensitive changes are best split into their own PR.
4. The PR declares a category in its `## Review tier` section. It may declare a higher one than required, which is then enforced, but never a lower one. When a declaration is too low, the error names up to five matching patterns from the trusted config, or indicates the configured default or unavailable paths. It never prints changed filenames.
5. With an empty or unavailable changed-file list, for example in a local run without `--changed-files`, the strictest category applies.
6. A control-critical PR that also touches domain files must supply the domain evidence too.

Tests follow the code they cover. A test-only edit to a financial test can let wrong calculations pass, so it should be domain-critical. Test infrastructure such as `conftest.py` or lint baselines can weaken every check, so it should be control-critical.

## Keeping the gate trustworthy

- **The trusted workflow uses `pull_request_target`.** It must be installed on the default branch by an owner-authorized reviewed merge. It checks out only the target base SHA's gate and config, never PR code, and has only `contents: read` and `pull-requests: read` permissions. Actions are pinned by full SHA; no dependencies, caches, artifacts or configured secrets are used. The automatic read-only GitHub token is used only to read metadata.
- **There is no bootstrap fallback.** Missing base gate/config fails. The initial installation cannot prove its own trusted execution. After merging, open a fresh test PR and confirm the **`review-gate`** job runs, then have the owner add that status check to protected branches. Here it belongs in **Protect Main**, alongside **`checks`**. The separate `ci.yml` runs lint, tests and private-content checks on PR code; it is not trusted enforcement.
- **File evidence is revision-bound.** Before collection, after collection and after validation, the API's PR snapshot must match the event's head SHA, base SHA/ref, body, count and repository identity. Every page must have the expected length, every filename must be unique, and the total must equal the count. More than GitHub's 3,000-file API limit, unavailable API data, malformed rows, missing rename origins, or drift fails closed with exit code 2. Retrigger after a concurrent edit or push.
- **Names remain data.** Paths must be repository-relative POSIX names: no absolute or drive paths, dot/parent/empty segments, backslashes or control characters. Renames include both old and new paths; deletions retain their path's risk. An empty diff takes control-critical.
- **The gate, config, workflows and tests stay control-critical.** `gate_paths` names those installed files for your project; config validation checks every listed path against the rules. Keep this inventory complete when moving or adding gate files. Rules need a nonempty reason and nonempty patterns.
- **The PR body is treated as data.** It is never interpolated into a shell. Output never echoes submitted text, file names, API responses, credentials or tracebacks.

Domain-evidence labels must each appear exactly once. Domain and labelled security evidence may say `N/A: reason`; bare `N/A` is rejected. Standard acceptance result cells start with `PASS`, `FAIL`, `XFAIL`, `SKIP` or `NOT RUN`, followed by whitespace, a colon or the end of the cell (`PASSING` is invalid).

For local structural feedback (not revision-bound enforcement):

```sh
python gate/review_gate.py event.json --config .github/review-gate.json --changed-files paths.txt
```

`paths.txt` contains one exact repository path per LF-delimited line; blank records and control characters fail. Without this file, the strictest category applies. In trusted CI, use `python -I gate/review_gate.py "$GITHUB_EVENT_PATH" --config .github/review-gate.json --github-api` from the base checkout. API mode refuses a local file list. Exit codes: 0 means structurally valid, 1 means missing/invalid review evidence, 2 means unavailable or malformed inputs/config or stale API evidence.

## Limits

- **The gate checks structure only.** It does not verify that evidence is true, that tests are sufficient, that criteria match the original issue, or that a review is authentic.
- **Merge authority stays with the owner,** ideally enforced by branch protection that requires the gate's check and a review.
- **Snapshots are point-in-time evidence.** A subsequent push or description edit needs a new run; there is no atomic lock across GitHub API calls and merge. Branch protection and owner review remain necessary.
- **Path rules are heuristic.** Add new sensitive files to the config when they're created; a Checker or the owner can always raise a PR's category.

## Configuration reference

See [`templates/review-gate.example.json`](../templates/review-gate.example.json).

| Key | Meaning |
| --- | --- |
| `repo_url` | `https://github.com/OWNER/REPO`; issue and review links must point here |
| `allowed_bases` | Branches PRs may target |
| `requirement_ids` | `null`, or `{"pattern": regex, "allowed": [ids] or null}`; when set, control-critical PRs list IDs and map each to evidence |
| `scope_acknowledgements` | Number of `SCOPE-N` checkboxes required for control-critical PRs |
| `domain_evidence` | `{"section": name, "labels": [...]}`; defaults to `Numeric verification` with `Hand-worked`, `Point-in-time`, `Missing data` |
| `gate_paths` | Object with nonempty `code`, `config`, `workflows`, `tests` lists of installed file paths. Every listed file must classify as control-critical. Defaults match this kit; adopters should explicitly configure their own inventory. |
| `tiers` | `order` (fixed), `default`, and `rules`: a list of `{tier, reason, patterns}` |

## Upgrading from v0.1

Replace the gate and workflow together through a reviewed installation on the default branch; remove the old PR-triggered review job so the trusted `review-gate` name is unambiguous. Update `gate_paths` for your actual installed files and ensure all remain control-critical. Target branches need the base gate and config; there is no PR-code fallback. Reasons and patterns are now mandatory, N/A needs a reason, duplicate domain labels fail, and result words must match exactly. The shipped example includes the new inventory. No runtime dependencies or new secrets are needed.
