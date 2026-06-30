# ADR-0009 — CI ESBMC binary provenance: upstream release pin + corpus parity guard

- **Status:** Accepted
- **Date:** 2026-06-30
- **Relates to:** ADR-0004 (ESBMC fork strategy)

## Context

ADR-0004 pins the project to its own ESBMC fork so the agent loop is never blocked on upstream
review latency. That decision governs which **branch** the project iterates on; it does not
prescribe where CI downloads a pre-built binary from.

When the CI gate was designed (issue #30, PR #41), the fork had no published release assets.
The CI workflow pinned `esbmc/esbmc` upstream at v8.3.0 — verified byte-identical to the fork
HEAD on every corpus kernel. An inline comment in the workflow acknowledged this, but a
subsequent review flagged the upstream URL as a policy violation of ADR-0004.

The evaluators split (REJECT vs DEFER), deferring to this ADR to settle the question.

Two resolution paths exist:

- **(a)** Publish a pinned release binary from the fork, then change `ESBMC_URL` and
  `ESBMC_SHA256` in CI to point there.
- **(b)** Codify that CI may pin an upstream release binary while a corpus verdict-parity check
  guards against undetected fork divergence.

Path (a) is the eventual target: once the fork accumulates patches that diverge from upstream,
a fork-published binary is the only way to exercise those patches in CI. Path (b) is the safe
interim policy while the fork is still byte-identical to upstream.

## Decision

**CI may pin an upstream `esbmc/esbmc` release binary as long as the following invariant holds:**

> The pinned binary must be confirmed verdict-identical to the project fork on the full
> verification corpus (all `tests/corpus/` kernels, same ESBMC flags).

This invariant must be re-verified whenever either the pinned version *or* the fork HEAD
changes. The CI workflow must document the parity check result inline (binary hash, commit,
date) so any divergence is visible without running the check.

When the fork first publishes a release asset — or when the parity check fails — switch
`ESBMC_URL` to the fork URL immediately. Do not wait for the next planned release.

## Consequences

- The interim CI gate stays unblocked: no fork release infrastructure is needed today.
- The parity invariant makes any fork divergence detectable before it silently affects CI
  verdicts.
- The obligation to switch URLs when fork assets become available (or parity breaks) is
  explicit, keeping ADR-0004's intent intact.
- Maintainers must remember to re-run the parity check after any fork-side ESBMC patch; the
  roadmap Risk 7/11 (fork divergence) now has a CI-visible tripwire.
