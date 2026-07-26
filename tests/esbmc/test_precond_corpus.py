"""Acceptance: the memory-precondition gate over the corpus (RFC-0003 S2 + S3).

The issues' exit criteria, pinned end-to-end against the real esbmc binary.

S2 (#125), over ``examples/sha1.c``:

- ``sha1_init/update/final/transform`` each reach **ASSUMED_VERIFIED** up to
  ``max_len`` under their synthesised precondition (a fresh ``sha1_ctx``, a
  ``malloc(len)`` buffer, a ``malloc(20)`` digest) — including the non-vacuity
  discharge (a reachable call site).
- the off-by-one twin ``sha1_bug.c::sha1_update`` (``i <= len`` reads
  ``data[len]``) is **VIOLATED** with a real ``array bounds violated``
  counterexample — a non-vacuous failure, not a phantom.

S3 (#126), over ``examples/frame_checksum.c``:

- a correct caller makes ``sum_bytes``'s assumed precondition **discharged**,
  not merely assumed;
- the twin whose caller drops its short-frame guard is **VIOLATED at the call
  site**, naming the caller — while the callee itself is untouched and its
  sibling caller still discharges.

Skipped when esbmc is not on PATH, exactly like `test_corpus.py`; CI installs a
pinned esbmc so it always runs there.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from forseti.precond import (
    Assessment,
    CallerOutcome,
    discharge_precondition,
    verify_precondition,
)

pytestmark = pytest.mark.skipif(
    shutil.which("esbmc") is None, reason="esbmc binary not on PATH"
)

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
MAX_LEN = 8


@pytest.mark.parametrize(
    "function",
    ["sha1_init", "sha1_transform", "sha1_update", "sha1_final"],
)
def test_sha1_unit_assumed_verified(function: str) -> None:
    result = verify_precondition(
        EXAMPLES / "sha1.c", function=function, max_len=MAX_LEN
    )
    assert result.assessment is Assessment.ASSUMED_VERIFIED, result.label
    assert result.max_len == MAX_LEN
    assert result.settled_k is not None and result.settled_k > MAX_LEN
    assert "assuming valid caller pointers" in result.label


def test_offbyone_twin_is_violated_non_vacuously() -> None:
    result = verify_precondition(
        EXAMPLES / "sha1_bug.c", function="sha1_update", max_len=MAX_LEN
    )
    assert result.assessment is Assessment.VIOLATED, result.label
    # a real, reachable out-of-bounds — the mechanism the gate exists to keep,
    # never silenced as a phantom.
    assert result.esbmc_result is not None
    raw = getattr(result.esbmc_result, "raw_counterexample", "")
    assert "array bounds violated" in raw


_STATIC_MIN_AND_LEN = """\
#include <stddef.h>

void fill(unsigned char p[static 4], size_t len) {
  for (size_t i = 0; i < len; i++) p[i] = 0;
  p[3] = 1;
}

void fill_bug(unsigned char p[static 4], size_t len) {
  for (size_t i = 0; i <= len; i++) p[i] = 0;
}

