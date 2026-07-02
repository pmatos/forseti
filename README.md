# Forseti

> *Forseti — the Norse god who presides over every dispute and hands down the fairest
> judgment, in his hall Glitnir. This system judges code and returns a verdict:
> **VERIFIED**, or a concrete **counterexample**.*

**Forseti puts a formal verifier inside the agent's coding loop.** As coding agents write
faster than anyone can review, trust has to move from *"a human looked at it"* (LGTM) to
*"the code carries a proof"* (Q.E.D.). Forseti closes that loop:

```
write  →  verify (ESBMC)  →  counterexample  →  fix  →  ↺
```

The agent never hands you the broken draft — you see the version that already passed.

## The bar (Jan 2027)

A **demoable** ESBMC-in-the-loop system that self-corrects on a **non-toy** example,
plus a public writeup. Engine fixes and a Lean backend are *supporting*, not the metric.

## Status

H2 2026 Igalia investment (Jul 2026 → Jan 2027). Currently: **P0 · Foundation**.

## Layout

| Path | What |
|---|---|
| [`docs/roadmap.md`](docs/roadmap.md) | The 6-phase plan, exit criteria, and risk register |
| [`docs/adr/`](docs/adr/) | Architecture Decision Records — the *why* behind each choice |
| [`docs/design/`](docs/design/) | Design RFCs — strawmen (with diagrams) under discussion |
| [`docs/walkthroughs/`](docs/walkthroughs/) | Worked end-to-end loop turns (the P0 manual `abs`/`INT_MIN` turn) |
| [`adapters/`](adapters/) | Per-harness adapters over the neutral Core + the prompt+tools fallback contract |
| `src/` | The orchestrator loop, property store, and grading harness (built P1+) |

## What it builds on

- **[ESBMC](https://github.com/esbmc/esbmc)** — the multi-language bounded model checker
  (C, C++, Python, Solidity, CUDA, Java/Kotlin) that does the proving.
- The **[ESBMC Claude Code plugin](https://github.com/esbmc)** (`/esbmc:verify`, `/esbmc:audit`)
  — already shipped; the loop dogfoods and extends it.
- **GEPA** ([arXiv:2507.19457](https://arxiv.org/abs/2507.19457)) — reflective prompt
  evolution, used to teach an LLM to propose *good* properties.
- **LLM-generated invariants for BMC** — Pirzada et al., ASE '24.

## Scope guardrail

[Vow](https://vow-lang.com) — a *proof-native* language — is the bigger, separate bet.
Forseti retrofits proofs onto code agents already write; it does **not** include Vow.
