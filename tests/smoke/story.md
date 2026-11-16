## Story
As a scratch repository user, I want a tiny tested addition function so that the lifecycle can prove it builds, tests, reviews and publishes a real change.

## Acceptance criteria
- AC-1: Add `smoke_add.py` with the function `add(a, b)` returning the sum of two integers.
- AC-2: Add `test_smoke_add.py` using standard-library unittest, covering positive, zero and negative integers. Run `python -m unittest -q`.

## Scope
Only add these two files. Do not change framework files, workflows, configuration or dependencies. Leave all changes uncommitted for the trusted runner. Do not merge the pull request.

## Owner decisions
This is a disposable smoke story. The expected end is a reviewed pull request stopped for the owner's merge; the harness closes it afterwards.