void over_minimum(unsigned char p[static 4], size_t len) {
  (void)len;
  p[4] = 0;
}
"""


def test_static_minimum_with_length_is_not_a_phantom(tmp_path: Path) -> None:
    # Issue #137: `p[static 4]` binds the caller to *at least* 4 elements, and the
    # companion `len` says the buffer holds `len` — a valid caller satisfies both,
    # so a body that touches `len` elements *and* `p[3]` is correct code. Sizing
    # the object at exactly 4 phantom-VIOLATED it for every `len > 4`.
    src = tmp_path / "static_min.c"
    src.write_text(_STATIC_MIN_AND_LEN)
    result = verify_precondition(src, function="fill", max_len=MAX_LEN)
    assert result.assessment is Assessment.ASSUMED_VERIFIED, result.label


def test_static_minimum_with_length_still_catches_the_off_by_one(
    tmp_path: Path,
) -> None:
    # The floor must not blunt the detector: `i <= len` reads `p[len]`, which is
    # out of bounds for every `len >= 4`, and stays a real VIOLATED.
    src = tmp_path / "static_min.c"
    src.write_text(_STATIC_MIN_AND_LEN)
    result = verify_precondition(src, function="fill_bug", max_len=MAX_LEN)
    assert result.assessment is Assessment.VIOLATED, result.label
    assert result.esbmc_result is not None
    raw = getattr(result.esbmc_result, "raw_counterexample", "")
    assert "array bounds violated" in raw


def test_static_minimum_floor_still_explores_the_weakest_caller(
    tmp_path: Path,
) -> None:
    # The floor raises the object's *ceiling*, it does not pin the length: `len`
    # stays symbolic down to 0, so a caller giving exactly the declared 4 elements
    # is still explored and `p[4]` — over the minimum, with nothing else to justify
    # it — remains a real VIOLATED rather than being swallowed by the max.
    src = tmp_path / "static_min.c"
    src.write_text(_STATIC_MIN_AND_LEN)
    result = verify_precondition(src, function="over_minimum", max_len=MAX_LEN)
    assert result.assessment is Assessment.VIOLATED, result.label
    assert result.esbmc_result is not None
    raw = getattr(result.esbmc_result, "raw_counterexample", "")
    assert "array bounds violated" in raw


def _discharge(name: str, function: str = "sum_bytes"):  # type: ignore[no-untyped-def]
    return discharge_precondition(EXAMPLES / name, function=function, max_len=MAX_LEN)


def test_correct_callers_discharge_the_precondition() -> None:
    # The S3 exit criterion: not "VERIFIED assuming valid caller pointers" but
    # VERIFIED *because every caller was verified to supply them, up to k*.
    result = _discharge("frame_checksum.c")
    assert result.assessment is Assessment.DISCHARGED_VERIFIED, result.label
    assert "discharged" in result.label
    assert {c.caller for c in result.callers} == {
        "frame_checksum",
        "payload_checksum",
    }
    assert all(c.outcome is CallerOutcome.DISCHARGED for c in result.callers)
    # the upgrade is on top of a real S2 pass, never instead of one
    assert result.unit_result.assessment is Assessment.ASSUMED_VERIFIED


def test_a_bad_caller_is_violated_at_the_call_site() -> None:
    # `payload_checksum` loses its short-frame guard, so `len - HEADER_BYTES`
    # underflows and it asks `sum_bytes` for a span the frame does not have.
    result = _discharge("frame_checksum_bug.c")
    assert result.assessment is Assessment.VIOLATED, result.label
    assert "payload_checksum()" in result.label
    broken = {
        c.caller
        for c in result.callers
        if c.outcome is CallerOutcome.OBLIGATION_VIOLATED
    }
    assert broken == {"payload_checksum"}
    # the leaf is not at fault and its other caller still discharges
    assert result.unit_result.assessment is Assessment.ASSUMED_VERIFIED
    clean = {c.caller for c in result.callers if c.outcome is CallerOutcome.DISCHARGED}
    assert clean == {"frame_checksum"}


def test_the_users_source_is_never_modified() -> None:
    # RFC-0003 OQ1: contracts are injected into a generated *copy*.
    source = EXAMPLES / "frame_checksum.c"
    before = source.read_text()
    _discharge("frame_checksum.c")
    assert source.read_text() == before


def test_a_unit_with_no_caller_here_stays_honestly_assumed() -> None:
    # Nothing in sha1.c calls sha1_update, so this TU discharges nothing about
    # it — the obligation is exported to its clients, and saying otherwise would
    # be the over-claim S3 exists to prevent.
    result = discharge_precondition(
        EXAMPLES / "sha1.c", function="sha1_update", max_len=MAX_LEN
    )
    assert result.assessment is Assessment.ASSUMED_VERIFIED, result.label
    assert result.callers == ()
    assert "no caller" in result.label


_BARE_POINTER_CALLER = """\
#include <stddef.h>
#include <stdint.h>

uint32_t sum_bytes(const uint8_t *buf, size_t len) {
    uint32_t acc = 0;
    for (size_t i = 0; i < len; i++) acc += buf[i];
    return acc;
}

