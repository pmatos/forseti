"""Prepare a source file for either Core property-ingestion path.

File loading and best-effort signature parsing belong to Core; the properties
proposer continues to accept requests built from in-memory source and signatures.
"""

from __future__ import annotations

from pathlib import Path

from forseti.properties import (
    HarnessError,
    PromptTemplate,
    ProposalRequest,
    build_context,
    extract_signature,
)
from forseti.properties.prompts import DEFAULT_PROMPT
from forseti.unit_id import make_unit_id


def prepare_proposal_request(
    source: Path,
    *,
    function: str,
    prompt: PromptTemplate = DEFAULT_PROMPT,
    recipe: str | None = None,
    project_root: Path = Path(),
) -> ProposalRequest:
    """Read once and prepare a request, preserving source text and path spelling.

    Only an unsupported signature (`HarnessError`) degrades to signature-free
    validation. File-read failures and unexpected parser errors propagate. Every
    invocation reads afresh; callers submitting a batch prepare before its loop.
    `recipe` names an input recipe (`forseti.properties.recipes`) whose context the
    prompt shows instead of the whole file (`RecipeError` if it cannot be built);
    `None` keeps the whole-file prompt.
    """
    source_text = source.read_text()
    unit_id = make_unit_id(source, function)
    try:
        signature = extract_signature(source_text, function, infer_structs=True)
    except HarnessError:
        signature = None
    context = (
        None
        if recipe is None
        else build_context(
            recipe,
            unit_id=unit_id,
            source_text=source_text,
            project_root=project_root,
        )
    )
    return ProposalRequest(
        unit_id=unit_id,
        source_text=source_text,
        prompt=prompt,
        signature=signature,
        context=context,
    )
