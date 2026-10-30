"""Prepare a source file for either Core property-ingestion path.

File loading and best-effort signature parsing belong to Core; the properties
proposer continues to accept requests built from in-memory source and signatures.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from forseti.core.persistence import open_store, store_exists
from forseti.properties import (
    DEFAULT_INVARIANT_PROMPT,
    RECIPES,
    HarnessError,
    InvariantRequest,
    PromptTemplate,
    ProposalContext,
    ProposalRequest,
    StructParam,
    build_context,
    extract_signature,
    normalised_type_name,
    store_contract_source,
    type_name_reason,
)
from forseti.properties.pack import ContractSource
from forseti.properties.prompts import DEFAULT_PROMPT
from forseti.unit_id import make_unit_id


@contextmanager
def _contract_source(
    recipe: str, store_root: Path | None
) -> Generator[ContractSource | None]:
    """The accepted contracts of `store_root`, for a recipe that reads them.

    The store is read here and not created: a root without a store is an empty one.
    """
    selected = RECIPES.get(recipe)
    if selected is None or not selected.reads_store or store_root is None:
        yield None
    elif not store_exists(store_root):
        yield store_contract_source(None)
    else:
        with open_store(store_root) as store:
            yield store_contract_source(store)


def _recipe_context(
    recipe: str | None,
    *,
    unit_id: str,
    source_text: str,
    project_root: Path,
    store_root: Path | None,
) -> ProposalContext | None:
    if recipe is None:
        return None
    with _contract_source(recipe, store_root) as contracts_for:
        return build_context(
            recipe,
            unit_id=unit_id,
            source_text=source_text,
            project_root=project_root,
            contracts_for=contracts_for,
        )


def prepare_proposal_request(
    source: Path,
    *,
    function: str,
    prompt: PromptTemplate = DEFAULT_PROMPT,
    recipe: str | None = None,
    project_root: Path = Path(),
    store_root: Path | None = None,
) -> ProposalRequest:
    """Read once and prepare a request, preserving source text and path spelling.

    Only an unsupported signature (`HarnessError`) degrades to signature-free
    validation. File-read failures and unexpected parser errors propagate. Every
    invocation reads afresh; callers submitting a batch prepare before its loop.
    `recipe` names an input recipe (`forseti.properties.recipes`) whose context the
    prompt shows instead of the whole file (`RecipeError` if it cannot be built);
    `None` keeps the whole-file prompt. A recipe that reads accepted contracts
    (`callees`) reads them from `store_root`, as the store is at this call; the
    digest of the contracts shown is in the recipe ref. It is a `RecipeError`
    without a `store_root`.
    """
    source_text = source.read_text()
    unit_id = make_unit_id(source, function)
    try:
        signature = extract_signature(source_text, function, infer_structs=True)
    except HarnessError:
        signature = None
    context = _recipe_context(
        recipe,
        unit_id=unit_id,
        source_text=source_text,
        project_root=project_root,
        store_root=store_root,
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
    store_root: Path | None = None,
) -> InvariantRequest:
    """Read once and prepare a representation-invariant request for `type_name`.

    `function` must take a ``type_name *``: it is the unit the recipe's context is
    built for and the example the prompt names. `HarnessError` if the type name is
    not a struct type name, or `function` is not found or takes no such pointer.
    The type key is ``<source>::<type_name>`` in the raw `source` spelling, like
    every unit id. `recipe` is as for `prepare_proposal_request`.
    """
    type_name = normalised_type_name(type_name)
    reason = type_name_reason(type_name)
    if reason is not None:
        raise HarnessError(reason)
    source_text = source.read_text()
    signature = extract_signature(source_text, function, struct_types=(type_name,))
    if not any(isinstance(p, StructParam) for p in signature.params):
        raise HarnessError(f"{function!r} takes no {type_name!r} pointer parameter")
    context = _recipe_context(
        recipe,
        unit_id=make_unit_id(source, function),
        source_text=source_text,
        project_root=project_root,
        store_root=store_root,
    )
    return InvariantRequest(
        type_key=make_unit_id(source, type_name),
        type_name=type_name,
        function=function,
        source_text=source_text,
        prompt=DEFAULT_INVARIANT_PROMPT,
        context=context,
    )
