"""Prepare a source file for either Core property-ingestion path.

File loading and best-effort signature parsing belong to Core; the properties
proposer continues to accept requests built from in-memory source and signatures.
"""

from __future__ import annotations

from pathlib import Path

from forseti.properties import (
    DEFAULT_INVARIANT_PROMPT,
    HarnessError,
    InvariantRequest,
    PromptTemplate,
    ProposalRequest,
    StructParam,
    build_context,
    extract_signature,
    normalised_type_name,
    valid_type_name,
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


def prepare_invariant_request(
    source: Path,
    *,
    function: str,
    type_name: str,
    recipe: str | None = None,
    project_root: Path = Path(),
) -> InvariantRequest:
    """Read once and prepare a representation-invariant request for `type_name`.

    `function` must take a ``type_name *``: it is the unit the recipe's context is
    built for and the example the prompt names. `HarnessError` if the type name is
    not a struct type name, or `function` is not found or takes no such pointer.
    The type key is ``<source>::<type_name>`` in the raw `source` spelling, like
    every unit id. `recipe` is as for `prepare_proposal_request`.
    """
    type_name = normalised_type_name(type_name)
    if not valid_type_name(type_name):
        raise HarnessError(
            f"{type_name!r} is not a struct type name (ring_t or 'struct node')"
        )
    source_text = source.read_text()
    signature = extract_signature(source_text, function, struct_types=(type_name,))
    if not any(isinstance(p, StructParam) for p in signature.params):
        raise HarnessError(f"{function!r} takes no {type_name!r} pointer parameter")
    context = (
        None
        if recipe is None
        else build_context(
            recipe,
            unit_id=make_unit_id(source, function),
            source_text=source_text,
            project_root=project_root,
        )
    )
    return InvariantRequest(
        type_key=make_unit_id(source, type_name),
        type_name=type_name,
        function=function,
        source_text=source_text,
        prompt=DEFAULT_INVARIANT_PROMPT,
        context=context,
    )
