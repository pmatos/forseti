# Architecture review — forseti — 2026-09-03

**Scope**: Reconciled the persisted `.architecture/backlog.md` against `gh`, then scanned the
recent hot spots — the composed semantic-loop work (#252 `submit`/MCP, #255 loop, #259 Stop-gate
semantic block) in `src/forseti/core/` and `src/forseti/adapters/`. YAGNI weighting: the semantic
pipeline is the actively-growing area of the tree, so friction there pays back fastest.
**Picked**: `propose-submit-ingest-trace-seam` — see the PR and `.architecture/backlog.md`.
**Degradations**: none. (`advisor` was rate-limited at the pick; the pick was stress-tested by hand
instead — see *Pick*.)

Diagram convention (replaces the upstream HTML legend): **solid edges are the interface a caller
sees; dashed edges are inside the implementation.**

## Candidates

### propose-submit-ingest-trace-seam — one home for the property-ingest persistence boundary · Strong · score 20/25

- **Files** — `src/forseti/core/propose.py:72-104` (`propose_source`), `src/forseti/core/submit.py:79-128`
  (`submit_source`), and the third store-open/translate site `src/forseti/core/check.py:160-183`
  (`check_source`); a new leaf to hold the seam; plus their tests. **File-count estimate: ~6.**
- **Score — 20/25**
  - **Leverage 4** — two sibling Core faces (`propose_source`, `submit_source`) each shed a duplicated
    prologue *and* a byte-identical persistence epilogue; the store-open error translation is shared with
    a third caller (`check_source`). Several call sites simplify materially.
  - **Locality 4** — the three co-varying concerns (the `sqlite3.Error → PropertyStoreError` translation,
    the `persist=False` "touch nothing" invariant, and the `record_property_proposed` trace dispatch) are
    today kept in lockstep across two/three files only by copied code and copied comments; afterwards a
    change to any one is a one-file edit.
  - **Blast radius 2** — a module and its direct callers; no published interface changes (the public
    `propose_source`/`submit_source`/`check_source` signatures and the #44 wire shape are untouched). ~6 files.
  - **Heat 4** — `submit.py` was *created* by #252 (2026-09-02) and `propose.py`/`check.py` were touched in
    the same semantic-loop push; this is the hottest code in the tree.
- **Problem** — `propose_source` and `submit_source` are structural twins. Both open with
  `source.read_text()` → `unit_id = f"{source}::{function}"` → best-effort `extract_signature` (degrading to
  `None` on `HarnessError`) → build a `ProposalRequest`. Both then run the **identical** persistence epilogue:
  the `persist=False` early return carrying the *same verbatim comment* about the dry-run contract, then
  `with PropertyStore.open(store_root) as store: result = <ingest>(…, store=store)`, the *same*
  `except sqlite3.Error → PropertyStoreError(f"property store error at {store_root}: {exc}")`, then
  `record_property_proposed(store_root, result, channel=…)`. The only genuine variance is the ingest callable
  (`propose_properties` vs `submit_candidates`) and the channel string (`"llm"` vs `"submitted"`). This is a
  **shallow duplication with no seam**: the persistence-boundary policy (error translation + dry-run invariant
  + trace dispatch) has no single home, so it is held consistent by hand — the docstrings even cross-reference
  each other ("mirroring `propose_source`/`check_source`") to remind a maintainer to keep them in step.
- **Deletion test** — **Concentrates.** Give the epilogue one home — a higher-order seam that takes the
  ingest as a callable and owns the open/translate/trace — and the policy lives once. Delete a caller's copy
  and the behaviour does not move to the caller; it is already behind the seam. A future third ingest channel
  adds a call, not a fourth copy of the invariant.
- **Solution** — Extract a persistence-boundary seam in Core: a context manager (or helper) that opens the
  `PropertyStore` and translates `sqlite3.Error → PropertyStoreError` — reused by all three faces including
  `check_source` — and, layered on it, an "ingest-and-trace" seam for the proposer faces that owns the
  `persist=False` dry-run skip and the `record_property_proposed` dispatch, taking
  `ingest: Callable[[PropertyStore | None], ProposalResult]` and a `channel`. `propose_source` and
  `submit_source` collapse to "read the unit → build the request → delegate". The exact surface is chosen in
  *Design*.
- **Benefits** — **Leverage**: the persistence-boundary policy is written once and reused by three callers;
  a new proposer channel (a plausible next step in the #213 epic) inherits it for free. **Locality**: the
  dry-run invariant and the store-error contract stop being a "keep these two functions in sync" hazard.
  **Test surface**: the store-error translation and the dry-run "touches nothing" guarantee become directly
  testable *through the seam* with a single fake, instead of being re-asserted against each face; the faces'
  own tests shrink to "did it delegate and record the right channel".

**Before**

```mermaid
graph LR
  P[propose_source] --> RD1[read + signature]
  P --> OT1[open store + translate sqlite]
  P --> TR1[record_property_proposed llm]
  S[submit_source] --> RD2[read + signature]
  S --> OT2[open store + translate sqlite]
  S --> TR2[record_property_proposed submitted]
  C[check_source] --> OT3[open store + translate sqlite]
```

**After**

```mermaid
graph LR
  P[propose_source] --> I[ingest_and_trace]
  S[submit_source] --> I
  I -.-> OT[open store + translate]
  I -.-> TR[record_property_proposed]
  C[check_source] --> OT
```

### hook-verdict-report-two-hooks — one UnitVerdict→report transform for the two PostToolUse hooks · Worth exploring · score 19/25 · runner-up candidate

- **Files** — `src/forseti/adapters/claude_code/post_tool_use.py`, `post_bash.py` (and the dict-based
  `stop_gate.py:_residual`, deliberately **out of scope** — it renders persisted state dicts, not live
  `UnitVerdict`s, and is the fail-closed Stop heart). ~5 files.
- **Score — 19/25** (leverage 4, locality 4, blast radius 2, heat 3). Carried from the prior firing's
  backlog; friction re-confirmed present (`post_bash._report` and `post_tool_use`'s success/failure blocks are
  still near-verbatim, differing only in a few text fragments).
- **Problem / deletion test / solution** — unchanged from `.architecture/backlog.md`; the duplicated
  `UnitVerdict[] → (message, exit code)` render across the two PostToolUse hooks would concentrate behind one
  `verdict_report` seam.
- **Recommendation strength** — Worth exploring. Lost to the pick by one point on **heat**: the pick sits in
  code created yesterday, this in code last touched 2026-09-01/-08-20. Natural next firing.

The remaining proposed candidates (`counterexample-fired-label-predicate` 17, `esbmc-caller-openings-module-split`
17, `precond-under-unwound-detection-into-esbmc` 15, `core-store-session-boundary`, `gate-env-config-extraction`,
`adapter-install-skeleton-two-harnesses`, `canonical-gate-decision-helper` 13) are unchanged from the backlog
and were re-checked as still-present but lower-scoring; they are not re-carded here.

## Dropped

No new candidate tripped a hard filter this run. The standing `dropped` rows in `.architecture/backlog.md`
were re-checked against their filters (per the reconcile step) and none has changed:

| Candidate | Dropped because (re-confirmed) |
|---|---|
| `mcp-server-tool-wrappers` | Leverage 1 — the typed `*_tool` param list *is* the MCP schema the SDK introspects |
| `verify-and-record-decomposition` | Not a deepening — the fail-closed/ownership heart of the gate; deep, not shallow |
| `cli-run-handler-shape` | Leverage 1 — per-command exit-code logic genuinely differs |
| `esbmc-init-all-parser-surface` | Leverage 1 — interface hygiene, nothing concentrates |
| `harness-writer-port-inline` | Not a deepening — a one-adapter seam to inline, not deepen |

## Too large to automate

None this run — no surviving candidate scored blast radius 5.

## Pick

**`propose-submit-ingest-trace-seam` (20/25).** It is the top-scored surviving candidate under the
deterministic rubric and clears every hard filter. It is genuinely fresh: `submit.py` was created by #252 and
did not exist at the prior firing's scan, so this is not an override of the persisted memory — the runner-up
candidate `hook-verdict-report-two-hooks` remains `proposed` for the next firing.

**The top two are within 1 point** (20 vs 19), so the pick was close. The single point is **heat**: both
refactors are the same shape (two sibling faces sharing a duplicated block, blast radius 2, leverage 4,
locality 4), but the pick sits in code created *yesterday* while the runner-up's files were last touched
2026-09-01/2026-08-20. The pick is robust to conservative scoring: even if the pick's heat were 3 (a tie at
19), the deterministic tie-break — equal blast radius, then higher heat — still selects it, because its files
are strictly more recent. `advisor` was rate-limited at the decision, so this reasoning stands in for it and is
recorded here for a reviewer to disagree with in the PR.

## Design

Three interfaces were designed in parallel by sub-agents briefed to differ radically, then a fourth
sub-agent that authored none of them adjudicated against the fixed criteria in order: **depth →
locality → seam placement → test surface → blast radius.**

All three converged on the **same epilogue seam** — a higher-order
`run_proposal(ingest, *, persist, store_root, channel) -> ProposalResult` that owns the dry-run skip,
the store lifecycle, and the trace dispatch, with the per-face driver injected as a closure. That
convergence is itself evidence the epilogue is the right seam. They differed on two axes: **where the
store-open/translate concern lives**, and **whether to also extract the shared prologue**.

### The three designs

- **Design A — minimal functional, Core-contained (WINNER).** One new Core leaf
  `src/forseti/core/persistence.py` holds *both* an `open_store(store_root)` context manager (translating
  `sqlite3.Error → PropertyStoreError` around open **and** body) and `persist_proposal(ingest, *, persist,
  store_root, channel)`. All three concerns live in one module. `check_source` imports `open_store` only.
  No change to the published `forseti.properties` surface.
- **Design B — layered, resource-oriented (runner-up design).** Concern #1 becomes a new
  `PropertyStore.session(root)` **classmethod on the published `PropertyStore` type** (in `properties/store.py`,
  beside `.open` and the `PropertyStoreError` it raises); concern #2/#3 become `run_proposal` in
  `core/propose.py`. The persistence boundary is split across two packages.
- **Design C — maximum encapsulation, value object.** Adds a `ProposalUnit` frozen dataclass
  (`.read(source, function)` smart constructor + `.request(prompt=None)`) to also swallow the prologue, plus
  a free `open_property_store` and `run_proposal` in a new `core/proposal.py`.

### Adjudication

- **Depth**: A ≈ B. At each face, one deep call hides concerns #1+#2+#3; `check` learns exactly one open
  primitive. Symmetric leverage. **C fails to lead**: `ProposalUnit.read(...).request()` is two
  temporally-coupled methods whose combined interface roughly equals the 3-line block they hide — de-dup in a
  value-object costume, not added depth (its own author flagged `.request()` as "first to drop"). Fall through.
- **Locality (decisive)**: **A wins.** The unit of change is the persistence epilogue *as a whole* — #1, #2,
  #3 co-vary. A concentrates that entire boundary (plus the `check`-shared open primitive) in one Core module,
  so every future change and its tests land in one file. B **fragments** it across two packages. The
  clincher is primary-source: a **fourth caller**, `adapters/claude_code/property_gate.py:367-381`,
  deliberately *swallows* the raw `(PropertyStoreError, sqlite3.Error, OSError)` — with a comment explicitly
  contrasting `check_source`, which raises. So the `sqlite3.Error → PropertyStoreError` translation is a
  Core-**face raise-policy**, not a `PropertyStore` invariant; it belongs in Core (A), not bolted onto the
  published store type (B). B would also ship *two* opening primitives with silently different exception
  contracts on the published type without even being able to retire `.open` (the adapter still needs the raw
  one) — the exact smell B invoked to justify itself, merely relocated.
- **Seam placement / test surface**: tie among A/B; both real seams (2- and 3-adapter) at the genuinely
  varying spot (the ingest closure + `channel`). C adds a third "seam" at the prologue where nothing varies.
- **Blast radius (confirmatory)**: A is smallest and touches **zero** published surface; B enlarges the public
  `PropertyStore` API; C is largest (new type + new file + `properties/__init__.py` export).

**Winner: Design A.** Runner-up **design**: Design B — it lost on locality (splitting the boundary across
two packages), a criterion-2 loss that dominates its subordinate "error next to its class" argument, which
the fourth-caller evidence further undercuts.

### Improvements folded into the winner (absorbed from the losers / adjudicator)

1. **Loud contract docstring** on `open_store` naming `PropertyStoreError` as its raised contract and
   contrasting it with `PropertyStore.open`'s raw one — B's "make the exception contract loud" insight,
   honored at A's single home instead of by splitting.
2. **`channel: Literal["llm", "submitted"]`** on `persist_proposal` (C/adjudicator note) so the trace-dispatch
   seam can't be handed a bad channel. Kept **local** to `persist_proposal`; `record_property_proposed`'s own
   `channel: str` is left unchanged to keep blast radius tight (a wider `events.py` change is out of scope).
3. **Body-spanning translation**: `open_store` must translate `sqlite3.Error` raised inside the `with` body
   (the ingest call; `check`'s `record_event` + `check_properties`), not just at open — matching today's
   inline `try/except` span. Non-`sqlite3` errors (`LLMError`, `DuplicateProperty`, `OSError` from `mkdir`)
   pass through untranslated, as now.
4. **Not absorbed (scope creep, recorded)**: relocating `DEFAULT_STORE_ROOT` from `propose.py` into
   `persistence.py`, and propagating the `Literal` into `events.py`, are deliberately deferred — both widen
   the diff past what the candidate was scored on.

The load-bearing equivalence was verified against the code before implementation: both `propose_properties`
and `submit_candidates` default `store: CandidateStore | None = None`, and `_persist(accepted, None)` returns
immediately (`proposer.py:214-216`), so the dry-run closure `ingest(None)` is behaviour-identical to today's
no-`store` call.
