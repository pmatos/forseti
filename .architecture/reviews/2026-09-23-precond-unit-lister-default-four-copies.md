# Precondition unit-lister default: one configuration seam

- **Date:** 2026-09-23
- **Candidate:** `precond-unit-lister-default-four-copies`
- **Score:** 20/25
- **Branch:** `pm-deepen/precond-unit-lister-default-four-copies`

## Prior-run reconciliation

The current base deleted `.architecture/` in `030dbf3`, so prior candidate memory was recovered from `030dbf3^:.architecture/backlog.md` and reconciled with pull requests carrying the `<!-- pm-deepen -->` marker. All earlier picked candidates were merged; no pm-deepen pull request was open. The recovered backlog named this candidate as the deterministic next pick after PR #312.

## Candidate ranking

The selected candidate remained the only 20/25 survivor. The next band tied at 19/25.

| Candidate | Leverage | Locality | Blast radius | Heat | Total |
|---|---:|---:|---:|---:|---:|
| `precond-unit-lister-default-four-copies` | 4 | 5 | 3 | 4 | **20/25** |
| `semantic-loop-mode-vocabulary` | 3 | 4 | 2 | 5 | 19/25 |
| `harness-reply-triage-two-hooks` | 4 | 4 | 2 | 3 | 19/25 |
| `renderability-authority-completes-identifier-checks` | 4 | 4 | 3 | 4 | 19/25 |
| `forseti-cli-json-subprocess-seam` | 4 | 5 | 3 | 3 | 19/25 |

### Score justification

- **Leverage 4:** four precondition entry points now consume one adapter-selection interface; both emit-only CLI faces also stop bypassing the user's timeout budget.
- **Locality 5:** production/injected unit-listing selection and its `esbmc_bin`/`timeout_s` binding move from `verify.py` and `discharge.py` into one implementation.
- **Blast radius 3:** five implementation/test files, but the correction adds a keyword-only timeout to two exported functions and changes invalid-tooling timing at both CLI faces, so the behavior matters more than the raw file count.
- **Heat 4:** the precondition and CLI paths were touched repeatedly in the last 60 commits and remain active engine paths.

## Problem

`verify_precondition`, `synthesize`, `emit_obligations`, and `discharge_precondition` each built the same `Path -> list[Unit]` adapter independently. The verified paths forwarded `timeout_s`; the emit-only paths omitted it and silently inherited `list_units`' unrelated 30-second default. Consequently `forseti synth|discharge --emit-only -t N` parsed for up to 30 seconds regardless of `N`.

The interface was shallow: each caller had to know the external process binary, timeout keyword, injected-test override, and—in discharge—the cache composition.

## Deletion test

Deleting the new seam would restore four process-configuration sites across two modules, including the timeout drift. The complexity does not disappear; it returns to every caller. The seam therefore earns its place.

## Design it twice

### Design A — minimal selector factory

Keep one package-internal `unit_lister(*, esbmc_bin, timeout_s, adapter)` in `precond.verify`. It returns the injected callable unchanged or a production closure around `esbmc.units.list_units`. Discharge composes its existing invocation-local `_memoized` wrapper outside the factory.

### Design B — immutable listing configuration

Add a private module with `UnitListingConfig`, a cache enum, and `make_unit_lister`. This centralizes configuration and cache policy, but introduces a carrier and enum for one production adapter and one discharge-only cache variant.

### Design C — caller-first plan resolution

Replace `plan_for` with `resolve_unit_plan(source, function, timeout_s, esbmc_bin, list_units_fn)`. Ordinary callers obtain their `UnitPlan` directly, but discharge still needs the underlying lister for S2/S3 reuse. More importantly, `plan_for` is exported from `forseti.precond`, so a clean replacement expands the public blast radius.

### Design D — explicit port and adapters

Add a private `_unit_lister.py` containing a callable Protocol plus production/injected adapter selection. The seam is real, but the Protocol and new module add no behavior beyond the callable type already used by every test adapter.

## Adjudication

Criteria, in order: depth, locality, seam placement, test surface, blast radius.

**Design A won.** One small interface hides the whole process-configuration decision, sits beside the precondition timeout and planning policy, stays testable through existing public entry points, and adds no new module or carrier. The implementation folds adapter selection into the factory, eliminating repeated `if adapter` setup as well as the four lambdas.

**Design C was the strongest loser.** It hides more from ordinary callers, but puts the seam above what actually varies, leaves discharge needing a second lower seam, and would replace an exported function. Designs B and D add interface weight without additional leverage. No advisor tool was available; adjudication used the fixed criteria directly.

## Implementation

- Added `precond.verify.unit_lister`, kept package-internal by omitting it from `forseti.precond.__all__`.
- Routed all four precondition entry points through it.
- Added keyword-only `timeout_s=DEFAULT_TIMEOUT_S` to `synthesize` and `emit_obligations`.
- Forwarded the CLI's existing `args.timeout` on both emit-only paths.
- Retained discharge's per-invocation, exact-`Path`, successful-result memoization around the selected adapter.
- Preserved every assessment, label, exit code, JSON/event shape, k-ladder rule, non-vacuity rule, obligation predicate, and caller-completeness policy.

The implementation diff is five files: 62 insertions and 8 deletions. No `CONTEXT.md` exists, and the change introduces no new domain term.

## Test-first evidence

The regression test exercised both real CLI faces with a fake ESBMC subprocess and `--timeout 0.125`.

- **Red:** both cases reported `timed out after 30.0 seconds`.
- **Green:** both cases reported `timed out after 0.125 seconds`.
- **Focused suites:** 72 tests passed across Core precondition CLI, S2 verification, and S3 discharge.

## Runtime smoke

Both actual commands were executed through `python -m forseti.core` with a sleeping fake ESBMC process:

- `synth --emit-only --timeout 0.05` exited 3 and reported `timed out after 0.05 seconds`.
- `discharge --emit-only --timeout 0.05` exited 3 and reported `timed out after 0.05 seconds`.

This verifies the changed user-facing path rather than only the test harness.


## Quality gate

Each command ran separately against the project dev environment:

- `ruff check src tests` — passed.
- `ruff format --check src tests` — 163 files already formatted.
- `ty check --python <scratch-venv> src tests` — passed. The system environment had no `ty`, so the declared dev dependencies were installed into a virtual environment under `$TMPDIR` and supplied explicitly to module resolution.
- `pytest -q` — 1799 passed, 1 skipped in 90.47 seconds; the ESBMC-gated tests ran because ESBMC 8.3.0 is on `PATH`.
