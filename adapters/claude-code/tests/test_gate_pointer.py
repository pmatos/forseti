"""Tests for S1 (#124): pointer/array units become NEEDS_CONTRACT, not phantoms.

Function/signature enumeration is done by ``forseti list-units`` — ESBMC's own
clang frontend (#131) — not a regex, so the brittleness-class cases (typedef'd
pointers, K&R and multi-line signatures, function-like macros, a ``*`` inside a
comment) are classified correctly. The end-to-end cases below need ``esbmc`` +
``forseti`` on PATH; the wiring and error handling are covered fast with a fake
CLI so they run everywhere.

Run from the repo root with the dev venv (put its ``forseti`` first on PATH so a
broken launcher elsewhere is shadowed)::

    PATH=.venv/bin:$PATH .venv/bin/python -m pytest adapters/claude-code/tests -q
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import forseti_gate as gate
import pytest

_HAVE_ESBMC = shutil.which("esbmc") is not None and shutil.which("forseti") is not None


# --- fake `forseti list-units` CLI (no esbmc needed) -----------------------


def _fake_forseti_cmd(
    tmp_path: Path, *, stdout: str = "", stderr: str = "", exit_code: int = 0
) -> list[str]:
    """A stand-in for `resolve_forseti_cmd()` that emits canned CLI output.

    Writes a tiny Python script that ignores its argv and prints the given
    streams / exit code, so the gate's subprocess wiring is exercised for real
    (spawn, streams, returncode) without depending on esbmc.
    """
    script = tmp_path / "fake_forseti.py"
    script.write_text(
        "import sys\n"
        f"sys.stdout.write({stdout!r})\n"
        f"sys.stderr.write({stderr!r})\n"
        f"sys.exit({exit_code})\n"
    )
    return [sys.executable, str(script)]


def _argv_capturing_forseti_cmd(tmp_path: Path, dest: Path) -> list[str]:
    """A fake CLI recording its argv and the source's *neighbourhood* to `dest`.

    `siblings` maps each entry beside the file it was handed to that entry's
    real path — which is how a test checks that a snapshot's temp directory
    stands in for the source's own directory (`_enumerable_source` mirrors it
    with symlinks, so quoted `#include`s resolve exactly as they would in place).
    `ancestors` is the same walking upwards *lexically*, one record per level
    from the source's directory to ``/``: what a `..` in an `#include` would find
    if the caller normalized the path itself. `kernel_ancestors` is the walk the
    kernel actually performs — it resolves each component first, so a symlinked
    one sends the chain somewhere the lexical walk never goes. `islink`/`real`
    say which of the two a given level belongs to. A level that cannot be listed
    records ``None`` rather than aborting the capture.
    """
    script = tmp_path / "argv_forseti.py"
    script.write_text(
        "import json, os, sys\n"
        "def ents(p):\n"
        "    try:\n"
        "        return {e: os.path.realpath(os.path.join(p, e))"
        " for e in os.listdir(p)}\n"
        "    except OSError:\n"
        "        return None\n"
        "src = sys.argv[2]\n"
        "d = os.path.dirname(src)\n"
        "sib = ents(d)\n"
        "anc = []\n"
        "while d and d != os.sep:\n"
        "    anc.append({'path': d, 'islink': os.path.islink(d),"
        " 'real': os.path.realpath(d), 'entries': ents(d)})\n"
        "    d = os.path.dirname(d)\n"
        "k = os.path.realpath(os.path.dirname(src))\n"
        "ker = []\n"
        "while k and k != os.sep:\n"
        "    ker.append({'path': k, 'entries': ents(k)})\n"
        "    k = os.path.dirname(k)\n"
        f"open({str(dest)!r}, 'w').write("
        "json.dumps({'argv': sys.argv[1:], 'siblings': sib, 'ancestors': anc,"
        " 'kernel_ancestors': ker}))\n"
        "sys.stdout.write('{\"units\": []}')\n"
    )
    return [sys.executable, str(script)]


def _echoing_forseti_cmd(
    tmp_path: Path,
    *,
    before_read: str = "",
    after_read: str = "",
    during_verify: str = "",
    verdict: str = "violated",
) -> list[str]:
    """A fake CLI: `list-units` reports one unit per word of the file it was HANDED.

    Echoing the *content back as unit names* is what lets a test assert which
    bytes were actually enumerated — a canned payload cannot. `before_read` and
    `after_read` are Python statements run around that read, straddling it: this
    is the seam for rewriting the original source *while* the CLI is "parsing"
    it, which is the only ordering that reproduces the issue #141 interleaving (a
    rewrite that both starts and finishes before the read is not a race at all).
    `during_verify` is the same seam for the `verify` call. `verify` answers
    `verdict` (`violated` by default, so every enumerated unit ends up blocking).
    """
    script = tmp_path / "echo_forseti.py"
    body = [
        "import json, sys",
        "if sys.argv[1] == 'list-units':",
        "    src = sys.argv[2]",
        *(f"    {line}" for line in before_read.splitlines()),
        "    names = open(src).read().split()",
        *(f"    {line}" for line in after_read.splitlines()),
        "    print(json.dumps({'source': src, 'units': ["
        "{'function': n, 'takes_pointer': False} for n in names]}))",
        "else:",
        *(f"    {line}" for line in during_verify.splitlines()),
        "    fn = sys.argv[sys.argv.index('--function') + 1]",
        f"    print(json.dumps({{'verdict': {verdict!r}, 'unwind': 1, "
        "'counterexample': 'cex ' + fn}))",
    ]
    script.write_text("\n".join(body) + "\n")
    return [sys.executable, str(script)]


def _seed_scanned(tmp_path: Path) -> None:
    """Put a sentinel in `scanned` so a later "not stamped" assertion has teeth.

    Without it, `"x.c" not in state["scanned"]` would also hold when the key was
    never created at all, and the test would pass against an implementation that
    stamps unconditionally.
    """
    state = gate.load_state(str(tmp_path))
    state.setdefault("scanned", {})["sentinel.c"] = "deadbeef"
    gate.save_state(str(tmp_path), state)


def _units_payload(*units: tuple[str, bool]) -> str:
    """A `forseti list-units --json` payload for the given (name, takes_pointer)."""
    return json.dumps(
        {
            "source": "x.c",
            "units": [
                {"function": name, "takes_pointer": tp, "params": []}
                for name, tp in units
            ],
        }
    )


def test_extract_function_defs_parses_cli_json(tmp_path: Path, monkeypatch) -> None:
    payload = _units_payload(("a", False), ("b", True))
    monkeypatch.setattr(
        gate, "resolve_forseti_cmd", lambda: _fake_forseti_cmd(tmp_path, stdout=payload)
    )
    defs = gate.extract_function_defs(str(tmp_path / "x.c"), project_dir=str(tmp_path))
    assert [(d.name, d.takes_pointer) for d in defs] == [("a", False), ("b", True)]


def test_extract_functions_returns_names(tmp_path: Path, monkeypatch) -> None:
    payload = _units_payload(("a", True), ("b", False))
    monkeypatch.setattr(
        gate, "resolve_forseti_cmd", lambda: _fake_forseti_cmd(tmp_path, stdout=payload)
    )
    assert gate.extract_functions(str(tmp_path / "x.c"), project_dir=str(tmp_path)) == [
        "a",
        "b",
    ]


@pytest.mark.parametrize(
    "budget, expected", [(0.5, "0.5"), (2.5, "2.5"), (30.0, "30.0")]
)
def test_list_units_timeout_reaches_the_cli_unrounded(
    tmp_path: Path, monkeypatch, budget: float, expected: str
) -> None:
    # `list-units` passes its --timeout straight to `subprocess.run(timeout=...)`,
    # where 0 expires immediately (esbmc's own --timeout, which reads 0 as
    # unbounded and is clamped to >=1s by the runner, is not used on the
    # parse-tree-only path). Truncating with `int()` therefore turned a sub-second
    # budget into a blocking `error` on every edited `.c`, so the float must
    # survive the trip to the CLI intact.
    dest = tmp_path / "argv.json"
    monkeypatch.setattr(gate, "LIST_UNITS_TIMEOUT_S", budget)
    monkeypatch.setattr(
        gate,
        "resolve_forseti_cmd",
        lambda: _argv_capturing_forseti_cmd(tmp_path, dest),
    )
    src = tmp_path / "x.c"
    src.write_text("int f(void) { return 0; }\n")
    assert gate.extract_function_defs(str(src), project_dir=str(tmp_path)) == []
    argv = json.loads(dest.read_text())["argv"]
    assert argv[argv.index("--timeout") + 1] == expected


def test_extract_function_defs_raises_on_nonzero_exit(
    tmp_path: Path, monkeypatch
) -> None:
    # A failed parse (nonzero exit) must raise, never be read as "no units" — the
    # latter would let the gate silently skip a unit.
    monkeypatch.setattr(
        gate,
        "resolve_forseti_cmd",
        lambda: _fake_forseti_cmd(tmp_path, stderr="ERROR: parse failed", exit_code=1),
    )
    with pytest.raises(gate.UnitsUnavailable, match="parse failed"):
        gate.extract_function_defs(str(tmp_path / "x.c"), project_dir=str(tmp_path))


def test_extract_function_defs_raises_on_bad_json(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        gate,
        "resolve_forseti_cmd",
        lambda: _fake_forseti_cmd(tmp_path, stdout="not json at all"),
    )
    with pytest.raises(gate.UnitsUnavailable):
        gate.extract_function_defs(str(tmp_path / "x.c"), project_dir=str(tmp_path))


def test_extract_function_defs_raises_on_malformed_payload(
    tmp_path: Path, monkeypatch
) -> None:
    # Valid JSON, wrong shape (a units entry missing `function`) must degrade to a
    # blocking UnitsUnavailable, never crash the hook with KeyError/TypeError.
    bad = '{"units": [{"takes_pointer": true}]}'
    monkeypatch.setattr(
        gate, "resolve_forseti_cmd", lambda: _fake_forseti_cmd(tmp_path, stdout=bad)
    )
    with pytest.raises(gate.UnitsUnavailable):
        gate.extract_function_defs(str(tmp_path / "x.c"), project_dir=str(tmp_path))


@pytest.mark.parametrize(
    "takes_pointer",
    [
        '"false"',  # a truthy string — `bool()` would invert the classification
        '"true"',
        "1",  # int, not bool: `isinstance(1, bool)` is False
        "0",
        "null",
        '"pointer"',
        "[]",
    ],
)
def test_extract_function_defs_raises_on_non_boolean_takes_pointer(
    tmp_path: Path, monkeypatch, takes_pointer: str
) -> None:
    # `takes_pointer` decides whether a unit is verified or parked in non-blocking
    # NEEDS_CONTRACT, so coercing it is unsafe: `bool("false")` is True, which would
    # make a scalar function look pointer-taking and skip its ESBMC run entirely.
    # Anything but a JSON boolean is an unusable payload → blocking UnitsUnavailable.
    bad = f'{{"units": [{{"function": "f", "takes_pointer": {takes_pointer}}}]}}'
    monkeypatch.setattr(
        gate, "resolve_forseti_cmd", lambda: _fake_forseti_cmd(tmp_path, stdout=bad)
    )
    with pytest.raises(gate.UnitsUnavailable, match="takes_pointer"):
        gate.extract_function_defs(str(tmp_path / "x.c"), project_dir=str(tmp_path))


def test_extract_function_defs_raises_on_missing_takes_pointer(
    tmp_path: Path, monkeypatch
) -> None:
    # An absent `takes_pointer` used to default to False ("scalar"), silently
    # gating a unit whose classification the CLI never actually reported. Like an
    # absent `units` key, absence blocks rather than picking a default.
    bad = '{"units": [{"function": "f"}]}'
    monkeypatch.setattr(
        gate, "resolve_forseti_cmd", lambda: _fake_forseti_cmd(tmp_path, stdout=bad)
    )
    with pytest.raises(gate.UnitsUnavailable, match="takes_pointer"):
        gate.extract_function_defs(str(tmp_path / "x.c"), project_dir=str(tmp_path))


@pytest.mark.parametrize(
    "function",
    ["123", "null", '""', '["f"]'],  # non-string or empty name
)
def test_extract_function_defs_raises_on_bad_function_name(
    tmp_path: Path, monkeypatch, function: str
) -> None:
    # A non-string name was previously coerced with `str()`, so `123` became the
    # unit "123" and `forseti verify --function 123` would fail downstream with a
    # confusing error. Reject it at the payload boundary instead.
    bad = f'{{"units": [{{"function": {function}, "takes_pointer": false}}]}}'
    monkeypatch.setattr(
        gate, "resolve_forseti_cmd", lambda: _fake_forseti_cmd(tmp_path, stdout=bad)
    )
    with pytest.raises(gate.UnitsUnavailable, match="function"):
        gate.extract_function_defs(str(tmp_path / "x.c"), project_dir=str(tmp_path))


def test_extract_function_defs_raises_on_non_object_units_entry(
    tmp_path: Path, monkeypatch
) -> None:
    # A list-valued `units` whose entries are scalars must not crash the hook with
    # AttributeError/TypeError — it degrades to a blocking UnitsUnavailable.
    monkeypatch.setattr(
        gate,
        "resolve_forseti_cmd",
        lambda: _fake_forseti_cmd(tmp_path, stdout='{"units": ["f", 2]}'),
    )
    with pytest.raises(gate.UnitsUnavailable):
        gate.extract_function_defs(str(tmp_path / "x.c"), project_dir=str(tmp_path))


def test_non_boolean_takes_pointer_records_blocking_error(
    tmp_path: Path, monkeypatch
) -> None:
    # End to end: the reviewer's scenario — a list-valued `units` with the truthy
    # string "false" — must persist a blocking `error` verdict, never a silent pass
    # and never a non-blocking NEEDS_CONTRACT.
    bad = '{"units": [{"function": "f", "takes_pointer": "false"}]}'
    monkeypatch.setattr(
        gate, "resolve_forseti_cmd", lambda: _fake_forseti_cmd(tmp_path, stdout=bad)
    )
    src = tmp_path / "x.c"
    src.write_text("int f(int x) { return x; }\n")
    verdicts = gate.verify_and_record(str(src), project_dir=str(tmp_path))
    assert [v.verdict for v in verdicts] == ["error"]
    state = gate.load_state(str(tmp_path))
    assert gate.blocking_units(state)  # non-empty → the Stop-gate blocks
    assert gate.needs_contract_units(state) == []


@pytest.mark.parametrize(
    "stdout",
    [
        '{"source": "x.c"}',  # no `units` key (older/incompatible build)
        '{"units": null}',  # present but null
        '{"units": "f"}',  # present but not a list
        "[]",  # a JSON array, not an object
        '"ok"',  # a JSON scalar
    ],
)
def test_extract_function_defs_raises_when_units_absent_or_not_a_list(
    tmp_path: Path, monkeypatch, stdout: str
) -> None:
    # An exit-0 payload without a list-valued `units` (e.g. an older `forseti`
    # build) must NOT default to "no units" — that would let an edited `.c` pass
    # unverified. It has to surface as a blocking UnitsUnavailable.
    monkeypatch.setattr(
        gate, "resolve_forseti_cmd", lambda: _fake_forseti_cmd(tmp_path, stdout=stdout)
    )
    with pytest.raises(gate.UnitsUnavailable):
        gate.extract_function_defs(str(tmp_path / "x.c"), project_dir=str(tmp_path))


def test_extract_function_defs_empty_units_is_a_clean_pass(
    tmp_path: Path, monkeypatch
) -> None:
    # An *empty* list is a legitimate "file defines no functions" pass — only an
    # absent/non-list `units` blocks. Guards against over-rejecting the empty case.
    monkeypatch.setattr(
        gate,
        "resolve_forseti_cmd",
        lambda: _fake_forseti_cmd(tmp_path, stdout='{"source": "x.c", "units": []}'),
    )
    assert (
        gate.extract_function_defs(str(tmp_path / "x.c"), project_dir=str(tmp_path))
        == []
    )


def test_enumeration_parses_the_given_content_not_a_re_read(
    tmp_path: Path, monkeypatch
) -> None:
    # `content=` is the fix for issue #141: those exact bytes are what gets
    # parsed, so no re-read of the path can substitute different ones. Here the
    # on-disk bytes and the passed bytes disagree — the passed bytes must win,
    # and the original must be left alone.
    src = tmp_path / "x.c"
    src.write_text("ondisk\n")
    monkeypatch.setattr(
        gate, "resolve_forseti_cmd", lambda: _echoing_forseti_cmd(tmp_path)
    )
    defs = gate.extract_function_defs(
        str(src), project_dir=str(tmp_path), content=b"snapshot\n"
    )
    assert [d.name for d in defs] == ["snapshot"]
    assert src.read_text() == "ondisk\n"


def test_snapshot_directory_stands_in_for_the_sources_own(
    tmp_path: Path, monkeypatch
) -> None:
    # clang resolves a quoted `#include "sibling.h"` against the directory of the
    # file it is reading, so the snapshot has to sit in an equivalent directory:
    # every entry beside the source is mirrored into the temp dir. Crucially this
    # needs NO `-I` — see the next test for why a flag is not a substitute.
    dest = tmp_path / "argv.json"
    monkeypatch.delenv("FORSETI_BUILD_FLAGS", raising=False)
    monkeypatch.setattr(
        gate,
        "resolve_forseti_cmd",
        lambda: _argv_capturing_forseti_cmd(tmp_path, dest),
    )
    sub = tmp_path / "sub"
    (sub / "nested").mkdir(parents=True)
    (sub / "helper.h").write_text("#define HELP 1\n")
    (sub / "nested" / "deep.h").write_text("#define DEEP 2\n")
    src = sub / "x.c"
    src.write_text("int f(void) { return 0; }\n")

    gate.extract_function_defs(str(src), project_dir=str(tmp_path), content=b"f\n")

    captured = json.loads(dest.read_text())
    enumerated = Path(captured["argv"][1])
    assert enumerated != src and enumerated.name == "x.c"  # a recognisable name
    # The sibling header and the subdirectory both resolve to the real ones...
    assert captured["siblings"]["helper.h"] == str((sub / "helper.h").resolve())
    assert captured["siblings"]["nested"] == str((sub / "nested").resolve())
    # ...and the source's own name is the snapshot, not a link back to the file.
    assert captured["siblings"]["x.c"] != str(src.resolve())
    assert "--" not in captured["argv"]  # no include flag invented
    assert not enumerated.exists()  # the temp mirror is cleaned up


def test_snapshot_does_not_disturb_angle_include_or_iquote_precedence(
    tmp_path: Path, monkeypatch
) -> None:
    # Why the directory is mirrored rather than named with `-I`: `-I` also joins
    # the *angle-bracket* search, and lands after any `-iquote` from
    # FORSETI_BUILD_FLAGS instead of taking the source directory's first-place
    # precedence. With `#include <config.h>` next to a generated `config.h` —
    # the standard shape — either one selects a different header than the
    # in-place parse, flipping `#if` branches so enumeration reports units the
    # verify never sees. The build flags must reach esbmc untouched and alone.
    dest = tmp_path / "argv.json"
    monkeypatch.setenv("FORSETI_BUILD_FLAGS", "-Igenerated -iquote quoted -DWIDGET")
    monkeypatch.setattr(
        gate,
        "resolve_forseti_cmd",
        lambda: _argv_capturing_forseti_cmd(tmp_path, dest),
    )
    src = tmp_path / "x.c"
    src.write_text("int f(void) { return 0; }\n")

    gate.extract_function_defs(str(src), project_dir=str(tmp_path), content=b"f\n")

    argv = json.loads(dest.read_text())["argv"]
    assert argv[argv.index("--") + 1 :] == [
        "-Igenerated",
        "-iquote",
        "quoted",
        "-DWIDGET",
    ]


def test_snapshot_mirrors_the_lexical_directory_not_the_symlink_target(
    tmp_path: Path, monkeypatch
) -> None:
    # clang searches the directory of the path it was *given*, so for a symlinked
    # source that is the link's directory, not the target's. Mirroring the
    # resolved one would drop a header beside the link and silently prefer a
    # same-named header beside the target — enumerating units the in-place parse
    # never sees.
    dest = tmp_path / "argv.json"
    monkeypatch.delenv("FORSETI_BUILD_FLAGS", raising=False)
    monkeypatch.setattr(
        gate,
        "resolve_forseti_cmd",
        lambda: _argv_capturing_forseti_cmd(tmp_path, dest),
    )
    real = tmp_path / "real"
    real.mkdir()
    (real / "x.c").write_text("int f(void) { return 0; }\n")
    (real / "config.h").write_text("#define WHICH 2\n")  # must NOT be mirrored
    link_dir = tmp_path / "linked"
    link_dir.mkdir()
    (link_dir / "config.h").write_text("#define WHICH 1\n")  # this one must be
    (link_dir / "x.c").symlink_to(real / "x.c")

    gate.extract_function_defs(
        str(link_dir / "x.c"), project_dir=str(tmp_path), content=b"f\n"
    )

    siblings = json.loads(dest.read_text())["siblings"]
    assert siblings["config.h"] == str((link_dir / "config.h").resolve())


def test_snapshot_mirrors_the_dir_resolved_against_project_dir(
    tmp_path: Path, monkeypatch
) -> None:
    # The CLI subprocess runs with cwd=project_dir, so a relative `file_path` is
    # relative to *that*, not to the hook process's cwd. A bare `os.path.abspath`
    # would mirror whatever directory the hook happened to start in.
    dest = tmp_path / "argv.json"
    monkeypatch.delenv("FORSETI_BUILD_FLAGS", raising=False)
    monkeypatch.setattr(
        gate,
        "resolve_forseti_cmd",
        lambda: _argv_capturing_forseti_cmd(tmp_path, dest),
    )
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "x.c").write_text("int f(void) { return 0; }\n")
    (sub / "helper.h").write_text("#define HELP 1\n")

    gate.extract_function_defs("sub/x.c", project_dir=str(tmp_path), content=b"f\n")

    siblings = json.loads(dest.read_text())["siblings"]
    assert siblings["helper.h"] == str((sub / "helper.h").resolve())


def test_snapshot_mirrors_the_ancestry_up_to_the_project_dir(
    tmp_path: Path, monkeypatch
) -> None:
    # `#include "../common.h"` — the ordinary shape for a `src/foo.c` — resolves
    # against the *parent* of the source's directory. Mirroring only the siblings
    # left it unresolvable, so `list-units` failed on a translation unit that
    # parses perfectly in place and the gate recorded a blocking `error`. The
    # chain from the project dir down is mirrored too: each level carries its own
    # entries, minus the one the chain continues through.
    dest = tmp_path / "argv.json"
    monkeypatch.delenv("FORSETI_BUILD_FLAGS", raising=False)
    monkeypatch.setattr(
        gate,
        "resolve_forseti_cmd",
        lambda: _argv_capturing_forseti_cmd(tmp_path, dest),
    )
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "common.h").write_text("#define COMMON 1\n")
    (proj / "include").mkdir()
    src = proj / "src" / "x.c"
    src.write_text("int f(void) { return 0; }\n")

    gate.extract_function_defs(str(src), project_dir=str(proj), content=b"f\n")

    captured = json.loads(dest.read_text())
    staged = Path(captured["argv"][1])
    src_level, proj_level = captured["ancestors"][0], captured["ancestors"][1]
    assert proj_level["entries"]["common.h"] == str((proj / "common.h").resolve())
    assert proj_level["entries"]["include"] == str((proj / "include").resolve())
    # `src` on the chain is the mirror the snapshot sits in, NOT a link back to
    # the real directory — a link there would put the real `x.c` beside it.
    assert proj_level["entries"]["src"] == str(staged.parent.resolve())
    # Every step is a real directory, so `..` walks the mirror whether the caller
    # normalizes `foo/../bar` lexically or leaves it to the kernel.
    assert not src_level["islink"] and not proj_level["islink"]


def test_snapshot_reproduces_a_symlinked_directory_component(
    tmp_path: Path, monkeypatch
) -> None:
    # A `..` climbing past a *symlinked* component does not climb the spelled
    # chain: the kernel resolves the component first, so `link/src/../..` lands
    # beside the link's target. clang hands the concatenated path straight to
    # `open`, so that is the resolution the in-place parse gets. Reproducing the
    # component as a real directory would send the climb up the spelled chain
    # instead and silently select a different header — a different translation
    # unit, which enumeration then prunes and stamps against.
    dest = tmp_path / "argv.json"
    monkeypatch.delenv("FORSETI_BUILD_FLAGS", raising=False)
    monkeypatch.setattr(
        gate,
        "resolve_forseti_cmd",
        lambda: _argv_capturing_forseti_cmd(tmp_path, dest),
    )
    proj = tmp_path / "proj"
    pkg = proj / "vendor" / "pkg"
    (pkg / "src").mkdir(parents=True)
    (pkg / "common.h").write_text("#define COMMON 1\n")
    (proj / "vendor" / "selector.h").write_text("#define WHICH 1\n")  # the real pick
    (proj / "selector.h").write_text("#define WHICH 2\n")  # the spelled-chain decoy
    (proj / "link").symlink_to(pkg)
    src = proj / "link" / "src" / "x.c"
    src.write_text("int f(void) { return 0; }\n")

    gate.extract_function_defs(str(src), project_dir=str(proj), content=b"f\n")

    captured = json.loads(dest.read_text())
    src_level, link_level, proj_level = captured["ancestors"][:3]
    kernel = captured["kernel_ancestors"]
    # The component is a link in the mirror too, so the kernel walk leaves the
    # spelled chain exactly where it does in place...
    assert link_level["islink"]
    assert os.path.dirname(src_level["real"]) == link_level["real"]
    assert os.path.dirname(link_level["real"]) != proj_level["real"]
    # ...landing on the target's own parent, where `../../selector.h` finds the
    # header the in-place parse finds — not the project's decoy.
    assert kernel[2]["entries"]["selector.h"] == str(
        (proj / "vendor" / "selector.h").resolve()
    )
    # The target's entries are mirrored, so a one-level `#include "../common.h"`
    # keeps resolving (it already did — mirroring scandirs through the link).
    assert kernel[1]["entries"]["common.h"] == str((pkg / "common.h").resolve())
    # And the spelled chain survives as spelled, for a caller that normalizes
    # `foo/../bar` itself: lexically two levels up is still the project dir.
    assert proj_level["entries"]["selector.h"] == str((proj / "selector.h").resolve())


def test_snapshot_reproduces_the_sources_absolute_depth(
    tmp_path: Path, monkeypatch
) -> None:
    # An `#include` that climbs past the mirror root has to land somewhere
    # private and empty. Staging at the temp root itself would make `../x.h`
    # resolve into `/tmp` — world-writable, so anyone's same-named header would
    # be included and the gate would enumerate a *different* translation unit.
    # That is worse than the blocking `error` an unresolved include produces, so
    # the source's absolute path is reproduced under the temp root and the levels
    # above the mirror root are left empty.
    dest = tmp_path / "argv.json"
    monkeypatch.delenv("FORSETI_BUILD_FLAGS", raising=False)
    monkeypatch.setattr(
        gate,
        "resolve_forseti_cmd",
        lambda: _argv_capturing_forseti_cmd(tmp_path, dest),
    )
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    src = proj / "src" / "x.c"
    src.write_text("int f(void) { return 0; }\n")

    gate.extract_function_defs(str(src), project_dir=str(proj), content=b"f\n")

    captured = json.loads(dest.read_text())
    staged = captured["argv"][1]
    assert staged.endswith(str(src)) and staged != str(src)  # padded, not flattened
    above_root = captured["ancestors"][2]  # one level above the mirrored project
    assert list(above_root["entries"]) == ["proj"]  # nothing real to climb into


def test_snapshot_ancestry_stops_at_the_mirror_root(
    tmp_path: Path, monkeypatch
) -> None:
    # A source outside the project has no ancestry the gate can claim, and
    # walking to `/` would mean a `scandir` of `$HOME` on every edit (thousands
    # of entries, and an unreadable one becomes a blocking `error`). Mirroring
    # stops at the source's own directory: siblings yes, parent's entries no.
    dest = tmp_path / "argv.json"
    monkeypatch.delenv("FORSETI_BUILD_FLAGS", raising=False)
    monkeypatch.setattr(
        gate,
        "resolve_forseti_cmd",
        lambda: _argv_capturing_forseti_cmd(tmp_path, dest),
    )
    proj = tmp_path / "proj"
    proj.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "helper.h").write_text("#define HELP 1\n")
    src = outside / "x.c"
    src.write_text("int f(void) { return 0; }\n")

    gate.extract_function_defs(str(src), project_dir=str(proj), content=b"f\n")

    captured = json.loads(dest.read_text())
    assert captured["siblings"]["helper.h"] == str((outside / "helper.h").resolve())
    assert list(captured["ancestors"][1]["entries"]) == ["outside"]  # not mirrored


def test_in_place_enumeration_uses_no_snapshot(tmp_path: Path, monkeypatch) -> None:
    # Without `content=` nothing is staged — the file is parsed where it lies,
    # exactly as before.
    dest = tmp_path / "argv.json"
    monkeypatch.delenv("FORSETI_BUILD_FLAGS", raising=False)
    monkeypatch.setattr(
        gate,
        "resolve_forseti_cmd",
        lambda: _argv_capturing_forseti_cmd(tmp_path, dest),
    )
    src = tmp_path / "x.c"
    src.write_text("int f(void) { return 0; }\n")
    gate.extract_function_defs(str(src), project_dir=str(tmp_path))
    argv = json.loads(dest.read_text())["argv"]
    assert argv[1] == str(src)
    assert "--" not in argv


def _unstageable_snapshot(monkeypatch, message: str = "No space left") -> None:
    """Make staging the snapshot fail with a bare `OSError`: ENOSPC, bad TMPDIR, ..."""

    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError(message)

    monkeypatch.setattr(gate.tempfile, "TemporaryDirectory", _boom)


def test_unstageable_snapshot_raises_units_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    # An unwritable/missing TMPDIR, ENOSPC or EDQUOT must surface as the one
    # exception the gate knows how to block on. A bare OSError escapes to the
    # hook, which installs no handler — the process would die with a traceback
    # and exit 1 rather than the blocking exit 2.
    _unstageable_snapshot(monkeypatch)
    src = tmp_path / "x.c"
    src.write_text("int f(void) { return 0; }\n")
    with pytest.raises(gate.UnitsUnavailable, match="snapshot"):
        gate.extract_function_defs(
            str(src), project_dir=str(tmp_path), content=b"int f(void){return 0;}\n"
        )


def test_unstageable_snapshot_blocks_and_does_not_stamp(
    tmp_path: Path, monkeypatch
) -> None:
    # End to end, the fail-closed half: a file whose snapshot could not be staged
    # is never enumerated, so it must record a blocking `error` and must NOT be
    # stamped `scanned` — a stamp would let the out-of-band scan dedup the edit
    # as already handled, and outside a git work tree there is no scan at all.
    _unstageable_snapshot(monkeypatch)
    src = tmp_path / "x.c"
    src.write_text("int f(void) { return 0; }\n")
    _seed_scanned(tmp_path)

    verdicts = gate.verify_and_record(str(src), project_dir=str(tmp_path))

    assert [v.verdict for v in verdicts] == ["error"]
    after = gate.load_state(str(tmp_path))
    assert "x.c" not in after["scanned"]
    assert after["scanned"]["sentinel.c"] == "deadbeef"
    assert gate.blocking_units(after)


def test_transient_rewrite_during_enumeration_is_enumerated_faithfully(
    tmp_path: Path, monkeypatch
) -> None:
    # The A -> B -> A interleaving: a concurrent `> f.c` rewrite passes through a
    # zero-byte instant, which enumerates as a *successful* empty list. Re-reading
    # the path would prune every unit the file has — dropping an already-recorded
    # blocking verdict — and then stamp `scanned` with the restored content's
    # digest, leaving the Stop-gate and the out-of-band scan both satisfied.
    # Enumerating a snapshot of the hashed bytes makes that impossible, and makes
    # it so deterministically: the old metadata guard only noticed the rewrite
    # when the filesystem's timestamp granularity was fine enough to resolve it.
    src = tmp_path / "x.c"
    original = "alpha beta\n"
    src.write_text(original)
    _seed_scanned(tmp_path)

    monkeypatch.setattr(
        gate,
        "resolve_forseti_cmd",
        lambda: _echoing_forseti_cmd(
            tmp_path,
            before_read=f"open({str(src)!r}, 'w').write('')",  # truncated...
            after_read=f"open({str(src)!r}, 'w').write({original!r})",  # ...restored
        ),
    )
    verdicts = gate.verify_and_record(str(src), project_dir=str(tmp_path))

    assert sorted(v.function for v in verdicts) == ["alpha", "beta"]
    after = gate.load_state(str(tmp_path))
    assert sorted(after["units"]) == ["x.c::alpha", "x.c::beta"]
    assert len(gate.blocking_units(after)) == 2
    # Stamped with the digest of the content that was actually enumerated.
    assert after["scanned"]["x.c"] == hashlib.sha256(original.encode()).hexdigest()


def test_persistent_rewrite_during_enumeration_does_not_prune_or_stamp(
    tmp_path: Path, monkeypatch
) -> None:
    # A rewrite that lands and *stays* is a different failure: we enumerated A but
    # the file now holds B, so recording A's units against B would be a lie. The
    # post-enumeration content re-hash fails closed on it — by content, so it
    # holds on a coarse-timestamp filesystem, and without depending on the
    # out-of-band scan to re-gate B later (which it cannot do outside a git tree).
    src = tmp_path / "x.c"
    src.write_text("alpha\n")
    state = gate.load_state(str(tmp_path))
    gate.record(state, gate.UnitVerdict("x.c::alpha", "x.c", "alpha", "violated", 4))
    gate.save_state(str(tmp_path), state)
    _seed_scanned(tmp_path)

    monkeypatch.setattr(
        gate,
        "resolve_forseti_cmd",
        lambda: _echoing_forseti_cmd(
            tmp_path,
            before_read=f"open({str(src)!r}, 'w').write('beta\\n')",  # and left there
        ),
    )
    verdicts = gate.verify_and_record(str(src), project_dir=str(tmp_path))

    assert [v.verdict for v in verdicts] == ["error"]
    after = gate.load_state(str(tmp_path))
    assert "x.c" not in after["scanned"]  # never stamped as handled
    assert after["scanned"]["sentinel.c"] == "deadbeef"
    assert gate.blocking_units(after)  # the pre-existing violation survived
    assert any(u.get("verdict") == "violated" for u in after["units"].values())


def test_persistent_rewrite_during_verify_withdraws_the_stamp_and_blocks(
    tmp_path: Path, monkeypatch
) -> None:
    # The verifies read the *real* path — a verdict must describe the translation
    # unit that ships — so a rewrite landing there can attach a `verified` verdict
    # to bytes the gate never stamped. Re-hashing once after the loop fails closed
    # on a rewrite that lands and *stays*: the up-front stamp is withdrawn, so the
    # out-of-band scan re-gates the file, and a blocking `error` covers the case
    # where there is no such scan (outside a git work tree). A transient
    # A -> B -> A during a verify is the acknowledged residual (see below) — the
    # final bytes compare equal, so nothing here can see it.
    src = tmp_path / "x.c"
    src.write_text("alpha\n")
    _seed_scanned(tmp_path)

    monkeypatch.setattr(
        gate,
        "resolve_forseti_cmd",
        lambda: _echoing_forseti_cmd(
            tmp_path,
            verdict="verified",  # a clean verdict — for content no longer on disk
            during_verify=f"open({str(src)!r}, 'w').write('beta\\n')",
        ),
    )
    verdicts = gate.verify_and_record(str(src), project_dir=str(tmp_path))

    assert [v.verdict for v in verdicts] == ["error"]
    after = gate.load_state(str(tmp_path))
    assert "x.c" not in after["scanned"]  # the up-front stamp was withdrawn
    assert after["scanned"]["sentinel.c"] == "deadbeef"
    assert gate.blocking_units(after)  # not left passing on a stale `verified`
    # The unfinished-verify marker (#140) is released too. A claim means "a run
    # started verifying these bytes and never finished", which is what makes the
    # next scan retry; this run *did* finish — it concluded the verdicts cannot
    # be trusted and said so with a blocking `error`. Only a kill should leave a
    # claim behind.
    assert "x.c" not in after["pending"]


def test_rewrite_during_verify_releases_the_claim_when_deferring(
    tmp_path: Path, monkeypatch
) -> None:
    # The other drift exit: a concurrent run owns the stamp, so this run neither
    # withdraws nor blocks. It has still finished, so its own claim must be
    # released — while `_pending_owner` keeps it from touching the *other* run's.
    src = tmp_path / "x.c"
    src.write_text("alpha\n")
    _seed_scanned(tmp_path)
    state_file = tmp_path / ".forseti" / "gate_state.json"
    beta_digest = hashlib.sha256(b"beta\n").hexdigest()

    monkeypatch.setattr(
        gate,
        "resolve_forseti_cmd",
        lambda: _echoing_forseti_cmd(
            tmp_path,
            verdict="verified",
            during_verify=(
                f"open({str(src)!r}, 'w').write('beta\\n')\n"
                "import json, pathlib\n"
                f"_p = pathlib.Path({str(state_file)!r})\n"
                "_st = json.loads(_p.read_text())\n"
                f"_st['scanned']['x.c'] = {beta_digest!r}\n"
                # The concurrent run's own claim, which this run must not clear.
                f"_st['pending']['x.c'] = {{'hash': {beta_digest!r}, "
                "'attempts': 1, 'pid': 999999}\n"
                "_p.write_text(json.dumps(_st))"
            ),
        ),
    )
    gate.verify_and_record(str(src), project_dir=str(tmp_path))

    after = gate.load_state(str(tmp_path))
    assert after["scanned"]["x.c"] == beta_digest  # the other run's stamp survives
    assert after["pending"]["x.c"] == {  # ...and so does its claim, untouched
        "hash": beta_digest,
        "attempts": 1,
        "pid": 999999,
    }


def test_transient_rewrite_during_verify_is_a_known_residual(
    tmp_path: Path, monkeypatch
) -> None:
    # Characterization, not an endorsement. A transient A -> B -> A *during a
    # verify* leaves B's verdict attached to A's stamp: the post-loop re-hash can
    # only compare final bytes, and they are equal. Closing it would mean
    # verifying immutable content, which changes *what* is verified — the
    # snapshot reproduces include resolution only up to its mirror root, so an
    # include reaching above that would turn a verifiable file into a blocking
    # `error` — and would put a since-deleted temp path in every counterexample
    # and in the trace's
    # `argv`. Pinned here so the limit is explicit in the suite, and so a future
    # fix flips this test rather than quietly widening the guarantee.
    src = tmp_path / "x.c"
    original = "alpha\n"
    src.write_text(original)

    monkeypatch.setattr(
        gate,
        "resolve_forseti_cmd",
        lambda: _echoing_forseti_cmd(
            tmp_path,
            verdict="verified",
            during_verify=(
                f"open({str(src)!r}, 'w').write('beta\\n')\n"  # what got verified
                f"open({str(src)!r}, 'w').write({original!r})"  # ...then restored
            ),
        ),
    )
    verdicts = gate.verify_and_record(str(src), project_dir=str(tmp_path))

    assert [v.verdict for v in verdicts] == ["verified"]
    after = gate.load_state(str(tmp_path))
    assert after["scanned"]["x.c"] == hashlib.sha256(original.encode()).hexdigest()
    assert gate.blocking_units(after) == []  # the residual: A passes on B's verdict


def test_rewrite_during_verify_defers_to_a_concurrent_runs_stamp(
    tmp_path: Path, monkeypatch
) -> None:
    # Withdrawing the stamp must be ownership-scoped, and so must the block.
    # Concurrent hooks share gate_state.json, so a run that has since re-stamped
    # its own digest owns the entry — popping it would re-gate content that run
    # legitimately verified, and blocking anyway would strand a `x.c::?` error
    # nothing can clear: the file now hashes equal to the surviving stamp, so the
    # out-of-band scan reads it as fresh and never re-runs the reconcile that
    # prunes `?`.
    #
    # The *per-function* write has to be scoped the same way, and that is the
    # sharper half: this run verified A and says `verified`; the other run
    # verified the B now on disk and says `violated`. Writing ours last would
    # replace a live violation with a stale pass on a file that hashes fresh —
    # nothing would block. Here the fake CLI stands in for that concurrent hook,
    # rewriting the source and recording both its stamp and its verdict.
    src = tmp_path / "x.c"
    src.write_text("alpha\n")
    _seed_scanned(tmp_path)
    state_file = tmp_path / ".forseti" / "gate_state.json"
    beta_digest = hashlib.sha256(b"beta\n").hexdigest()

    monkeypatch.setattr(
        gate,
        "resolve_forseti_cmd",
        lambda: _echoing_forseti_cmd(
            tmp_path,
            verdict="verified",  # ...for content the other run has superseded
            during_verify=(
                f"open({str(src)!r}, 'w').write('beta\\n')\n"
                "import json, pathlib\n"
                f"_p = pathlib.Path({str(state_file)!r})\n"
                "_st = json.loads(_p.read_text())\n"
                f"_st['scanned']['x.c'] = {beta_digest!r}\n"
                "_st['units']['x.c::alpha'] = {'unit_id': 'x.c::alpha', "
                "'file': 'x.c', 'function': 'alpha', 'verdict': 'violated', "
                "'k': 1}\n"
                "_p.write_text(json.dumps(_st))"
            ),
        ),
    )
    verdicts = gate.verify_and_record(str(src), project_dir=str(tmp_path))

    assert [v.function for v in verdicts] == ["alpha"]  # no blocking `?` verdict
    after = gate.load_state(str(tmp_path))
    assert after["scanned"]["x.c"] == beta_digest  # the other run's stamp survives
    assert "x.c::?" not in after["units"]  # ...and no stranded, unclearable block
    # ...and the stale `verified` never displaced the live violation.
    assert after["units"]["x.c::alpha"]["verdict"] == "violated"
    assert gate.blocking_units(after)


def test_units_absent_payload_records_blocking_error(
    tmp_path: Path, monkeypatch
) -> None:
    # End to end: an exit-0 payload with no `units` key must make verify_and_record
    # persist a blocking `error` verdict, not silently pass the edited file.
    monkeypatch.setattr(
        gate,
        "resolve_forseti_cmd",
        lambda: _fake_forseti_cmd(tmp_path, stdout='{"source": "x.c"}'),
    )
    src = tmp_path / "x.c"
    src.write_text("int f(void) { return 0; }\n")
    verdicts = gate.verify_and_record(str(src), project_dir=str(tmp_path))
    assert len(verdicts) == 1
    assert verdicts[0].verdict == "error"
    state = gate.load_state(str(tmp_path))
    assert gate.blocking_units(state)  # non-empty → the Stop-gate blocks


def test_header_edit_short_circuits_to_clean_pass(tmp_path: Path, monkeypatch) -> None:
    # ESBMC can't parse a .h standalone, so a header is out of gate scope: enumerate
    # nothing (clean pass) WITHOUT shelling out — the fake CLI here would fail if
    # called, proving the .c allowlist short-circuits before the subprocess.
    monkeypatch.setattr(
        gate,
        "resolve_forseti_cmd",
        lambda: _fake_forseti_cmd(tmp_path, stderr="ERROR: must not run", exit_code=1),
    )
    hdr = tmp_path / "api.h"
    hdr.write_text("void foo(int *p);\n")
    assert gate.extract_function_defs(str(hdr), project_dir=str(tmp_path)) == []
    verdicts = gate.verify_and_record(str(hdr), project_dir=str(tmp_path))
    assert verdicts == []  # no units, no block, no error verdict
    state = gate.load_state(str(tmp_path))
    assert gate.blocking_units(state) == []


def test_enumeration_failure_records_blocking_error(
    tmp_path: Path, monkeypatch
) -> None:
    # If units can't be enumerated, verify_and_record must persist a blocking
    # `error` verdict — an edited-but-unparseable file cannot pass silently.
    monkeypatch.setattr(
        gate,
        "resolve_forseti_cmd",
        lambda: _fake_forseti_cmd(tmp_path, stderr="ERROR: boom", exit_code=1),
    )
    src = tmp_path / "x.c"
    src.write_text("int f(void) { return 0; }\n")
    verdicts = gate.verify_and_record(str(src), project_dir=str(tmp_path))
    assert len(verdicts) == 1
    assert verdicts[0].verdict == "error"
    state = gate.load_state(str(tmp_path))
    assert gate.blocking_units(state)  # non-empty → the Stop-gate blocks
    assert gate.needs_contract_units(state) == []


def test_enumeration_failure_does_not_stamp_scanned(
    tmp_path: Path, monkeypatch
) -> None:
    # Interaction between the out-of-band scan (#99) and list-units enumeration
    # (#131): a file whose units could not be enumerated must NOT be recorded in
    # `scanned`. Stamping it would let a later out-of-band scan dedup the edit as
    # already handled, so the blocking `error` would be the only thing standing
    # between an unparseable edit and a silent pass.
    monkeypatch.setattr(
        gate,
        "resolve_forseti_cmd",
        lambda: _fake_forseti_cmd(tmp_path, stderr="ERROR: boom", exit_code=1),
    )
    src = tmp_path / "x.c"
    src.write_text("int f(void) { return 0; }\n")
    _seed_scanned(tmp_path)
    verdicts = gate.verify_and_record(str(src), project_dir=str(tmp_path))
    assert verdicts[0].verdict == "error"
    state = gate.load_state(str(tmp_path))
    assert "x.c" not in state["scanned"]
    assert state["scanned"]["sentinel.c"] == "deadbeef"  # untouched, so the key exists
    assert gate.blocking_units(state)


def test_unreadable_file_does_not_stamp_scanned(tmp_path: Path, monkeypatch) -> None:
    # Same invariant on the sibling failure path: an unreadable file records a
    # blocking `error` and stays out of `scanned`. Enumeration must not even be
    # attempted, so the fake CLI is armed to fail if it is called.
    monkeypatch.setattr(
        gate,
        "resolve_forseti_cmd",
        lambda: _fake_forseti_cmd(tmp_path, stderr="ERROR: must not run", exit_code=1),
    )
    src = tmp_path / "gone.c"  # never created → read_bytes raises OSError
    _seed_scanned(tmp_path)
    verdicts = gate.verify_and_record(str(src), project_dir=str(tmp_path))
    assert len(verdicts) == 1
    assert verdicts[0].verdict == "error"
    state = gate.load_state(str(tmp_path))
    assert "gone.c" not in state["scanned"]
    assert state["scanned"]["sentinel.c"] == "deadbeef"  # untouched, so the key exists
    assert gate.blocking_units(state)


def test_pointer_unit_recorded_needs_contract(tmp_path: Path, monkeypatch) -> None:
    # A pointer-taking unit (from the CLI) is classified NEEDS_CONTRACT without
    # ever shelling out to esbmc verify.
    monkeypatch.setattr(
        gate,
        "resolve_forseti_cmd",
        lambda: _fake_forseti_cmd(tmp_path, stdout=_units_payload(("f", True))),
    )
    src = tmp_path / "buf.c"
    src.write_text("int f(int *p) { return *p; }\n")

    verdicts = gate.verify_and_record(str(src), project_dir=str(tmp_path))

    assert len(verdicts) == 1
    v = verdicts[0]
    assert v.verdict == gate.NEEDS_CONTRACT
    assert v.counterexample is None  # no ESBMC run → no counterexample
    assert v.argv is None  # never shelled out to esbmc verify
    assert not v.passed
    # persisted, and the Stop-gate would treat it as non-blocking
    state = gate.load_state(str(tmp_path))
    assert gate.blocking_units(state) == []
    assert len(gate.needs_contract_units(state)) == 1


# --- authoritative signature detection (needs esbmc) -----------------------


@pytest.mark.skipif(not _HAVE_ESBMC, reason="needs esbmc + forseti on PATH")
@pytest.mark.parametrize(
    "src, name, takes_pointer",
    [
        ("int f(int *p) { return *p; }", "f", True),  # pointer param
        ("int f(int x) { return x; }", "f", False),  # scalar param
        ("char *f(int x) { return 0; }", "f", False),  # pointer RETURN, scalar param
        ("void f(int p[10]) { }", "f", True),  # array param decays to pointer
        ("void f(void) { }", "f", False),  # no params
        ("unsigned f(const char *k, unsigned long n){return n;}", "f", True),
        ("void f(int (*cb)(void)) { }", "f", True),  # function-pointer param
        ("int neg(int x /* input */) { return -x; }", "neg", False),  # #130 comment
        ("int f(int *p /* ptr */) { return *p; }", "f", True),  # real ptr + comment
        ("int g(int a, /* a */ int b) { return a + b; }", "g", False),  # mid-list
        # --- brittleness class (#131): a regex fundamentally cannot do these ---
        ("typedef char* str_t;\nvoid f(str_t s) { (void)s; }", "f", True),  # typedef
        ("int f(\n  int *p\n) {\n  return *p;\n}", "f", True),  # multi-line signature
        ("long\nf(long n) { return n; }", "f", False),  # return type on its own line
        (
            "int f(x, p)\n  int x;\n  int *p;\n{\n  return x + *p;\n}",
            "f",
            True,
        ),  # K&R-style definition
    ],
)
def test_pointer_param_detection(
    tmp_path: Path, src: str, name: str, takes_pointer: bool
) -> None:
    source = tmp_path / "u.c"
    source.write_text(src + "\n")
    defs = gate.extract_function_defs(str(source), project_dir=str(tmp_path))
    assert len(defs) == 1
    assert defs[0].name == name
    assert defs[0].takes_pointer is takes_pointer


@pytest.mark.skipif(not _HAVE_ESBMC, reason="needs esbmc + forseti on PATH")
@pytest.mark.parametrize("depth, spelled", [(1, "../common.h"), (2, "../../common.h")])
def test_relative_include_resolves_through_the_snapshot(
    tmp_path: Path, depth: int, spelled: str
) -> None:
    # The fake CLIs never resolve an `#include`, so only the real frontend can
    # show the behaviour: a parent-relative include enumerates through the
    # snapshot exactly as it does in place. Mirroring siblings alone, this raised
    # `UnitsUnavailable` — a blocking `error` on a file that parses fine on disk.
    (tmp_path / "common.h").write_text("#define COMMON 1\n")
    src_dir = tmp_path.joinpath(*[f"d{i}" for i in range(depth)])
    src_dir.mkdir(parents=True)
    src = src_dir / "x.c"
    src.write_text(f'#include "{spelled}"\nint f(int x) {{ return x + COMMON; }}\n')

    in_place = gate.extract_function_defs(str(src), project_dir=str(tmp_path))
    snapshotted = gate.extract_function_defs(
        str(src), project_dir=str(tmp_path), content=src.read_bytes()
    )

    assert [d.name for d in in_place] == ["f"]
    assert [d.name for d in snapshotted] == ["f"]


@pytest.mark.skipif(not _HAVE_ESBMC, reason="needs esbmc + forseti on PATH")
def test_include_above_the_mirror_root_is_a_known_residual(
    tmp_path: Path, monkeypatch
) -> None:
    # Characterization, not an endorsement. Mirroring stops at the project dir,
    # so `#include "../common.h"` from a file *at* the root misses in the mirror
    # — and clang then falls through to the `-I` search with the spelled path,
    # which here resolves `sub/../common.h` to the project's own header instead
    # of the one above it. Different `VARIANT`, so the `#if` hides a function the
    # in-place parse enumerates: a wrong translation unit, not a block. Pinned so
    # a future fix flips this test rather than quietly widening the guarantee.
    # It is not a regression — the siblings-only staging did exactly this; what
    # the padded depth removes is the worse variant where the miss landed in
    # `/tmp` itself, where any user's `common.h` would have won.
    (tmp_path / "common.h").write_text("#define VARIANT 2\n")  # the in-place pick
    proj = tmp_path / "proj"
    (proj / "sub").mkdir(parents=True)
    (proj / "common.h").write_text("#define VARIANT 1\n")  # what the `-I` finds
    src = proj / "x.c"
    src.write_text(
        '#include "../common.h"\n'
        "int f(int x) { return x; }\n"
        "#if VARIANT == 2\n"
        "int only_above(int x) { return x; }\n"
        "#endif\n"
    )
    monkeypatch.setenv("FORSETI_BUILD_FLAGS", "-Isub")

    in_place = gate.extract_function_defs("x.c", project_dir=str(proj))
    snapshotted = gate.extract_function_defs(
        "x.c", project_dir=str(proj), content=src.read_bytes()
    )

    assert sorted(d.name for d in in_place) == ["f", "only_above"]
    assert sorted(d.name for d in snapshotted) == ["f"]  # the residual


def _symlinked_component_tree(proj: Path, pkg: Path) -> Path:
    """A source under a symlinked directory, with two candidate `selector.h`.

    ``proj/link -> pkg``; the source sits at ``link/src/x.c`` and climbs two
    levels. The kernel resolves `link` first, so in place it reaches the header
    beside `pkg`; the spelled chain would reach the one beside `proj`. The two
    select different `#if` branches, so the unit lists differ.
    """
    (pkg / "src").mkdir(parents=True)
    (pkg.parent / "selector.h").write_text("#define PICK_TARGET 1\n")
    (proj / "selector.h").write_text("#define PICK_SPELLED 1\n")
    (pkg / "common.h").write_text("#define ONE 1\n")
    (proj / "link").symlink_to(pkg)
    src = pkg / "src" / "x.c"
    src.write_text(
        '#include "../../selector.h"\n'
        '#include "../common.h"\n'
        "#ifdef PICK_TARGET\n"
        "int target_won(int x) { return x + ONE; }\n"
        "#endif\n"
        "#ifdef PICK_SPELLED\n"
        "int spelled_won(int x) { return x; }\n"
        "#endif\n"
    )
    return src


@pytest.mark.skipif(not _HAVE_ESBMC, reason="needs esbmc + forseti on PATH")
def test_symlinked_component_enumerates_like_the_in_place_parse(tmp_path: Path) -> None:
    # End to end against the real frontend: `..` past a symlinked component is
    # resolved by the kernel, so the in-place parse reads the header beside the
    # link's *target*. Mirroring the component as a real directory read the one
    # beside the link instead — a different translation unit, enumerated as
    # authoritative and then pruned and stamped against.
    proj = tmp_path / "proj"
    proj.mkdir()
    src = _symlinked_component_tree(proj, proj / "vendor" / "pkg")

    in_place = gate.extract_functions("link/src/x.c", project_dir=str(proj))
    snapshotted = [
        d.name
        for d in gate.extract_function_defs(
            "link/src/x.c", project_dir=str(proj), content=src.read_bytes()
        )
    ]

    assert in_place == ["target_won"]
    assert snapshotted == in_place


@pytest.mark.skipif(not _HAVE_ESBMC, reason="needs esbmc + forseti on PATH")
def test_symlink_out_of_the_project_blocks_rather_than_switching_headers(
    tmp_path: Path,
) -> None:
    # Characterization. When the link leaves the project the mirror can claim no
    # ancestry above its target — the same rule that stops the walk at the
    # project dir, and for the same reason (a `scandir` of `$HOME` on every
    # edit). So a climb past the target lands on empty padding and the parse
    # fails: a blocking `error`, which is the honest answer for a translation
    # unit the gate cannot reproduce. What it must never be again is the silent
    # one — enumerating `spelled_won`, a unit the in-place parse does not have.
    proj = tmp_path / "proj"
    proj.mkdir()
    src = _symlinked_component_tree(proj, tmp_path / "external" / "pkg")

    assert gate.extract_functions("link/src/x.c", project_dir=str(proj)) == [
        "target_won"
    ]
    with pytest.raises(gate.UnitsUnavailable):
        gate.extract_function_defs(
            "link/src/x.c", project_dir=str(proj), content=src.read_bytes()
        )


@pytest.mark.skipif(not _HAVE_ESBMC, reason="needs esbmc + forseti on PATH")
def test_function_like_macro_not_enumerated(tmp_path: Path) -> None:
    # A function-like macro is not a definition — the authoritative parse ignores
    # it, where the regex could false-match its `NAME(args)` shape.
    src = tmp_path / "m.c"
    src.write_text("#define SQ(a) ((a) * (a))\nint use(int x) { return SQ(x); }\n")
    names = [
        d.name for d in gate.extract_function_defs(str(src), project_dir=str(tmp_path))
    ]
    assert names == ["use"]


# --- blocking vs needs_contract classification -----------------------------


def _state(*verdicts: str) -> dict:
    units = {
        f"u{i}": {"unit_id": f"u{i}", "verdict": v} for i, v in enumerate(verdicts)
    }
    return {"units": units, "stop_attempts": 0}


def test_needs_contract_is_not_blocking() -> None:
    state = _state("verified", gate.NEEDS_CONTRACT, "violated", "unknown", "error")
    blocking = {u["verdict"] for u in gate.blocking_units(state)}
    needs = {u["verdict"] for u in gate.needs_contract_units(state)}
    assert blocking == {"violated", "unknown", "error"}  # neither verified nor needs
    assert needs == {gate.NEEDS_CONTRACT}


def test_verified_and_needs_only_does_not_block() -> None:
    state = _state("verified", gate.NEEDS_CONTRACT, gate.NEEDS_CONTRACT)
    assert gate.blocking_units(state) == []
    assert len(gate.needs_contract_units(state)) == 2


# --- verify_and_record: a mixed file, end to end ---------------------------


@pytest.mark.skipif(not _HAVE_ESBMC, reason="needs esbmc + forseti on PATH")
def test_mixed_file_scalar_gated_pointer_needs_contract(tmp_path: Path) -> None:
    # scalar my_abs is genuinely VIOLATED at INT64_MIN; the pointer unit is skipped.
    src = tmp_path / "mix.c"
    src.write_text(
        "#include <stdint.h>\n"
        "int64_t my_abs(int64_t x) { return (x < 0) ? -x : x; }\n"
        "int deref(int *p) { return *p; }\n"
    )
    verdicts = {
        v.function: v.verdict
        for v in gate.verify_and_record(str(src), project_dir=str(tmp_path))
    }
    assert verdicts["deref"] == gate.NEEDS_CONTRACT
    assert verdicts["my_abs"] == "violated"  # real scalar verdict still produced


# --- FORSETI_BUILD_FLAGS reaches both the enumeration and the verify (#131) ---


def _captured_verify_argv(monkeypatch, tmp_path: Path) -> list[str]:
    """Run `verify_function` against a stubbed subprocess and return its argv."""
    seen: list[str] = []

    class _Proc:
        stdout = '{"verdict": "verified"}'
        stderr = ""
        returncode = 0

    def _fake_run(argv, **kwargs):
        seen.extend(argv)
        return _Proc()

    monkeypatch.setattr(gate.subprocess, "run", _fake_run)
    monkeypatch.setattr(gate, "resolve_forseti_cmd", lambda: ["forseti"])
    gate.verify_function("x.c", "f", project_dir=str(tmp_path))
    return seen


def test_build_flags_reach_the_list_units_parse(tmp_path: Path, monkeypatch) -> None:
    # A TU that only parses with the project's `-I` must be enumerated with it:
    # otherwise esbmc exits nonzero and *every* edited file blocks with an
    # `error`, which is the gate refusing to work rather than gating.
    monkeypatch.setenv("FORSETI_BUILD_FLAGS", "-Iinclude -DNDEBUG")
    dest = tmp_path / "argv.json"
    monkeypatch.setattr(
        gate, "resolve_forseti_cmd", lambda: _argv_capturing_forseti_cmd(tmp_path, dest)
    )
    src = tmp_path / "x.c"
    src.write_text("int f(void) { return 0; }\n")
    assert gate.extract_function_defs(str(src), project_dir=str(tmp_path)) == []
    argv = json.loads(dest.read_text())["argv"]
    assert argv[argv.index("--") + 1 :] == ["-Iinclude", "-DNDEBUG"]


def test_build_flags_are_shell_split_not_split_on_spaces(
    tmp_path: Path, monkeypatch
) -> None:
    # A quoted include path with a space must survive as ONE argument; naive
    # `.split()` would hand esbmc `-I/opt/my` and a stray `sdk/include`, and the
    # parse would fail exactly where the knob was meant to make it succeed.
    monkeypatch.setenv("FORSETI_BUILD_FLAGS", "'-I/opt/my sdk/include' -DX=1")
    dest = tmp_path / "argv.json"
    monkeypatch.setattr(
        gate, "resolve_forseti_cmd", lambda: _argv_capturing_forseti_cmd(tmp_path, dest)
    )
    src = tmp_path / "x.c"
    src.write_text("int f(void) { return 0; }\n")
    gate.extract_function_defs(str(src), project_dir=str(tmp_path))
    argv = json.loads(dest.read_text())["argv"]
    assert argv[argv.index("--") + 1 :] == ["-I/opt/my sdk/include", "-DX=1"]


def test_no_build_flags_adds_no_separator(tmp_path: Path, monkeypatch) -> None:
    # The default path stays exactly as it was: no `--`, no empty passthrough.
    monkeypatch.delenv("FORSETI_BUILD_FLAGS", raising=False)
    dest = tmp_path / "argv.json"
    monkeypatch.setattr(
        gate, "resolve_forseti_cmd", lambda: _argv_capturing_forseti_cmd(tmp_path, dest)
    )
    src = tmp_path / "x.c"
    src.write_text("int f(void) { return 0; }\n")
    gate.extract_function_defs(str(src), project_dir=str(tmp_path))
    assert "--" not in json.loads(dest.read_text())["argv"]


def test_build_flags_reach_the_verify_too(tmp_path: Path, monkeypatch) -> None:
    # The verify must see the same translation unit the unit list came from —
    # enumerating with `-DFOO` and verifying without it would gate a different
    # set of functions than the ones that were found.
    monkeypatch.setenv("FORSETI_BUILD_FLAGS", "-Iinclude -DNDEBUG")
    argv = _captured_verify_argv(monkeypatch, tmp_path)
    assert argv[argv.index("--") + 1 :] == [
        *gate.SAFETY_FLAGS,
        "-Iinclude",
        "-DNDEBUG",
    ]


def test_safety_flags_do_not_leak_into_the_parse(tmp_path: Path, monkeypatch) -> None:
    # `--overflow-check` is a property-checking flag; a `--parse-tree-only` run
    # has no properties to check. Keeping the two sets apart is the point of the
    # split — the enumeration gets build flags only.
    monkeypatch.setenv("FORSETI_BUILD_FLAGS", "-Iinclude")
    dest = tmp_path / "argv.json"
    monkeypatch.setattr(
        gate, "resolve_forseti_cmd", lambda: _argv_capturing_forseti_cmd(tmp_path, dest)
    )
    src = tmp_path / "x.c"
    src.write_text("int f(void) { return 0; }\n")
    gate.extract_function_defs(str(src), project_dir=str(tmp_path))
    argv = json.loads(dest.read_text())["argv"]
    for flag in gate.SAFETY_FLAGS:
        assert flag not in argv


def test_build_flags_are_read_per_call_not_at_import(
    tmp_path: Path, monkeypatch
) -> None:
    # Read at call time, so a hook process that sets the variable after this
    # module is imported still gets its flags forwarded.
    dest = tmp_path / "argv.json"
    monkeypatch.setattr(
        gate, "resolve_forseti_cmd", lambda: _argv_capturing_forseti_cmd(tmp_path, dest)
    )
    src = tmp_path / "x.c"
    src.write_text("int f(void) { return 0; }\n")
    monkeypatch.setenv("FORSETI_BUILD_FLAGS", "-DLATE")
    gate.extract_function_defs(str(src), project_dir=str(tmp_path))
    assert "-DLATE" in json.loads(dest.read_text())["argv"]


def test_malformed_build_flags_block_instead_of_crashing(
    tmp_path: Path, monkeypatch
) -> None:
    # Quoting IS this knob's interface (that's why it is shlex-split), so an
    # unbalanced quote is the expected typo. It must land as the blocking `error`
    # verdict the gate is built around — a bare ValueError escaping here is an
    # unhandled traceback out of a PostToolUse hook, which is not a gate at all.
    monkeypatch.setenv("FORSETI_BUILD_FLAGS", "'-I/opt/my sdk")
    src = tmp_path / "x.c"
    src.write_text("int f(void) { return 0; }\n")
    _seed_scanned(tmp_path)

    verdicts = gate.verify_and_record(str(src), project_dir=str(tmp_path))

    assert [v.verdict for v in verdicts] == ["error"]
    assert "FORSETI_BUILD_FLAGS" in (verdicts[0].detail or "")
    # ...and it must not be mistaken for a handled file on a later scan.
    after = gate.load_state(str(tmp_path))
    assert "x.c" not in after["scanned"]
    assert after["scanned"]["sentinel.c"] == "deadbeef"


def test_malformed_build_flags_do_not_crash_a_direct_verify(
    tmp_path: Path, monkeypatch
) -> None:
    # `verify_function` is reachable without going through the enumeration, so it
    # needs its own conversion rather than relying on failing earlier.
    monkeypatch.setenv("FORSETI_BUILD_FLAGS", '"-Iunclosed')
    verdict = gate.verify_function("x.c", "f", project_dir=str(tmp_path))
    assert verdict.verdict == "error"
    assert "FORSETI_BUILD_FLAGS" in (verdict.detail or "")


def test_wellformed_build_flags_still_parse(tmp_path: Path, monkeypatch) -> None:
    # The guard must not swallow valid config: balanced quoting still splits.
    monkeypatch.setenv("FORSETI_BUILD_FLAGS", "'-I/opt/my sdk' -DX")
    assert gate._build_flags() == ("-I/opt/my sdk", "-DX")


@pytest.mark.skipif(not _HAVE_ESBMC, reason="needs esbmc + forseti on PATH")
def test_snapshot_enumeration_matches_the_in_place_parse(
    tmp_path: Path, monkeypatch
) -> None:
    # End to end against the real frontend, on the shape an include flag gets
    # wrong: `#include <config.h>` with a same-named header sitting beside the
    # source and the real one reached through FORSETI_BUILD_FLAGS. The two
    # `config.h` select different `#if` branches, so a snapshot that disturbed
    # the search order would enumerate a different unit list than the in-place
    # parse — and the gate would prune and stamp on the strength of it.
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "config.h").write_text("#define WIDGET 1\n")
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "config.h").write_text("#define WIDGET 0\n")  # the decoy sibling
    (src_dir / "helper.h").write_text("#define HELP 1\n")  # a genuine quoted sibling
    body = (
        '#include <config.h>\n#include "helper.h"\n'
        "int scal(int a) { return a + HELP; }\n"
        "#if WIDGET\n"
        "int only_with_widget(int x) { return x; }\n"
        "#endif\n"
    )
    src = src_dir / "x.c"
    src.write_text(body)
    monkeypatch.setenv("FORSETI_BUILD_FLAGS", f"-I{generated}")

    in_place = gate.extract_functions(str(src), project_dir=str(tmp_path))
    snapshot = [
        d.name
        for d in gate.extract_function_defs(
            str(src), project_dir=str(tmp_path), content=body.encode()
        )
    ]

    # The generated header wins in both, so the guarded unit is present in both.
    assert in_place == ["scal", "only_with_widget"]
    assert snapshot == in_place
