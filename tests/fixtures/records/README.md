# Lifecycle record shapes

`record-shapes-413.json` contains generic, sanitised shapes reconstructed from
413 parseable records among the 457 public lifecycle records examined for #175
at baseline `a1ccc41` on 20 November 2026. It is test data, not a live API capture.
Repository, actors, issue and event numbers, jobs, commits, timestamps and public
text use fixture values; record keys were recomputed from those values. The
remaining 44 records are excluded, including the guard-record defect tracked
separately in #192. All 413 shapes also parse unchanged at the build base.

Keep these fixtures outside `tests/fixtures/github/`, whose capture inventory
has a separate contract. Tests use the synthetic policy in `test_lifecycle`.
