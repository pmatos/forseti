# Architecture Decision Records

Each ADR captures one decision, its context, and its consequences. They are immutable once
accepted — to change one, add a new ADR that supersedes it.

## Numbering

`0001`-`0009` are frozen legacy sequential numbers, hand-picked by whoever wrote the ADR.
Don't renumber or reuse a number from this range — they're cited elsewhere in the tree as
`ADR-0007` etc., and `tests/docs/test_adr_citations.py` checks those citations resolve.

New ADRs use `docs/adr/YYYY-MM-DD-slug.md` instead, dated the day the ADR is authored, and
are cited as `ADR-YYYY-MM-DD`. Two authors can't independently pick the same real-world date
and slug the way they could pick the same next integer, so there's no more numbering-collision
class for `test_no_two_records_share_a_number` to catch among new records — a genuine clash
becomes an ordinary git filename conflict instead.

| # | Decision | Status |
|---|---|---|
| [0001](0001-codename-forseti.md) | Codename: Forseti | Accepted |
| [0002](0002-scope-and-success-metric.md) | Scope & success metric | Accepted |
| [0003](0003-language-priority.md) | Language priority: C → C++ → Python | Accepted |
| [0004](0004-esbmc-fork-strategy.md) | ESBMC: fork now, upstream in batches | Accepted |
| [0005](0005-hybrid-tracking.md) | Hybrid tracking (in-repo + GitHub) | Accepted |
| [0006](0006-sequencing-gepa-before-real-code.md) | Sequence GEPA before real-code | Accepted |
| [0007](0007-lean-off-critical-path.md) | Lean stretch off the critical path | Accepted |
| [0008](0008-vow-out-of-scope.md) | Vow is out of scope | Accepted |
| [0009](0009-property-pipeline-decisions.md) | Property pipeline: store, scope, transport, verdict | Accepted |
