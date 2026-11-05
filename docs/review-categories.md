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
4. The PR declares a category in its `## Review tier` section. It may declare a higher one than required, which is then enforced, but never a lower one.
5. Without a changed-file list, for example in a local run without `--changed-files`, the strictest category applies.
6. A control-critical PR that also touches domain files must supply the domain evidence too.

Tests follow the code they cover. A test-only edit to a financial test can let wrong calculations pass, so it should be domain-critical. Test infrastructure such as `conftest.py` or lint baselines can weaken every check, so it should be control-critical.

## Keeping the gate trustworthy

- **CI runs the gate and config from the base branch,** not from the PR. A PR cannot loosen the rules applied to itself. The first PR that adds the gate is the only exception (bootstrap), and CI prints a warning when that happens.
- **The gate, its config and its tests are control-critical** in every config the kit ships.
- **The PR body is treated as data.** It is never interpolated into a shell, and errors never echo submitted text.

## Limits

- **The gate checks structure only.** It does not verify that evidence is true, that tests are sufficient, that criteria match the original issue, or that a review is authentic.
- **Merge authority stays with the owner,** ideally enforced by branch protection that requires the gate's check and a review.
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
| `tiers` | `order` (fixed), `default`, and `rules`: a list of `{tier, patterns}` |
