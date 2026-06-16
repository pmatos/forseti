# src/

Implementation home. Populated from P1 onward:

- `orchestrator/` — closes the write → verify → counterexample → fix loop (P1)
- `properties/` — the property store + LLM property proposer, semantic + reachability (P2)
- `grading/` — differential mutation-kill harness that scores properties (P2)
- `gepa/` — prompt-evolution driver over the kill-rate score (P3)
- `esbmc/` — thin typed wrapper: invoke ESBMC, parse VERIFIED | VIOLATED+cex | UNKNOWN (P0)

See [`../docs/roadmap.md`](../docs/roadmap.md).
