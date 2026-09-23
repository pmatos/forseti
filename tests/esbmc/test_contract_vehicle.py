"""Pin what esbmc can and cannot carry as a caller obligation (RFC-0003 OQ3).

RFC-0003 named ESBMC **function contracts** as the S3 discharge vehicle: mark the
callee with ``__ESBMC_requires(__ESBMC_is_fresh(p, n))`` and let
``--replace-call-with-contract`` check it at every call site. Originally (esbmc
8.3.0) this failed shut for every intrinsic: a `requires` containing a call was
transplanted into the caller still referring to a callee-local temporary that no
longer existed, so **every** call site FAILED, good or bad, with a "Could not
find definition for temporary variable" warning.

esbmc 8.5.0 (upstream PR esbmc/esbmc#6944, "[contracts] Report a clause call
instead of lifting it") changed this:

- the contract machinery itself works — a `requires` over plain parameter
  arithmetic passes for a good caller and fails for a bad one;
- ``__ESBMC_is_fresh`` is now **excluded from the transplant** and materialised
  properly — OQ3 is reopened for it: a valid caller VERIFIES, a bad one
  VIOLATES at "contract requires", exactly what RFC-0003's original S3 vehicle
  wanted. The old warning still prints (a harmless byproduct), but no longer
  reflects a broken check;
- a bare ``__ESBMC_r_ok(p, n)`` call in a `requires` (not a materialising
  contract intrinsic) is no longer silently lifted-and-wrong — esbmc now
  refuses it up front with an explicit `Error`, naming the callee. Still fails
  closed, for a clearer reason;
- hoisting the intrinsic into a local `_Bool` first still exhibits the original
  dead-temporary bug (unaffected by #6944, since the clause then references a
  plain boolean symbol, not a call);
- ``__ESBMC_r_ok`` used as a plain check (what S3 actually injects, not via
  contracts) is exact — **except** for a pointer whose offset already lies past
  its object's end, where it answers *true*. That single quirk is why
  `obligation_expr` rebases to offset zero rather than calling ``r_ok(p, n)``
  directly.

Skipped when esbmc is not on PATH, like the rest of the gated suite.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from forseti.esbmc import Error, EsbmcResult, Verified, Violated, verify

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


def _replace_run_any(tmp_path: Path, requires: str, size: int) -> EsbmcResult:
    """Like `_replace_run`, but for a `requires` esbmc now refuses up front."""
    callee = tmp_path / f"callee_{size}_{abs(hash(requires))}.c"
    callee.write_text(_CALLEE.format(requires=requires))
    caller = tmp_path / f"caller_{size}_{abs(hash(requires))}.c"
    caller.write_text(_CALLER.format(callee=callee.name, size=size))
    return verify(
        caller,
        unwind=_K,
        timeout_s=_TIMEOUT,
        extra_flags=("--replace-call-with-contract", "fill", "--force-malloc-success"),
    )


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


def test_is_fresh_in_a_requires_now_discharges_correctly(tmp_path: Path) -> None:
    # OQ3, re-measured under esbmc 8.5.0 (upstream #6944): `is_fresh` is exempt
    # from the transplant and materialised properly — a correct caller VERIFIES,
    # a bad one VIOLATES at "contract requires", exactly what RFC-0003's
    # original S3 vehicle wanted.
    good = _replace_run(tmp_path, "__ESBMC_requires(__ESBMC_is_fresh(p, n));", 4)
    assert isinstance(good, Verified), good
    bad = _replace_run(tmp_path, "__ESBMC_requires(__ESBMC_is_fresh(p, n));", 3)
    assert isinstance(bad, Violated), bad
    assert "contract requires" in bad.raw_counterexample


def test_the_is_fresh_warning_is_now_a_harmless_byproduct(tmp_path: Path) -> None:
    # esbmc still prints the "lost temporary" warning it always did — but it no
    # longer reflects a broken check (see the test above): the warning and a
    # correct VERIFIED now coexist.
    result = _replace_run(tmp_path, "__ESBMC_requires(__ESBMC_is_fresh(p, n));", 4)
    assert isinstance(result, Verified), result
    output = result.meta.stdout + result.meta.stderr
    assert "Could not find definition for temporary variable" in output
    assert "__ESBMC_is_fresh" in output


def test_bare_intrinsic_call_in_a_requires_is_now_rejected_up_front(
    tmp_path: Path,
) -> None:
    # A bare `__ESBMC_r_ok(p, n)` call (not a materialising contract intrinsic
    # like `is_fresh`) is no longer silently lifted-and-wrong: esbmc now refuses
    # it up front, naming the callee. Still fails closed, never a false
    # VERIFIED, just for a clearer reason than the old dead-temporary Violated.
    result = _replace_run_any(tmp_path, "__ESBMC_requires(__ESBMC_r_ok(p, n));", 4)
    assert isinstance(result, Error), "expected the new up-front contract rejection"
    assert "cannot name outside the function body" in result.message
    assert "__ESBMC_r_ok" in result.message


def test_hoisted_local_intrinsic_result_still_fails_even_a_valid_caller(
    tmp_path: Path,
) -> None:
    # Hoisting the intrinsic into a local `_Bool` first is unaffected by #6944
    # (the clause then references a plain boolean symbol, not a call): `malloc(4)`
    # then `fill(b, 4)` is a *correct* caller, and it still FAILS. So the
    # transplant bug persists for this shape.
    requires = "_Bool ok = __ESBMC_r_ok(p, n); __ESBMC_requires(ok);"
    result = _replace_run(tmp_path, requires, 4)
    assert isinstance(result, Violated), "expected the known transplant failure"
    assert "contract requires" in result.raw_counterexample


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
