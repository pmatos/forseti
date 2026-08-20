"""End-to-end: `forseti check` (Core face + CLI) against the real esbmc binary.

Mirrors `tests/orchestrator/test_check_integration.py` (the driver-level
acceptance test) one layer up, at the Core/CLI surface this issue adds: real
stored properties, the real `SemanticHarnessWriter`, real ESBMC — plus the two
things specific to this layer: the harness never lands beside a *tracked*
source (issue #95 advisor note — a stray `.c` there would look like new,
unverified source to the Claude Code adapter's git-status-driven discovery),
and a quoted sibling `#include` in the unit still resolves via the `-I` this
layer adds despite the harness living elsewhere. Skipped when esbmc is not on
PATH, like the other integration suites.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from forseti.core import check_source
from forseti.core.cli import main
from forseti.properties import (
    Property,
    PropertyKind,
    PropertyStatus,
    PropertyStore,
    Provenance,
    make_property_id,
)

pytestmark = pytest.mark.skipif(
    shutil.which("esbmc") is None, reason="esbmc binary not on PATH"
)

ORCH_FIXTURES = Path(__file__).resolve().parents[1] / "orchestrator" / "fixtures"


def _semantic(unit_id: str, expression: str, domain: tuple[str, ...] = ()) -> Property:
    pid = make_property_id(unit_id, PropertyKind.SEMANTIC, expression, domain)
    return Property(
        property_id=pid,
        unit_id=unit_id,
        kind=PropertyKind.SEMANTIC,
        expression=expression,
        status=PropertyStatus.CANDIDATE,
        provenance=Provenance("test", "v1"),
        domain=domain,
    )


def _stage_unit(tmp_path: Path) -> Path:
    """A `.c` source next to its own quoted sibling header, in its own dir."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    unit = src_dir / "kernel.c"
    unit.write_text(
        '#include <stdint.h>\n#include "quoted_include_helper.h"\n\n'
        "int64_t my_abs(int64_t x) {\n"
        "    return saturate_abs(x);\n"
        "}\n"
    )
    shutil.copy(
        ORCH_FIXTURES / "quoted_include_helper.h", src_dir / "quoted_include_helper.h"
    )
    return unit


def test_check_source_resolves_sibling_quoted_include_from_elsewhere(
    tmp_path: Path,
) -> None:
    unit = _stage_unit(tmp_path)
    unit_id = f"{unit}::my_abs"
    root = tmp_path / ".forseti"
    store = PropertyStore.open(root)
    # saturate_abs (the header) maps INT64_MIN -> INT64_MAX, so `result >= 0`
    # holds unconditionally -- proves the harness (written under
    # root/check-work, nowhere near `unit`) still resolved the header via `-I`.
    store.add(_semantic(unit_id, "result >= 0"))
    store.close()

    run = check_source(unit, function="my_abs", store_root=root)

    assert run.counts()["held"] == 1
    assert run.counts()["error"] == 0  # an unresolved include would be an ERROR
    # The harness landed under the store, never beside the tracked source --
    # in a per-invocation subdirectory of check-work (issue #95 review), so
    # glob recursively.
    assert list(unit.parent.glob("*.c")) == [unit]
    assert any((root / "check-work").glob("**/*.c"))


