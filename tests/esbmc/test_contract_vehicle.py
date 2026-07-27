"""Pin what esbmc 8.3.0 can and cannot carry as a caller obligation (RFC-0003 OQ3).

RFC-0003 named ESBMC **function contracts** as the S3 discharge vehicle: mark the
callee with ``__ESBMC_requires(__ESBMC_is_fresh(p, n))`` and let
``--replace-call-with-contract`` check it at every call site. These tests record
the measurement that sent S3 down a different road, so a fork fix reopens the
question by *failing* rather than by going unnoticed:

- the contract machinery itself works — a `requires` over plain parameter
  arithmetic passes for a good caller and fails for a bad one;
- but a `requires` whose expression contains an **intrinsic call**
  (``is_fresh``, ``r_ok``) is transplanted into the caller still referring to a
  callee-local temporary that no longer exists, so **every** call site FAILS,
  good or bad — the "Could not find definition for temporary variable" warning
  of OQ3 is therefore *not* cosmetic. It fails closed (never a false VERIFIED)
  but discharges nothing;
- hoisting the intrinsic into a local first does not help, so the shape of the
  limitation is the transplant, not the expression;
- ``__ESBMC_r_ok`` used as a plain check (what S3 injects instead) is exact —
  **except** for a pointer whose offset already lies past its object's end,
  where it answers *true*. That single quirk is why `obligation_expr` rebases to
  offset zero rather than calling ``r_ok(p, n)`` directly.

Skipped when esbmc is not on PATH, like the rest of the gated suite.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from forseti.esbmc import Verified, Violated, verify

pytestmark = pytest.mark.skipif(
    shutil.which("esbmc") is None, reason="esbmc binary not on PATH"
)

_K = 8
_TIMEOUT = 30.0

# The callee, parameterised by the `requires` under test. `fill` writes `n` bytes.
_CALLEE = """\
#include <stdint.h>
#include <stddef.h>
void fill(uint8_t *p, size_t n) {{
    {requires}
    for (size_t i = 0; i < n; i++) p[i] = 0;
}}
"""

_CALLER = """\
#include <stdlib.h>
#include "{callee}"
int main(void) {{ uint8_t *b = malloc({size}); fill(b, 4); return 0; }}
"""


def _replace_run(tmp_path: Path, requires: str, size: int) -> Violated | Verified:
    callee = tmp_path / f"callee_{size}_{abs(hash(requires))}.c"
    callee.write_text(_CALLEE.format(requires=requires))
    caller = tmp_path / f"caller_{size}_{abs(hash(requires))}.c"
    caller.write_text(_CALLER.format(callee=callee.name, size=size))
    result = verify(
        caller,
        unwind=_K,
        timeout_s=_TIMEOUT,
        extra_flags=("--replace-call-with-contract", "fill", "--force-malloc-success"),
    )
    assert isinstance(result, Verified | Violated), result
    return result


def test_contract_replacement_works_for_a_plain_parameter_predicate(
    tmp_path: Path,
) -> None:
    # The control: the machinery is functional, so the failures below are about
    # *what* the requires contains, not about contracts being unimplemented.
    requires = "__ESBMC_requires(n <= 4);"
    assert isinstance(_replace_run(tmp_path, requires, 4), Verified)
    bad = _replace_run(tmp_path, "__ESBMC_requires(n <= 3);", 4)
    assert isinstance(bad, Violated)
    assert "contract requires" in bad.raw_counterexample


@pytest.mark.parametrize(
    "requires",
    [
        "__ESBMC_requires(__ESBMC_is_fresh(p, n));",
        "__ESBMC_requires(__ESBMC_r_ok(p, n));",
        "_Bool ok = __ESBMC_r_ok(p, n); __ESBMC_requires(ok);",
    ],
)
def test_intrinsic_in_a_requires_fails_even_a_valid_caller(
    tmp_path: Path, requires: str
) -> None:
    # OQ3, measured: `malloc(4)` then `fill(b, 4)` is a *correct* caller, and it
    # still FAILS. So the vehicle cannot discharge — not for is_fresh, not for
    # r_ok, and not with the intrinsic hoisted into a local first.
    result = _replace_run(tmp_path, requires, 4)
    assert isinstance(result, Violated), "expected the known transplant failure"
    assert "contract requires" in result.raw_counterexample


def test_the_is_fresh_warning_names_the_lost_temporary(tmp_path: Path) -> None:
    # The mechanism behind the failure above, so a fork fix is recognisable: the
    # transplanted expression is the intrinsic call's *result temporary*, which
    # esbmc then reports it cannot define.
    result = _replace_run(tmp_path, "__ESBMC_requires(__ESBMC_is_fresh(p, n));", 4)
    output = result.meta.stdout + result.meta.stderr
    assert "Could not find definition for temporary variable" in output
    assert "__ESBMC_is_fresh" in output


def test_r_ok_as_a_plain_check_is_exact(tmp_path: Path) -> None:
    src = tmp_path / "r_ok.c"
    src.write_text(
        "#include <stdint.h>\n"
        "#include <stddef.h>\n"
        "#include <stdlib.h>\n"
        "int main(void) {\n"
        "    uint8_t *b = malloc(8);\n"
        '    __ESBMC_assert(__ESBMC_r_ok(b, 8), "exact-fit");\n'
        '    __ESBMC_assert(__ESBMC_r_ok(b + 5, 3), "interior-fit");\n'
        "    return 0;\n"
        "}\n"
    )
    result = verify(
        src, unwind=2, timeout_s=_TIMEOUT, extra_flags=("--force-malloc-success",)
    )
    assert isinstance(result, Verified), result


@pytest.mark.parametrize(
    "expr, expect_violated",
    [
        ("__ESBMC_r_ok(b, 9)", True),  # over the object from its base — caught
        ("__ESBMC_r_ok(b + 5, 4)", True),  # over the object from inside — caught
        ("__ESBMC_r_ok(b + 9, 4)", False),  # base already past the end — MISSED
    ],
)
def test_r_ok_misses_a_base_pointer_past_the_object_end(
    tmp_path: Path, expr: str, expect_violated: bool
) -> None:
    # The quirk `obligation_expr` is written around. The third case is a real
    # caller bug (`len - HEADER` underflowing to a huge span walks the pointer
    # off the object) that a direct `r_ok(p, n)` obligation would discharge.
    src = tmp_path / f"quirk_{abs(hash(expr))}.c"
    src.write_text(
        "#include <stdint.h>\n"
        "#include <stddef.h>\n"
        "#include <stdlib.h>\n"
        "int main(void) {\n"
        "    uint8_t *b = malloc(8);\n"
        f'    __ESBMC_assert({expr}, "probe");\n'
        "    return 0;\n"
        "}\n"
    )
    result = verify(
        src, unwind=2, timeout_s=_TIMEOUT, extra_flags=("--force-malloc-success",)
    )
    assert isinstance(result, Violated) is expect_violated, result