uint32_t hash_block(const uint8_t *blk) {
    return sum_bytes(blk, 16);
}
"""


def test_an_underdetermined_caller_is_not_a_phantom_violation(tmp_path: Path) -> None:
    # `hash_block` is *correct code* whose contract ("blk points to a 16-byte
    # block") its signature does not state, so L0 materialises it as one byte and
    # the obligation cannot hold. Reporting VIOLATED would move RFC-0003's phantom
    # from the callee to the call site; the honest answer is that this caller
    # discharges nothing and nobody is accused.
    src = tmp_path / "block.c"
    src.write_text(_BARE_POINTER_CALLER)
    result = discharge_precondition(src, function="sum_bytes", max_len=MAX_LEN)
    assert result.assessment is Assessment.ASSUMED_VERIFIED, result.label
    assert result.callers[0].outcome is CallerOutcome.UNDERDETERMINED
    assert "hash_block" in result.label


_HEADER_CALLER = """\
static inline uint32_t header_client(const uint8_t *p) {
    return sum_bytes(p, 64);
}
"""

_TU_WITH_HEADER_CALLER = """\
#include <stddef.h>
#include <stdint.h>

uint32_t sum_bytes(const uint8_t *buf, size_t len) {
    uint32_t acc = 0;
    for (size_t i = 0; i < len; i++) acc += buf[i];
    return acc;
}

#include "helper.h"

uint32_t frame_checksum(const uint8_t *frame, size_t len) {
    return sum_bytes(frame, len);
}
"""


def test_a_caller_defined_in_a_header_withholds_the_upgrade(tmp_path: Path) -> None:
    # `list_units` narrows to the file under test by design, so a `static inline`
    # caller in an included header is part of the translation unit but is not an
    # enumerable unit. Upgrading here would claim "every caller in this TU" about
    # a set that was never fully seen — so it is counted and the upgrade withheld,
    # even though the one caller we *can* check discharges cleanly.
    (tmp_path / "helper.h").write_text(_HEADER_CALLER)
    src = tmp_path / "tu.c"
    src.write_text(_TU_WITH_HEADER_CALLER)
    result = discharge_precondition(src, function="sum_bytes", max_len=MAX_LEN)
    assert result.assessment is Assessment.ASSUMED_VERIFIED, result.label
    outcomes = {c.caller: c.outcome for c in result.callers}
    assert outcomes["frame_checksum"] is CallerOutcome.DISCHARGED
    assert outcomes["header_client"] is CallerOutcome.UNCHECKED
    assert "defined outside" in result.label


_RECURSIVE_LEAF = """\
#include <stddef.h>
#include <stdint.h>

/* Terminating recursion that still breaks the L0 pair: advances two bytes while
 * shrinking `len` by one, so `(buf, len)` walks off the object — without ever
 * dereferencing it, so the callee's own S2 run is memory-safe. */
static uint32_t skip(const uint8_t *buf, size_t len) {
    if (len <= 1) return 0;
    return skip(buf + 2, len - 1);
}

uint32_t clean(const uint8_t *frame, size_t len) { return skip(frame, len); }
"""


def test_a_re_entry_that_breaks_the_obligation_names_the_recursion(
    tmp_path: Path,
) -> None:
    # `clean` hands `skip` exactly the object it was given — a valid caller. The
    # obligation fails inside its run all the same, because the assert sits at
    # `skip`'s entry and `skip` re-enters itself with a pair that walks off the
    # object. Blaming `clean` would accuse correct code; the recursion is named
    # instead, and `clean` is reported as not attributable.
    src = tmp_path / "recursive.c"
    src.write_text(_RECURSIVE_LEAF)
    result = discharge_precondition(src, function="skip", max_len=MAX_LEN)
    assert result.assessment is Assessment.VIOLATED, result.label
    outcomes = {c.caller: c.outcome for c in result.callers}
    assert outcomes["skip"] is CallerOutcome.OBLIGATION_VIOLATED
    assert outcomes["clean"] is CallerOutcome.UNATTRIBUTED
    assert "skip() passes skip()" in result.label
    assert "clean()" not in result.label


_BLOCK_SCOPE_DECL_CALLER = """\
#include <stddef.h>
#include <stdint.h>

static uint32_t sum_bytes(const uint8_t *buf, size_t len) {
    uint32_t acc = 0;
    for (size_t i = 0; i < len; i++) acc += buf[i];
    return acc;
}

uint32_t frame_checksum(const uint8_t *frame, size_t len) {
    return sum_bytes(frame, len);
}