def test_check_source_extra_flags_include_dir_wins_over_sibling_angle_bracket(
    tmp_path: Path,
) -> None:
    """A project `-I` (`extra_flags`) must resolve `<...>` before the unit's own
    directory, or a same-named sibling header silently shadows the project's
    (issue #95 review: `-I` affects angle-bracket lookup order same as quoted,
    unlike a quote-only `-iquote` esbmc doesn't have)."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "config.h").write_text("#define BOUND 999999\n")
    unit = src_dir / "kernel.c"
    unit.write_text(
        "#include <config.h>\n\nint bound_ok(int x) {\n    return x < BOUND;\n}\n"
    )

    proj_include = tmp_path / "proj_include"
    proj_include.mkdir()
    (proj_include / "config.h").write_text("#define BOUND 5\n")

    unit_id = f"{unit}::bound_ok"
    root = tmp_path / ".forseti"
    store = PropertyStore.open(root)
    # BOUND is visible in the harness scope (the #include is inlined ahead of
    # main); only holds if `<config.h>` resolved to the project's copy (5),
    # not the sibling's (999999) sitting next to the unit.
    store.add(_semantic(unit_id, "BOUND == 5"))
    store.close()

    run = check_source(
        unit,
        function="bound_ok",
        store_root=root,
        extra_flags=(f"-I{proj_include}",),
    )

    assert run.counts()["error"] == 0
    assert run.counts()["held"] == 1


def test_check_source_checks_a_helper_in_a_file_that_also_defines_main(
    tmp_path: Path,
) -> None:
    """A normal executable TU (helper + its own `main`) is a legitimate check
    target, not just a hand-written main-free kernel slice (issue #95 review:
    `Unit.from_path` previously read the whole file verbatim, and the
    generated harness's own `main` collided with it -> unconditional ERROR)."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    unit = src_dir / "prog.c"
    unit.write_text(
        "int my_double(int x) {\n    return x + x;\n}\n\n"
        "int main(void) {\n    return my_double(2);\n}\n"
    )

    unit_id = f"{unit}::my_double"
    root = tmp_path / ".forseti"
    store = PropertyStore.open(root)
    store.add(_semantic(unit_id, "result == x + x"))
    store.close()

    run = check_source(unit, function="my_double", store_root=root)

    assert run.counts()["error"] == 0
    assert run.counts()["held"] == 1


def test_check_source_checks_a_helper_beside_a_mismatched_main_prototype(
    tmp_path: Path,
) -> None:
    """A realistic, internally-consistent `int main(int argc, char **argv)`
    (prototype + matching definition, both valid, ordinary C) does not match
    the harness's own hardcoded `int main(void) { ... }` entry point. If only
    the definition is renamed away, the left-behind prototype still declares
    `main` with `argc`/`argv` right next to the harness's `int main(void)` --
    a hard esbmc parse error ("conflicting types"), not merely a
    harness-render `ERROR` -- verified against a live esbmc run (issue #95
    review). `Unit.from_path` must rename the prototype alongside the
    definition, not just the definition, or this regresses to a parse
    failure regardless of the property being checked."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    unit = src_dir / "prog.c"
    unit.write_text(
        "int main(int argc, char **argv);  // forward-declared, matches below\n\n"
        "int my_double(int x) {\n    return x + x;\n}\n\n"
        "int main(int argc, char **argv) {\n"
        "    (void)argc;\n    (void)argv;\n    return my_double(2);\n}\n"
    )

    unit_id = f"{unit}::my_double"
    root = tmp_path / ".forseti"
    store = PropertyStore.open(root)
    store.add(_semantic(unit_id, "result == x + x"))
    store.close()

    run = check_source(unit, function="my_double", store_root=root)

    assert run.counts()["error"] == 0
    assert run.counts()["held"] == 1


def test_check_source_checks_main_itself(tmp_path: Path) -> None:
    """Checking `main`'s own semantic properties is a legitimate request too
    (issue #95 review): `Unit.from_path` still has to rename `main` out of
    the way (the generated harness needs that name for its own entry point),
    but the renderer must look up the identifier that actually survives the
    rename, not the now-absent `"main"` -- otherwise every property here
    would report an unconditional `ERROR` regardless of ESBMC."""
    unit = tmp_path / "prog.c"
    unit.write_text("int main(void) {\n    return 0;\n}\n")

    unit_id = f"{unit}::main"
    root = tmp_path / ".forseti"
    store = PropertyStore.open(root)
    store.add(_semantic(unit_id, "result == 0"))
    store.close()

    run = check_source(unit, function="main", store_root=root)

    assert run.counts()["error"] == 0
    assert run.counts()["held"] == 1


def test_check_source_detects_violation_past_the_default_unwind_bound(
    tmp_path: Path,
) -> None:
    """`sum_to_n(6)` needs a loop bound of 7 (N+1) to fully unwind, past
    check_source's own DEFAULT_UNWIND=4. With unwinding assertions off (the
    plain `verify` default), esbmc silently assumes the loop exits by k=4 and
    the post-loop assert path is UNREACHABLE -- a spurious HELD for a
    postcondition that is actually FALSE (issue #95 review). With them on
    (`escalating_port`, this fix), the under-unwound loop is a distinct
    `Unknown(UNDER_UNWOUND)` at k=4 that escalates the ladder to k=8 (>= 7),
    where the loop fully unwinds and the real violation is caught."""
    unit = tmp_path / "loop.c"
    unit.write_text(
        "int sum_to_n(int n) {\n"
        "    int i = 0, s = 0;\n"
        "    while (i < n) {\n"
        "        s = s + 1;\n"
        "        i = i + 1;\n"
        "    }\n"
        "    return s;\n"
        "}\n"
    )
    unit_id = f"{unit}::sum_to_n"
    root = tmp_path / ".forseti"
    store = PropertyStore.open(root)
    # sum_to_n(6) == 6 always -- this postcondition is FALSE and must be
    # caught once the loop is fully explored.
    store.add(_semantic(unit_id, "result != 6", domain=("n == 6",)))
    store.close()

    run = check_source(unit, function="sum_to_n", store_root=root)

    assert run.counts()["error"] == 0
    assert run.counts()["held"] == 0  # never a spurious pass
    assert run.counts()["violated"] == 1
    assert run.verdicts[0].k == 8  # settled past the default unwind, on escalation


def test_cli_check_exit_codes_and_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    unit = _stage_unit(tmp_path)
    unit_id = f"{unit}::my_abs"
    root = tmp_path / ".forseti"
    store = PropertyStore.open(root)
    held = _semantic(unit_id, "result >= 0")
    violated = _semantic(unit_id, "result < 0")  # always false -> VIOLATED
    store.add(held)
    store.add(violated)
    store.close()

    code = main(
        [
            "check",
            str(unit),
            "--function",
            "my_abs",
            "--store-root",
            str(root),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 1  # VIOLATED beats HELD -- worst-outcome-wins
    assert payload["counts"]["held"] == 1
    assert payload["counts"]["violated"] == 1
    verdicts_by_id = {v["property_id"]: v for v in payload["verdicts"]}
    assert verdicts_by_id[held.property_id]["outcome"] == "held"
    assert verdicts_by_id[violated.property_id]["outcome"] == "violated"
