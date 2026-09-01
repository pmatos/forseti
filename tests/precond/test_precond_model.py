"""The discharge result vocabulary, extracted to its own module (`precond/model.py`).

`CallerOutcome`, `CallerCheck` and `DischargeResult` are value objects that the
S3 discharge driver constructs and the CLI/`--json` consumer reads. Extracting
them out of `discharge.py` (RFC-0003 S3) gives them a home the driver depends
*on*, never the reverse; these tests pin that one-directional source-level
boundary and the value semantics directly, without the esbmc-gated driver runs
`test_discharge.py` needs to reach them.
"""

from __future__ import annotations

import ast
from pathlib import Path

from forseti.esbmc import RunMeta, Verified, Violated
from forseti.precond.model import CallerCheck, CallerOutcome, DischargeResult
from forseti.precond.verify import Assessment, PreconditionResult


def _meta() -> RunMeta:
    return RunMeta("8.3.0", ("esbmc",), 0, 0.0, "", "")


def _violated(text: str) -> Violated:
    return Violated(_meta(), text, None)


def _unit_result(assessment: Assessment) -> PreconditionResult:
    return PreconditionResult(
        function="f",
        assessment=assessment,
        detail="unit detail",
        settled_k=4,
        max_len=8,
    )


def _check(outcome: CallerOutcome = CallerOutcome.DISCHARGED) -> CallerCheck:
    return CallerCheck("g", outcome, "why")


def test_caller_check_to_dict_omits_counterexample_without_a_violation() -> None:
    payload = _check().to_dict()
    assert payload == {
        "caller": "g",
        "outcome": "discharged",
        "detail": "why",
        "settled_k": None,
    }
    assert "counterexample" not in payload


def test_caller_check_to_dict_carries_a_violated_counterexample() -> None:
    check = CallerCheck(
        "g",
        CallerOutcome.OBLIGATION_VIOLATED,
        "passes a bad pointer",
        settled_k=7,
        esbmc_result=_violated("dereference failure: g"),
    )
    payload = check.to_dict()
    assert payload["settled_k"] == 7
    assert payload["counterexample"] == "dereference failure: g"


def test_caller_check_to_dict_omits_counterexample_for_a_non_violated_result() -> None:
    check = CallerCheck(
        "g", CallerOutcome.UNREACHABLE, "never reaches", esbmc_result=Verified(_meta())
    )
    assert "counterexample" not in check.to_dict()


def test_label_reports_a_clean_discharge() -> None:
    result = DischargeResult(
        "f",
        Assessment.DISCHARGED_VERIFIED,
        "every caller satisfies it",
        _unit_result(Assessment.ASSUMED_VERIFIED),
        (_check(),),
    )
    assert result.label == "VERIFIED (discharged — every caller satisfies it)"


def test_label_keeps_the_s2_label_when_the_upgrade_is_withheld() -> None:
    unit_result = _unit_result(Assessment.ASSUMED_VERIFIED)
    result = DischargeResult(
        "f",
        Assessment.ASSUMED_VERIFIED,
        "discharge incomplete — reason",
        unit_result,
    )
    assert result.label == f"{unit_result.label} [discharge incomplete — reason]"


def test_label_reports_a_caller_side_violation() -> None:
    result = DischargeResult(
        "f",
        Assessment.VIOLATED,
        "g() passes a bad pointer",
        _unit_result(Assessment.ASSUMED_VERIFIED),
        (_check(CallerOutcome.OBLIGATION_VIOLATED),),
    )
    assert result.label == "VIOLATED at the call site (g() passes a bad pointer)"


def test_label_passes_through_the_s2_verdict_when_there_was_nothing_to_discharge() -> (
    None
):
    unit_result = _unit_result(Assessment.ERROR)
    result = DischargeResult("f", Assessment.ERROR, "no assumed pass", unit_result)
    assert result.label == unit_result.label
    assert result.label == "ERROR (unit detail)"


def test_label_falls_back_to_the_assessment_name_when_it_diverges() -> None:
    result = DischargeResult(
        "f",
        Assessment.UNKNOWN,
        "ladder exhausted",
        _unit_result(Assessment.ERROR),
    )
    assert result.label == "UNKNOWN (ladder exhausted)"


def test_to_dict_extends_the_unit_result_with_the_discharge_verdict() -> None:
    unit_result = _unit_result(Assessment.DISCHARGED_VERIFIED)
    result = DischargeResult(
        "f",
        Assessment.DISCHARGED_VERIFIED,
        "clean sweep",
        unit_result,
        (_check(), _check(CallerOutcome.UNREACHABLE)),
    )
    payload = result.to_dict()
    assert payload["function"] == "f"  # inherited from unit_result.to_dict()
    assert payload["assessment"] == "discharged_verified"
    assert payload["assumed"] is False
    assert payload["discharged"] is True
    assert payload["detail"] == "clean sweep"
    assert [c["caller"] for c in payload["callers"]] == ["g", "g"]


def test_to_dict_marks_an_assumed_verdict() -> None:
    payload = DischargeResult(
        "f",
        Assessment.ASSUMED_VERIFIED,
        "incomplete",
        _unit_result(Assessment.ASSUMED_VERIFIED),
    ).to_dict()
    assert payload["assumed"] is True
    assert payload["discharged"] is False


def test_model_never_imports_from_the_discharge_driver() -> None:
    """The one-directional boundary: the driver depends on the model, never back.

    An AST walk over the *import statements* (not the source text — the module
    docstring names `discharge.py` in prose) proves `model.py` itself names no
    import of `discharge`, so the extraction cannot have smuggled a cycle back
    in. This is a source-level acyclicity check, not a runtime-isolation one:
    it says nothing about what `model.py`'s own imports (`.verify`) or the
    package's `__init__.py` pull in transitively.
    """
    import forseti.precond.model as model

    tree = ast.parse(Path(model.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert "discharge" not in (node.module or ""), node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert "discharge" not in alias.name, alias.name