uint32_t over_read(const uint8_t frame[4]) {
    extern void unrelated(void);   /* legal ISO C: a block-scope declaration */
    return sum_bytes(frame, 64);
}
"""


def test_a_caller_hidden_behind_a_block_scope_declaration_is_still_checked(
    tmp_path: Path,
) -> None:
    # clang nests the block-scope `FunctionDecl` inside `over_read`'s subtree, and
    # the call to `sum_bytes` comes after it. Closing `over_read` at the inner
    # declaration would drop that edge — leaving a caller that asks for 64 bytes
    # of a 4-byte frame out of the sweep entirely, while `frame_checksum` alone
    # carried the verdict to "discharged".
    src = tmp_path / "blockscope.c"
    src.write_text(_BLOCK_SCOPE_DECL_CALLER)
    result = discharge_precondition(src, function="sum_bytes", max_len=MAX_LEN)
    assert result.assessment is Assessment.VIOLATED, result.label
    outcomes = {c.caller: c.outcome for c in result.callers}
    assert outcomes["over_read"] is CallerOutcome.OBLIGATION_VIOLATED
    assert outcomes["frame_checksum"] is CallerOutcome.DISCHARGED


_ALIASED_LEAF = """\
#include <stddef.h>
#include <stdint.h>

static uint32_t sum_bytes(const uint8_t *buf, size_t len) {
    uint32_t acc = 0;
    for (size_t i = 0; i < len; i++) acc += buf[i];
    return acc;
}

static uint32_t sum_alias(const uint8_t *buf, size_t len)
    __attribute__((alias("sum_bytes")));

uint32_t frame_checksum(const uint8_t *frame, size_t len) {
    return sum_bytes(frame, len);
}

uint32_t via_alias(const uint8_t frame[4]) { return sum_alias(frame, 64); }
"""

_LABELLED_LEAF = """\
#include <stddef.h>
#include <stdint.h>

static uint32_t sum_bytes(const uint8_t *buf, size_t len) {
    uint32_t acc = 0;
    for (size_t i = 0; i < len; i++) acc += buf[i];
    return acc;
}

uint32_t sum_labelled(const uint8_t *buf, size_t len) __asm__("sum_bytes");

uint32_t frame_checksum(const uint8_t *frame, size_t len) {
    return sum_bytes(frame, len);
}

uint32_t via_label(const uint8_t frame[4]) { return sum_labelled(frame, 64); }
"""


def test_an_assembly_label_naming_the_leaf_opens_the_caller_set(
    tmp_path: Path,
) -> None:
    # Only `sum_labelled` carries a label, and it spells the leaf's own default
    # symbol name — so the two are one function at link time with nothing marked
    # on the leaf's side, and `via_label`'s call reaches it under a name the
    # enumeration never sees.
    src = tmp_path / "labelled.c"
    src.write_text(_LABELLED_LEAF)
    result = discharge_precondition(src, function="sum_bytes", max_len=MAX_LEN)
    assert result.assessment is Assessment.ASSUMED_VERIFIED, result.label
    outcomes = {c.caller: c.outcome for c in result.callers}
    assert outcomes["frame_checksum"] is CallerOutcome.DISCHARGED
    assert outcomes["sum_labelled"] is CallerOutcome.UNRESOLVED
    assert "another name for sum_bytes()" in result.label


def test_an_alias_opens_the_caller_set(tmp_path: Path) -> None:
    # `sum_alias` *is* `sum_bytes` at link time, but `via_alias`'s call references
    # only the alias — no AST edge joins it to the callee, and no address is taken
    # either. Upgrading on the strength of `frame_checksum` would claim a caller
    # set that was complete for just one of the function's two names.
    src = tmp_path / "aliased.c"
    src.write_text(_ALIASED_LEAF)
    result = discharge_precondition(src, function="sum_bytes", max_len=MAX_LEN)
    assert result.assessment is Assessment.ASSUMED_VERIFIED, result.label
    outcomes = {c.caller: c.outcome for c in result.callers}
    assert outcomes["frame_checksum"] is CallerOutcome.DISCHARGED
    assert outcomes["sum_alias"] is CallerOutcome.UNRESOLVED
    assert "another name for sum_bytes()" in result.label


_ALIAS_TO_LABELLED_LEAF = """\
#include <stddef.h>
#include <stdint.h>

