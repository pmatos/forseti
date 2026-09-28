# Semantic-loop mode vocabulary: one Core parser

- **Date:** 2026-09-25
- **Candidate:** `semantic-loop-mode-vocabulary`
- **Score:** 19/25
- **Branch:** `sym/forseti/routine/refactor-audit/01M3ATD8F2`

## Prior-run reconciliation

The current base still contains only the latest architecture review, not the historical backlog. Candidate memory was recovered from `030dbf3^:.architecture/backlog.md` and reconciled with pull requests carrying the `<!-- pm-deepen -->` marker. PR #314 merged the only 20/25 survivor, every earlier picked candidate was merged, and no pm-deepen pull request was open. The recovered backlog therefore made `semantic-loop-mode-vocabulary` the deterministic next pick.

## Candidate ranking

The next band tied at 19/25. The existing ranking and tie-break order were preserved rather than rescored.

| Candidate | Leverage | Locality | Blast radius | Heat | Total |
|---|---:|---:|---:|---:|---:|
| `semantic-loop-mode-vocabulary` | 3 | 4 | 2 | 5 | **19/25** |
| `harness-reply-triage-two-hooks` | 4 | 4 | 2 | 3 | 19/25 |
| `renderability-authority-completes-identifier-checks` | 4 | 4 | 3 | 4 | 19/25 |
| `forseti-cli-json-subprocess-seam` | 4 | 5 | 3 | 3 | 19/25 |

### Score justification

- **Leverage 3:** two transport faces and Core now share one vocabulary boundary; the semantic loop itself remains deliberately small.
- **Locality 4:** accepted spellings and normalization now live next to `LoopMode`, while CLI and MCP retain only their transport-specific presentation and error handling.
- **Blast radius 2:** three implementation files, two behavior tests, and one existing design document changed; no exported command, payload field, or canonical value changed.
- **Heat 5:** the semantic-loop CLI, MCP tool, and Core orchestrator are active, recently added paths whose duplicated vocabulary would otherwise drift as modes evolve.

The tie-break first favored the two blast-radius-2 candidates, then the higher-heat semantic-loop candidate over `harness-reply-triage-two-hooks`.

## Problem

`CoreSemanticLoop.run` used canonical `LoopMode` values (`propose`, `submit`, `check_only`), but both transport faces independently encoded the valid vocabulary:

- the CLI declared `propose|submit|check-only`, then normalized every hyphen with `replace("-", "_")` and asserted the result through `cast`;
- the MCP tool declared `propose|submit|check_only`, repeated membership validation, then assigned the raw string to a typed local.

That made the Core contract shallow. Each adapter needed to know both the accepted spellings and the canonical representation, neither adapter was checked by a real parser, and the two public faces rejected each other's equivalent `check-only`/`check_only` spelling.

## Deletion test

Deleting the parser would restore vocabulary ownership and unchecked normalization to both transport adapters. The complexity would not disappear: every current and future caller would again need to duplicate aliases, canonicalization, and validation. The parser therefore earns its place at the Core boundary.

## Design it twice

### Design A — minimal Core codec

Keep `LoopMode` as a `Literal`, add one shared choices tuple and one explicit `parse_loop_mode` function beside it, and route both adapters through the function. Preserve the raw CLI spelling in the existing command event and expose only the canonical value in semantic-loop results.

### Design B — `StrEnum` value object

Replace the literal with a `StrEnum` that parses aliases and carries transport metadata. This gives the mode runtime identity, but expands the migration and exported interface for three stable string values.

### Design C — caller-specific parse adapters

Add `parse_cli_loop_mode` and `parse_mcp_loop_mode`, each returning `LoopMode`. This keeps transport policy visibly separate, but the two interfaces currently implement the same alias set and canonicalization rule, so it preserves duplication behind two names.

### Design D — metadata-driven mode registry

Define descriptors containing the canonical value, CLI spelling, MCP spelling, and help text, then derive choices and parsing from the registry. This makes future additions declarative, but introduces a registry abstraction before modes have distinct metadata or behavior.

## Adjudication

Criteria, in order: depth, locality, seam placement, test surface, blast radius.

**Design A won.** It gives Core sole authority over accepted spellings and canonical values without changing the existing `LoopMode` wire representation. Its explicit match rejects unknown values rather than asserting a generic string rewrite. The typed advisor selected it over every alternative.

**Design C was the strongest loser.** It preserves caller-local naming and could support divergent future policies, but there is no divergence to model today. Two parser interfaces would be shallower than the single Core invariant they duplicate. Designs B and D add runtime types or metadata machinery without additional current leverage.

The advisor path was available and used; no adjudication degradation occurred.

## Implementation

- Added `LOOP_MODE_CHOICES` and `parse_loop_mode` beside `LoopMode` in `forseti.core.loop`.
- Kept parsing explicit: `check-only` and `check_only` both canonicalize to `check_only`; all other accepted values are unchanged.
- Replaced the CLI's duplicated tuple and unchecked `replace`/`cast` sequence with the Core parser.
- Replaced the MCP tool's duplicated membership check and typed assignment with the Core parser while preserving its existing error text.
- Preserved the CLI command event's exact user-supplied mode spelling.
- Preserved semantic-loop result payloads with canonical underscore spelling.
- Documented the transport aliases and event/payload distinction in the existing harness-portability design document.

The implementation and behavior-test diff is six files: 58 insertions and 14 deletions. The review adds a seventh file. The candidate estimate was four to five implementation/test files, so the six-file realization remains below the 2× escalation threshold. No `CONTEXT.md` exists, and the change introduces no new domain term.

## Test-first evidence

The regression tests exercised both public faces with both equivalent spellings.

- **Red:** 2 failed and 2 passed. Argparse rejected CLI `check_only`; the MCP tool rejected `check-only` with `ValueError`.
- **Green:** 4 passed after both adapters used `parse_loop_mode`.
- **Focused suites:** 67 passed across Core loop, semantic-loop CLI, MCP server, and check-phase tests.

## Runtime smoke

Both changed user-facing paths were executed against an empty project store:

- `python -m forseti.core semantic-loop ... --mode check_only --json` accepted the CLI alias and returned `"mode":"check_only"` with empty ingestion and outcome lists.
- `semantic_loop_tool(..., mode="check-only")` accepted the MCP alias and returned canonical `check_only` with empty ingestion and outcome lists.

This verifies the actual CLI and MCP tool paths rather than only the test harness.

## Quality gate

Each final command ran separately against the project dev environment:

- `ruff check src tests` — passed.
- `ruff format --check src tests` — 163 files already formatted.
- `ty check --python <scratch-venv> src tests` — passed.
- `pytest -q` — 1803 passed, 1 skipped in 65.75 seconds.

The system environment exposed ESBMC 8.3.0 and a Python without the declared build dependency. The final test gate therefore used an explicit `PATH` containing the scratch Python 3.12 development environment and the locally installed ESBMC 8.5.0 release. This matches the test suite's stated ESBMC contract and keeps all generated environment state under `$TMPDIR`.

## Proposed ADR for review

**Title:** Core owns semantic-loop mode spelling normalization.

**Decision:** Canonical semantic-loop mode values remain `propose`, `submit`, and `check_only`. `forseti.core.loop` owns the accepted spelling set and parses both `check-only` and `check_only` into the canonical `check_only` value. CLI and MCP documentation may retain transport-preferred spellings. Semantic-loop result payloads always expose canonical values, while CLI command events retain the exact spelling supplied by the user. Candidate precondition validation remains at the CLI and MCP boundaries and is not folded into mode parsing.
