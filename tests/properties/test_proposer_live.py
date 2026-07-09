"""Live smoke test: a real ``claude -p`` proposes properties for abs.c::my_abs.

Double-gated -- the binary must be on PATH *and* FORSETI_LIVE_LLM must be set --
because a real call costs money and needs auth, a stricter bar than the
esbmc-gated tests' single check. Never runs in CI or a default ``pytest -q``.
"""

from __future__ import annotations

import os
import shutil

import pytest

from forseti.properties import (
    ClaudeCliClient,
    PropertyKind,
    ProposalRequest,
    extract_signature,
    propose_properties,
    render_semantic_harness,
)

pytestmark = pytest.mark.skipif(
    shutil.which("claude") is None or not os.environ.get("FORSETI_LIVE_LLM"),
    reason="live LLM smoke: set FORSETI_LIVE_LLM=1 with claude on PATH",
)

ABS_SOURCE = "int64_t my_abs(int64_t x) {\n    return (x < 0) ? -x : x;\n}\n"
ABS_UNIT = "examples/abs.c::my_abs"


def test_live_proposer_yields_a_renderable_candidate() -> None:
    signature = extract_signature(ABS_SOURCE, "my_abs")
    result = propose_properties(
        ProposalRequest(ABS_UNIT, ABS_SOURCE, signature=signature),
        client=ClaudeCliClient(),
        renderer=render_semantic_harness,
    )
    assert result.accepted, f"no candidate survived; rejected={result.rejected}"
    prop = result.accepted[0]
    assert prop.kind is PropertyKind.SEMANTIC
    assert prop.provenance.prompt_id == "semantic"
    assert prop.provenance.prompt_version == "1"