static uint32_t sum_bytes(const uint8_t *buf, size_t len) __asm__("sum_impl");

static uint32_t sum_bytes(const uint8_t *buf, size_t len) {
    uint32_t acc = 0;
    for (size_t i = 0; i < len; i++) acc += buf[i];
    return acc;
}

static uint32_t sum_alias(const uint8_t *buf, size_t len)
    __attribute__((alias("sum_impl")));

uint32_t frame_checksum(const uint8_t *frame, size_t len) {
    return sum_bytes(frame, len);
}

uint32_t via_alias(const uint8_t frame[4]) { return sum_alias(frame, 64); }
"""


def test_an_alias_to_a_labelled_leaf_opens_the_caller_set(tmp_path: Path) -> None:
    # `sum_alias`'s alias attribute names `sum_bytes`'s *linker* symbol
    # ("sum_impl"), not its C name — a shape the alias/label fixes up to now left
    # uncovered, since the alias branch compared only against the bare C name.
    # Without the fix, `via_alias`'s 64-byte read of a 4-byte object is invisible
    # to the caller set and `frame_checksum` alone would carry `sum_bytes` to
    # DISCHARGED_VERIFIED.
    src = tmp_path / "alias_to_labelled.c"
    src.write_text(_ALIAS_TO_LABELLED_LEAF)
    result = discharge_precondition(src, function="sum_bytes", max_len=MAX_LEN)
    assert result.assessment is Assessment.ASSUMED_VERIFIED, result.label
    outcomes = {c.caller: c.outcome for c in result.callers}
    assert outcomes["frame_checksum"] is CallerOutcome.DISCHARGED
    assert outcomes["sum_alias"] is CallerOutcome.UNRESOLVED
    assert "another name for sum_bytes()" in result.label


_CLEANUP_HANDLER = """\
#include <stddef.h>
#include <stdint.h>

static void zero_byte(uint8_t *p) { *p = 0; }

void with_cleanup(void) {
    uint8_t c __attribute__((cleanup(zero_byte))) = 1;
    (void)c;
}

void clean(uint8_t *q) { zero_byte(q); }
"""


def test_a_cleanup_handler_opens_the_caller_set(tmp_path: Path) -> None:
    # `with_cleanup` calls `zero_byte` at scope exit, and no expression in the AST
    # spells that call — clang prints it as an attribute on the variable. Upgrading
    # on the strength of `clean` alone would claim a caller set that never included
    # the scope-exit invocation.
    src = tmp_path / "cleanup.c"
    src.write_text(_CLEANUP_HANDLER)
    result = discharge_precondition(src, function="zero_byte", max_len=MAX_LEN)
    assert result.assessment is Assessment.ASSUMED_VERIFIED, result.label
    outcomes = {c.caller: c.outcome for c in result.callers}
    assert outcomes["clean"] is CallerOutcome.DISCHARGED
    assert outcomes["c"] is CallerOutcome.UNRESOLVED
    assert "names zero_byte() outside a direct call" in result.label


_CONSTRUCTOR_TARGET = """\
#include <stddef.h>
#include <stdint.h>

static void zero_byte(uint8_t *p) __attribute__((constructor));
static void zero_byte(uint8_t *p) { *p = 0; }

void clean(uint8_t *q) { zero_byte(q); }
"""


def test_a_constructor_attribute_opens_the_caller_set(tmp_path: Path) -> None:
    # The loader invokes `zero_byte` directly at load time, with none of the
    # arguments `clean` supplies — a call to `clean` names nothing that joins it
    # to the constructor invocation, so upgrading on `clean` alone would claim a
    # caller set that never included the loader's own call.
    src = tmp_path / "constructor.c"
    src.write_text(_CONSTRUCTOR_TARGET)
    result = discharge_precondition(src, function="zero_byte", max_len=MAX_LEN)
    assert result.assessment is Assessment.ASSUMED_VERIFIED, result.label
    outcomes = {c.caller: c.outcome for c in result.callers}
    assert outcomes["clean"] is CallerOutcome.DISCHARGED
    assert outcomes["<constructor>"] is CallerOutcome.UNRESOLVED
    assert "carries a constructor attribute" in result.label


_PUBLIC_LEAF = """\
#include <stddef.h>
#include <stdint.h>

uint32_t sum_bytes(const uint8_t *buf, size_t len) {
    uint32_t acc = 0;
    for (size_t i = 0; i < len; i++) acc += buf[i];
    return acc;
}

uint32_t frame_checksum(const uint8_t *frame, size_t len) {
    return sum_bytes(frame, len);
}
"""


def test_an_externally_visible_leaf_exports_its_obligation(tmp_path: Path) -> None:
    # `examples/frame_checksum.c`'s leaf is `static`, which is what makes its two
    # callers *every* caller. Without it the same clean sweep proves strictly less:
    # any other translation unit of the program can call `sum_bytes` with anything,
    # so the obligation is exported there rather than discharged here.
    src = tmp_path / "public.c"
    src.write_text(_PUBLIC_LEAF)
    result = discharge_precondition(src, function="sum_bytes", max_len=MAX_LEN)
    assert result.assessment is Assessment.ASSUMED_VERIFIED, result.label
    assert result.callers[0].outcome is CallerOutcome.DISCHARGED
    assert "externally visible" in result.label


_TU_WITH_INDIRECT_CALLER = """\
#include <stddef.h>
#include <stdint.h>

uint32_t sum_bytes(const uint8_t *buf, size_t len) {
    uint32_t acc = 0;
    for (size_t i = 0; i < len; i++) acc += buf[i];
    return acc;
}

typedef uint32_t (*summer_t)(const uint8_t *, size_t);
static summer_t dispatch = sum_bytes;

uint32_t frame_checksum(const uint8_t *frame, size_t len) {
    return sum_bytes(frame, len);
}

uint32_t dispatch_checksum(const uint8_t *frame) {
    return dispatch(frame, 64);
}
"""


def test_an_escaped_address_withholds_the_upgrade(tmp_path: Path) -> None:
    # `dispatch_checksum` calls `sum_bytes` through a file-scope function pointer
    # and asks for 64 bytes of a pointer whose signature states one. Its body
    # names only `dispatch`, so no `Unit.calls` edge leads back to `sum_bytes`
    # and it is not among the callers checked — upgrading on the strength of the
    # one direct caller that *is* would claim a sweep that never saw this path.
    src = tmp_path / "tu.c"
    src.write_text(_TU_WITH_INDIRECT_CALLER)
    result = discharge_precondition(src, function="sum_bytes", max_len=MAX_LEN)
    assert result.assessment is Assessment.ASSUMED_VERIFIED, result.label
    outcomes = {c.caller: c.outcome for c in result.callers}
    assert outcomes["frame_checksum"] is CallerOutcome.DISCHARGED
    assert outcomes["dispatch"] is CallerOutcome.UNRESOLVED
    assert "names sum_bytes() outside a direct call" in result.label


_FILE_IDENTITY_TEMPLATE = """\
#include <string.h>
#include <stddef.h>
#include <stdint.h>

static uint32_t sum_bytes(const uint8_t *buf, size_t len) {{
    uint32_t acc = 0;
    for (size_t i = 0; i < len; i++) acc += buf[i];
    return acc;
}}

uint32_t frame_checksum(const uint8_t *frame, size_t len) {{
    __ESBMC_assert(strcmp(__FILE__, "{path}") == 0, "file identity preserved");
    return sum_bytes(frame, len);
}}
"""


def test_discharge_preserves_file_identity_in_the_generated_copy(
    tmp_path: Path,
) -> None:
    # The obligation-injected copy is written to disk under its own temp path,
    # not the source's. Without a `#line` directive, `__FILE__` inside
    # `frame_checksum`'s body — copied verbatim into that file — would report
    # the temp path instead, changing the program a `__FILE__`-dependent caller
    # is actually checked against. The path is computed *before* writing the
    # file, so the assert can only pass if the copy's `__FILE__` reports the
    # original source's resolved path, not wherever the copy itself lives.
    src = tmp_path / "file_identity.c"
    src.write_text(_FILE_IDENTITY_TEMPLATE.format(path=str(src.resolve())))
    result = discharge_precondition(src, function="sum_bytes", max_len=MAX_LEN)
    assert result.assessment is Assessment.DISCHARGED_VERIFIED, result.label
    assert result.callers[0].outcome is CallerOutcome.DISCHARGED
