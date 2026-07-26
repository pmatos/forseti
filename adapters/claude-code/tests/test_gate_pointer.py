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

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
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
    """A fake CLI recording its argv and the source's *directory* to `dest`.

    `siblings` maps each entry beside the file it was handed to that entry's
    real path — how a test checks that the snapshot sits in the source's own
    real directory (`_enumerable_source`'s same-directory staging), so quoted
    `#include`s resolve exactly as they would in place. A directory that cannot
    be listed records ``None`` rather than aborting the capture.
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
        "sib = ents(os.path.dirname(src))\n"
        f"open({str(dest)!r}, 'w').write("
        "json.dumps({'argv': sys.argv[1:], 'siblings': sib}))\n"
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


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)


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
    # file it is reading, so the snapshot has to sit in that same directory — a
    # real sibling of the source, not a copy elsewhere. Crucially this needs NO
    # `-I` — see the next test for why a flag is not a substitute.
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
    # A distinct, recognisable name in the source's own real directory — never
    # the source's own path, and never occupying it.
    assert enumerated != src
    assert os.path.samefile(enumerated.parent, sub)
    assert enumerated.name.startswith(gate._ENUM_SNAPSHOT_PREFIX)
    # The sibling header, the subdirectory, and the source itself are all real —
    # nothing mirrored, because the snapshot sits right beside them.
    assert captured["siblings"]["helper.h"] == str((sub / "helper.h").resolve())
    assert captured["siblings"]["nested"] == str((sub / "nested").resolve())
    assert captured["siblings"]["x.c"] == str(src.resolve())
    assert "--" not in captured["argv"]  # no include flag invented
    assert not enumerated.exists()  # the snapshot is cleaned up


def test_snapshot_does_not_disturb_angle_include_or_iquote_precedence(
    tmp_path: Path, monkeypatch
) -> None:
    # Why the snapshot is staged in the source's own directory rather than named
    # with `-I`: `-I` also joins the *angle-bracket* search, and lands after any
    # `-iquote` from
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


def test_snapshot_stages_beside_the_lexical_directory_not_the_symlink_target(
    tmp_path: Path, monkeypatch
) -> None:
    # clang searches the directory of the path it was *given*, so for a symlinked
    # source that is the link's directory, not the target's. Staging beside the
    # resolved target instead would drop a header beside the link and silently
    # prefer a same-named header beside the target — enumerating units the
    # in-place parse never sees.
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
    (real / "config.h").write_text("#define WHICH 2\n")  # must NOT be picked
    link_dir = tmp_path / "linked"
    link_dir.mkdir()
    (link_dir / "config.h").write_text("#define WHICH 1\n")  # this one must be
    (link_dir / "x.c").symlink_to(real / "x.c")

    gate.extract_function_defs(
        str(link_dir / "x.c"), project_dir=str(tmp_path), content=b"f\n"
    )

    siblings = json.loads(dest.read_text())["siblings"]
    assert siblings["config.h"] == str((link_dir / "config.h").resolve())


def test_snapshot_stages_beside_the_dir_resolved_against_project_dir(
    tmp_path: Path, monkeypatch
) -> None:
    # The CLI subprocess runs with cwd=project_dir, so a relative `file_path` is
    # relative to *that*, not to the hook process's cwd. A bare `os.path.abspath`
    # would stage beside whatever directory the hook happened to start in.
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
    """Make staging the snapshot fail with a bare `OSError`: ENOSPC, an unwritable
    directory, ...
    """

    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError(message)

    monkeypatch.setattr(gate.tempfile, "mkstemp", _boom)


def test_unstageable_snapshot_raises_units_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    # An unwritable source directory, ENOSPC or EDQUOT must surface as the one
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


def test_snapshot_is_excluded_from_the_git_index_before_it_exists(
    tmp_path: Path, monkeypatch
) -> None:
    # A concurrent `git add -A` — an IDE, another automation job, a user's own
    # habit — can land at any point while the snapshot sits on disk for the
    # whole enumeration. Running `git add -A` from *inside* the fake CLI is the
    # moment the snapshot is guaranteed to exist, so it is the sharpest test of
    # whether `_index_ignore_snapshot`'s registration (which has to happen
    # before `mkstemp`, not after) actually keeps git from seeing it.
    _git_init(tmp_path)
    src = tmp_path / "x.c"
    src.write_text("int f(void) { return 0; }\n")
    monkeypatch.setattr(
        gate,
        "resolve_forseti_cmd",
        lambda: _echoing_forseti_cmd(
            tmp_path,
            before_read=(
                "import subprocess as sp; sp.run(['git', 'add', '-A'], check=True)"
            ),
        ),
    )

    gate.extract_function_defs(str(src), project_dir=str(tmp_path), content=b"f\n")

    staged = subprocess.run(
        ["git", "-C", str(tmp_path), "diff", "--cached", "--name-only"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert gate._ENUM_SNAPSHOT_PREFIX not in staged
    assert "x.c" in staged  # the real edit is still staged normally


def test_snapshot_exclusion_is_a_noop_outside_a_git_work_tree(
    tmp_path: Path, monkeypatch
) -> None:
    # No git index exists outside a work tree, so registering the exclude
    # pattern must not become a hard dependency of enumeration succeeding —
    # `tmp_path` here is deliberately never `git init`ed.
    src = tmp_path / "x.c"
    src.write_text("int f(void) { return 0; }\n")
    monkeypatch.setattr(
        gate, "resolve_forseti_cmd", lambda: _echoing_forseti_cmd(tmp_path)
    )

    defs = gate.extract_function_defs(
        str(src), project_dir=str(tmp_path), content=b"f\n"
    )

    assert [d.name for d in defs] == ["f"]


def test_unregisterable_git_exclude_blocks_with_units_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    # Every OSError this staging path can raise has to land as UnitsUnavailable
    # (fail closed) — including one from registering the exclude pattern
    # itself, or the snapshot would end up staged unprotected rather than not
    # staged at all. A regular file standing where a directory component of
    # the resolved exclude path needs to be makes `os.makedirs` fail with an
    # OSError it cannot route around (unlike a merely-missing directory, which
    # `os.makedirs` now creates — see
    # `test_missing_git_info_directory_is_created_before_writing_exclude`).
    (tmp_path / "blocker").write_text("not a directory\n")
    monkeypatch.setattr(gate, "_git", lambda start_dir, *args: "blocker/exclude")
    src = tmp_path / "x.c"
    src.write_text("int f(void) { return 0; }\n")
    with pytest.raises(gate.UnitsUnavailable, match="exclude"):
        gate.extract_function_defs(str(src), project_dir=str(tmp_path), content=b"f\n")


def test_snapshot_cleanup_failure_after_success_blocks(
    tmp_path: Path, monkeypatch
) -> None:
    # The happy path: enumeration itself succeeds, but the final `os.unlink`
    # fails (a lost-write-permission race, an NFS hiccup). That must not be
    # suppressed into a silent success — it would leave a complete source
    # snapshot in the worktree indefinitely while reporting the enumeration as
    # fine.
    real_unlink = os.unlink

    def _boom(path: str, *, dir_fd: int | None = None) -> None:
        if os.path.basename(path).startswith(gate._ENUM_SNAPSHOT_PREFIX):
            raise OSError("permission denied")
        real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(gate.os, "unlink", _boom)
    monkeypatch.setattr(
        gate, "resolve_forseti_cmd", lambda: _echoing_forseti_cmd(tmp_path)
    )
    src = tmp_path / "x.c"
    src.write_text("int f(void) { return 0; }\n")

    with pytest.raises(gate.UnitsUnavailable, match="remove"):
        gate.extract_function_defs(str(src), project_dir=str(tmp_path), content=b"f\n")


def test_snapshot_cleanup_failure_does_not_mask_a_pending_enumeration_failure(
    tmp_path: Path, monkeypatch
) -> None:
    # When the CLI call itself already failed, a *secondary* cleanup failure
    # must not replace that primary, more actionable exception — cleanup is
    # still attempted (best-effort) but its error is suppressed here, mirroring
    # the write-failure path just above.
    real_unlink = os.unlink

    def _boom(path: str, *, dir_fd: int | None = None) -> None:
        if os.path.basename(path).startswith(gate._ENUM_SNAPSHOT_PREFIX):
            raise OSError("permission denied")
        real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(gate.os, "unlink", _boom)
    monkeypatch.setattr(
        gate,
        "resolve_forseti_cmd",
        lambda: _fake_forseti_cmd(
            tmp_path, stderr="boom-original-failure", exit_code=1
        ),
    )
    src = tmp_path / "x.c"
    src.write_text("int f(void) { return 0; }\n")

    with pytest.raises(gate.UnitsUnavailable, match="boom-original-failure"):
        gate.extract_function_defs(str(src), project_dir=str(tmp_path), content=b"f\n")


def test_exclude_is_registered_against_the_source_repo_not_the_project_root(
    tmp_path: Path, monkeypatch
) -> None:
    # A source living in a *different* repository than `project_dir` (a
    # submodule, a sibling checkout reached via a symlink) must have the
    # exclude pattern registered in *its own* `.git/info/exclude` — the one a
    # `git add -A` run from inside that repo actually consults — not the
    # project root's, which `mkstemp` never stages anything into (issue #151
    # follow-up).
    _git_init(tmp_path)
    nested = tmp_path / "nested"
    nested.mkdir()
    _git_init(nested)
    src = nested / "x.c"
    src.write_text("int f(void) { return 0; }\n")
    monkeypatch.setattr(
        gate, "resolve_forseti_cmd", lambda: _echoing_forseti_cmd(tmp_path)
    )

    gate.extract_function_defs(str(src), project_dir=str(tmp_path), content=b"f\n")

    pattern = f"{gate._ENUM_SNAPSHOT_PREFIX}*"
    nested_exclude = (nested / ".git" / "info" / "exclude").read_text()
    assert pattern in nested_exclude.splitlines()
    # `git init` always seeds a commented-out `.git/info/exclude` (confirmed
    # empirically), so this is a real assertion, not a vacuous one.
    root_exclude = (tmp_path / ".git" / "info" / "exclude").read_text()
    assert pattern not in root_exclude.splitlines()


def test_exclude_is_registered_in_the_repo_root_for_a_nested_source(
    tmp_path: Path, monkeypatch
) -> None:
    # The common shape: one repo at `project_dir`, source one level down (e.g.
    # `src/x.c`). `git rev-parse --git-path info/exclude` run from a
    # subdirectory returns a path *relative to that subdirectory*
    # (``../.git/info/exclude``, confirmed empirically) rather than an
    # absolute one — this pins that `os.path.join`/`os.makedirs` resolve it to
    # the same repo root's exclude file the project-root case already covers,
    # not a nonexistent `src/../.git/info/exclude` tree of its own.
    _git_init(tmp_path)
    sub = tmp_path / "src"
    sub.mkdir()
    src = sub / "x.c"
    src.write_text("int f(void) { return 0; }\n")
    monkeypatch.setattr(
        gate, "resolve_forseti_cmd", lambda: _echoing_forseti_cmd(tmp_path)
    )

    gate.extract_function_defs(str(src), project_dir=str(tmp_path), content=b"f\n")

    pattern = f"{gate._ENUM_SNAPSHOT_PREFIX}*"
    root_exclude = (tmp_path / ".git" / "info" / "exclude").read_text()
    assert pattern in root_exclude.splitlines()
    assert not (sub / ".git").exists()


def test_preexisting_non_utf8_exclude_content_does_not_crash(
    tmp_path: Path, monkeypatch
) -> None:
    # `.git/info/exclude` is git's own file, read as raw bytes by git itself —
    # a non-UTF-8 byte sequence already in it (e.g. a pattern written under a
    # non-UTF-8 locale) is legal. Decoding it as text would raise
    # `UnicodeDecodeError`, a `ValueError` that escapes the surrounding
    # `except OSError` uncaught and crashes the hook process instead of
    # failing closed.
    _git_init(tmp_path)
    exclude_path = tmp_path / ".git" / "info" / "exclude"
    exclude_path.write_bytes(b"\xff\xfe not valid utf-8\n")
    monkeypatch.setattr(
        gate, "resolve_forseti_cmd", lambda: _echoing_forseti_cmd(tmp_path)
    )
    src = tmp_path / "x.c"
    src.write_text("int f(void) { return 0; }\n")

    defs = gate.extract_function_defs(
        str(src), project_dir=str(tmp_path), content=b"f\n"
    )

    assert [d.name for d in defs] == ["f"]
    updated = exclude_path.read_bytes()
    assert b"\xff\xfe not valid utf-8\n" in updated
    assert f"{gate._ENUM_SNAPSHOT_PREFIX}*".encode() in updated


def test_missing_git_info_directory_is_created_before_writing_exclude(
    tmp_path: Path, monkeypatch
) -> None:
    # `git rev-parse --git-path info/exclude` only computes a path — it does
    # not create `.git/info/`, which is legitimately absent for a repo made
    # with an empty template or by non-git tooling. That must not turn into a
    # hard, total gate outage on ordinary (if uncommon) repo state.
    _git_init(tmp_path)
    info_dir = tmp_path / ".git" / "info"
    shutil.rmtree(info_dir, ignore_errors=True)
    assert not info_dir.exists()
    monkeypatch.setattr(
        gate, "resolve_forseti_cmd", lambda: _echoing_forseti_cmd(tmp_path)
    )
    src = tmp_path / "x.c"
    src.write_text("int f(void) { return 0; }\n")

    defs = gate.extract_function_defs(
        str(src), project_dir=str(tmp_path), content=b"f\n"
    )

    assert [d.name for d in defs] == ["f"]
    exclude = (info_dir / "exclude").read_text()
    assert f"{gate._ENUM_SNAPSHOT_PREFIX}*" in exclude.splitlines()


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


def test_stamp_is_not_reclaimed_from_a_run_that_already_finished(
    tmp_path: Path, monkeypatch
) -> None:
    # Taking the stamp is a claim to be authoritative, so the content check has to
    # happen under the same lock that writes it. Checked before the lock, this run
    # (version A) could pass, pause, and resume after a concurrent run (version B)
    # had verified and stamped B — then reclaim the entry with A's digest, prune
    # and overwrite B's verdicts against content nobody has on disk, and finally
    # withdraw the stamp on the post-verify re-hash, leaving a blocking `x.c::?`
    # despite B having succeeded. Here the fake CLI stands in for that concurrent
    # run, landing entirely within this run's `list-units`.
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
            after_read=(
                f"open({str(src)!r}, 'w').write('beta\\n')\n"
                "import json, pathlib\n"
                f"_p = pathlib.Path({str(state_file)!r})\n"
                "_st = json.loads(_p.read_text())\n"
                f"_st['scanned']['x.c'] = {beta_digest!r}\n"
                "_st['units']['x.c::beta'] = {'unit_id': 'x.c::beta', "
                "'file': 'x.c', 'function': 'beta', 'verdict': 'violated', "
                "'k': 1}\n"
                "_p.write_text(json.dumps(_st))"
            ),
        ),
    )
    verdicts = gate.verify_and_record(str(src), project_dir=str(tmp_path))

    assert verdicts == []  # deferred to the run that owns the file
    after = gate.load_state(str(tmp_path))
    assert after["scanned"]["x.c"] == beta_digest  # never reclaimed for A
    assert after["units"]["x.c::beta"]["verdict"] == "violated"  # B's verdict stands
    assert "x.c::alpha" not in after["units"]  # ...and A's units were not recorded
    # No stranded, unclearable block: the file hashes equal to the surviving
    # stamp, so nothing would ever re-run the reconcile that prunes a `?`.
    assert "x.c::?" not in after["units"]
    assert gate.blocking_units(after)  # B's violation still gates the Stop hook


def test_stamp_is_not_reclaimed_after_losing_the_lock_race(
    tmp_path: Path, monkeypatch
) -> None:
    # The sharper half of the same race: the window that matters is between this
    # run's content check and its stamp, and waiting for the gate lock is what
    # opens it. Patching the lock so another run's work is already done when this
    # one gets in models exactly that — B rewrote the source, stamped its digest
    # and recorded its verdict while A queued. Checked before the lock, A would
    # then reclaim the entry with a digest matching nothing on disk, prune B's
    # units, and end by withdrawing the stamp and blocking on a file B had
    # legitimately verified.
    src = tmp_path / "x.c"
    src.write_text("alpha\n")
    _seed_scanned(tmp_path)
    beta_digest = hashlib.sha256(b"beta\n").hexdigest()
    monkeypatch.setattr(
        gate, "resolve_forseti_cmd", lambda: _echoing_forseti_cmd(tmp_path)
    )
    real_lock, landed = gate.gate_lock, []

    @contextlib.contextmanager
    def lock_a_concurrent_run_got_first(project_dir: str):
        with real_lock(project_dir):
            if not landed:
                landed.append(True)
                src.write_text("beta\n")
                state = gate.load_state(project_dir)
                state["scanned"]["x.c"] = beta_digest
                gate.record(
                    state,
                    gate.UnitVerdict("x.c::beta", "x.c", "beta", "violated", 1),
                )
                gate.save_state(project_dir, state)
            yield

    monkeypatch.setattr(gate, "gate_lock", lock_a_concurrent_run_got_first)
    verdicts = gate.verify_and_record(str(src), project_dir=str(tmp_path))

    assert verdicts == []
    after = gate.load_state(str(tmp_path))
    assert after["scanned"]["x.c"] == beta_digest  # B's stamp, not reclaimed
    assert after["units"]["x.c::beta"]["verdict"] == "violated"  # nor B's units
    assert "x.c::alpha" not in after["units"]  # A's stale reconcile never ran
    assert "x.c::?" not in after["units"]  # and B was not blocked on afterwards


def test_enumerate_drift_block_defers_to_a_stamp_taken_after_the_check(
    tmp_path: Path, monkeypatch
) -> None:
    # The enumerate-side drift check reads `scanned` under the stamp lock and then
    # releases it, so a concurrent run can stamp what is on disk before the block
    # is published — and that `x.c::?` is then unclearable for the usual reason.
    # Deciding the deferral where the check ran cannot close it; it has to be
    # re-decided under the lock that records the verdict. The rewrite lands during
    # enumeration (nothing vouches for it yet), the other run's stamp on the lock
    # acquisition the block itself takes.
    src = tmp_path / "x.c"
    src.write_text("alpha\n")
    _seed_scanned(tmp_path)
    beta_digest = hashlib.sha256(b"beta\n").hexdigest()

    monkeypatch.setattr(
        gate,
        "resolve_forseti_cmd",
        lambda: _echoing_forseti_cmd(
            tmp_path, after_read=f"open({str(src)!r}, 'w').write('beta\\n')"
        ),
    )
    real_lock, locks = gate.gate_lock, []

    @contextlib.contextmanager
    def lock_a_concurrent_run_stamped_in(project_dir: str):
        with real_lock(project_dir):
            locks.append(1)
            if len(locks) == 2:  # the one `_blocking_error` takes
                state = gate.load_state(project_dir)
                state["scanned"]["x.c"] = beta_digest
                gate.record(
                    state,
                    gate.UnitVerdict("x.c::beta", "x.c", "beta", "violated", 1),
                )
                gate.save_state(project_dir, state)
            yield

    monkeypatch.setattr(gate, "gate_lock", lock_a_concurrent_run_stamped_in)
    verdicts = gate.verify_and_record(str(src), project_dir=str(tmp_path))

    assert len(locks) == 2  # the stamp check, then the block — the window between
    assert verdicts == []  # deferred to the run that owns the file
    after = gate.load_state(str(tmp_path))
    assert "x.c::?" not in after["units"]  # no stranded, unclearable block
    assert after["scanned"]["x.c"] == beta_digest
    assert gate.blocking_units(after)  # the other run's violation still gates
    assert gate.stale_sources(str(tmp_path), after, [str(src)]) == []


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
    # verifying immutable content, which changes *what* is verified — a snapshot
    # kept alive for the whole verify call, not the brief enumeration parse, and
    # every counterexample and the trace's `argv` would need rewriting from a
    # temp path back to the real file so a since-deleted path never reaches
    # Claude (issue #150). Pinned here so the limit is explicit in the suite, and
    # so a future fix flips this test rather than quietly widening the guarantee.
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

    # No blocking `?` verdict — and no stale `verified` either. The returned list
    # is what the PostToolUse hook reports and acts on, so handing back a verdict
    # for content the other run has replaced would report a pass for bytes this
    # run never saw (or, the other way round, feed Claude a counterexample for
    # code no longer on disk).
    assert verdicts == []
    after = gate.load_state(str(tmp_path))
    assert after["scanned"]["x.c"] == beta_digest  # the other run's stamp survives
    assert "x.c::?" not in after["units"]  # ...and no stranded, unclearable block
    # ...and the stale `verified` never displaced the live violation.
    assert after["units"]["x.c::alpha"]["verdict"] == "violated"
    assert gate.blocking_units(after)


def test_drift_block_defers_to_a_stamp_taken_after_the_withdrawal(
    tmp_path: Path, monkeypatch
) -> None:
    # The sibling above catches the concurrent run *before* the withdrawal; this
    # one catches it after. Withdrawing releases the lock, so a hook for the newer
    # content can stamp it and verify it through before the block is published.
    # Published unconditionally, that `x.c::?` can never be cleared: the file
    # hashes equal to the surviving stamp and no claim is left pending, so
    # `stale_sources` never re-offers the file, the reconcile that prunes `?`
    # never runs, and the Stop-gate blocks its way to a residual on a file the
    # other run legitimately verified. The lock is the seam — the concurrent run's
    # work lands on the first acquisition taken after this run gave up its stamp.
    src = tmp_path / "x.c"
    src.write_text("alpha\n")
    _seed_scanned(tmp_path)
    beta_digest = hashlib.sha256(b"beta\n").hexdigest()

    monkeypatch.setattr(
        gate,
        "resolve_forseti_cmd",
        lambda: _echoing_forseti_cmd(
            tmp_path,
            verdict="verified",  # a clean verdict — for content no longer on disk
            during_verify=f"open({str(src)!r}, 'w').write('beta\\n')",
        ),
    )
    real_lock, landed = gate.gate_lock, []

    @contextlib.contextmanager
    def lock_a_concurrent_run_finished_in(project_dir: str):
        with real_lock(project_dir):
            state = gate.load_state(project_dir)
            gave_up_the_stamp = "x.c" not in state["scanned"]
            if not landed and gave_up_the_stamp and "x.c::alpha" in state["units"]:
                landed.append(True)
                state["scanned"]["x.c"] = beta_digest
                gate.record(
                    state,
                    gate.UnitVerdict("x.c::beta", "x.c", "beta", "violated", 1),
                )
                gate.save_state(project_dir, state)
            yield

    monkeypatch.setattr(gate, "gate_lock", lock_a_concurrent_run_finished_in)
    verdicts = gate.verify_and_record(str(src), project_dir=str(tmp_path))

    assert landed  # the interleaving under test really happened
    assert verdicts == []  # deferred to the run that owns the file
    after = gate.load_state(str(tmp_path))
    assert "x.c::?" not in after["units"]  # no stranded, unclearable block
    assert after["scanned"]["x.c"] == beta_digest  # the other run's stamp survives
    assert after["units"]["x.c::beta"]["verdict"] == "violated"  # ...and its verdict
    assert gate.blocking_units(after)  # which is what gates the Stop hook now
    # Why an unconditional block would have stuck: nothing marks the file for a
    # re-scan, so no later run would reach the reconcile that prunes the `?`.
    assert gate.stale_sources(str(tmp_path), after, [str(src)]) == []


def test_drift_block_still_lands_when_no_stamp_vouches_for_the_file(
    tmp_path: Path, monkeypatch
) -> None:
    # The other half of that test: "a stamp exists" is not the condition — "a stamp
    # equal to what is on disk" is. Here a concurrent run stamps a third content
    # that never reaches disk, so nothing vouches for the bytes sitting there. The
    # block must land, and it is clearable precisely because the file reads stale.
    src = tmp_path / "x.c"
    src.write_text("alpha\n")
    _seed_scanned(tmp_path)
    gamma_digest = hashlib.sha256(b"gamma\n").hexdigest()

    monkeypatch.setattr(
        gate,
        "resolve_forseti_cmd",
        lambda: _echoing_forseti_cmd(
            tmp_path,
            verdict="verified",
            during_verify=f"open({str(src)!r}, 'w').write('beta\\n')",
        ),
    )
    real_lock, landed = gate.gate_lock, []

    @contextlib.contextmanager
    def lock_a_concurrent_run_stamped_other_content_in(project_dir: str):
        with real_lock(project_dir):
            state = gate.load_state(project_dir)
            gave_up_the_stamp = "x.c" not in state["scanned"]
            if not landed and gave_up_the_stamp and "x.c::alpha" in state["units"]:
                landed.append(True)
                state["scanned"]["x.c"] = gamma_digest
                gate.save_state(project_dir, state)
            yield

    monkeypatch.setattr(
        gate, "gate_lock", lock_a_concurrent_run_stamped_other_content_in
    )
    verdicts = gate.verify_and_record(str(src), project_dir=str(tmp_path))

    assert landed
    assert [v.verdict for v in verdicts] == ["error"]  # not suppressed
    after = gate.load_state(str(tmp_path))
    assert after["units"]["x.c::?"]["verdict"] == "error"
    assert gate.blocking_units(after)
    # ...and it can be cleared: the file hashes to neither stamp, so the next scan
    # re-offers it and the reconcile drops the `?`.
    assert gate.stale_sources(str(tmp_path), after, [str(src)]) == [str(src)]


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
def test_include_above_the_project_root_is_no_longer_a_residual(
    tmp_path: Path, monkeypatch
) -> None:
    # This used to be a characterization test, not an endorsement: the mirrored
    # design that preceded `_enumerable_source`'s same-directory staging (issue
    # #151) stopped at the project dir, so `#include "../common.h"` from a file
    # *at* the root missed the mirror and fell through to the `-I` search with
    # the spelled path — landing on the project's own header instead of the one
    # above it, silently enumerating the wrong translation unit. A
    # same-directory snapshot has no mirror root to fall off: `../common.h`
    # resolves relative to its own real directory, exactly like the in-place
    # file, because it *is* in that directory.
    (tmp_path / "common.h").write_text("#define VARIANT 2\n")  # the in-place pick
    proj = tmp_path / "proj"
    (proj / "sub").mkdir(parents=True)
    (proj / "common.h").write_text("#define VARIANT 1\n")  # the old `-I` miss
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
    assert sorted(d.name for d in snapshotted) == ["f", "only_above"]


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
def test_symlink_out_of_the_project_no_longer_blocks(tmp_path: Path) -> None:
    # The mirrored design this replaced (issue #151) could claim no ancestry
    # above a link's target once it left the project — the same rule that
    # stopped the walk at the project dir, and for the same reason (a `scandir`
    # of `$HOME` on every edit). A climb past the target landed on empty padding
    # and the parse failed with a blocking `error`. Same-directory staging has no
    # project boundary to enforce in the first place: the snapshot sits beside
    # the real `x.c`, wherever that is, so the same translation unit enumerates
    # whether the source lives inside the project or, as here, entirely outside
    # it through a symlink.
    proj = tmp_path / "proj"
    proj.mkdir()
    src = _symlinked_component_tree(proj, tmp_path / "external" / "pkg")

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
@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
def test_a_source_alias_leads_back_into_the_live_file_is_a_known_residual(
    tmp_path: Path, alias: str
) -> None:
    # Characterization, not an endorsement — widened by the same-directory
    # staging issue #151 introduced. The old mirrored design redirected any
    # sibling sharing the source's inode to the snapshot, so a symlink or
    # hard-link alias beside the source still led back to the immutable copy.
    # A same-directory snapshot has a random name (`tempfile.mkstemp`, so a
    # concurrent enumeration of the same file cannot collide with it), so it
    # cannot occupy any of the source's other names either — every real
    # sibling, alias included, still resolves to the real, live file. Pinned
    # alongside `test_self_include_by_own_name_is_a_known_residual` (the
    # narrowest case, needing no alias at all) and
    # `test_a_nested_source_alias_is_a_known_residual` (one directory deeper),
    # which this change does not affect either way.
    proj = tmp_path / "proj"
    proj.mkdir()
    src = proj / "x.c"
    src.write_text(
        "#ifdef SELF\n"
        "#define PICK 1\n"
        "#else\n"
        "#define SELF\n"
        '#include "alias.c"\n'
        "#if PICK\n"
        "int from_the_snapshot(int x) { return x; }\n"
        "#else\n"
        "int from_the_live_file(int x) { return x; }\n"
        "#endif\n"
        "#endif\n"
    )
    snapshotted = src.read_bytes()
    if alias == "symlink":
        (proj / "alias.c").symlink_to("x.c")
    else:
        os.link(src, proj / "alias.c")
    src.write_bytes(snapshotted.replace(b"#define PICK 1", b"#define PICK 0"))

    names = [
        d.name
        for d in gate.extract_function_defs(
            "x.c", project_dir=str(proj), content=snapshotted
        )
    ]

    assert names == ["from_the_live_file"]  # the residual: not the snapshot's branch


@pytest.mark.skipif(not _HAVE_ESBMC, reason="needs esbmc + forseti on PATH")
def test_self_include_by_own_name_is_a_known_residual(tmp_path: Path) -> None:
    # The narrowest case of the alias residual above — no alias or symlink
    # needed, since the source is already a sibling of its own literal name and
    # the snapshot (a random `tempfile.mkstemp` name) can never occupy it.
    src = tmp_path / "x.c"
    src.write_text(
        "#ifdef SELF\n"
        "#define PICK 1\n"
        "#else\n"
        "#define SELF\n"
        '#include "x.c"\n'
        "#if PICK\n"
        "int from_the_snapshot(int x) { return x; }\n"
        "#else\n"
        "int from_the_live_file(int x) { return x; }\n"
        "#endif\n"
        "#endif\n"
    )
    snapshotted = src.read_bytes()
    src.write_bytes(snapshotted.replace(b"#define PICK 1", b"#define PICK 0"))

    names = [
        d.name
        for d in gate.extract_function_defs(
            "x.c", project_dir=str(tmp_path), content=snapshotted
        )
    ]

    assert names == ["from_the_live_file"]  # the residual: not the snapshot's branch


@pytest.mark.skipif(not _HAVE_ESBMC, reason="needs esbmc + forseti on PATH")
def test_a_resolved_path_under_a_symlinked_project_still_matches_in_place(
    tmp_path: Path,
) -> None:
    # The project dir and the source path need not be spelled the same way: with
    # `link -> real`, a hook may be handed `<link>/sub/x.c` while out-of-band
    # discovery builds its paths on the git root, which `git rev-parse` reports
    # *resolved*. Same-directory staging has no project-dir containment test to
    # get wrong here — the snapshot goes beside whatever real directory `x.c`'s
    # own (possibly-resolved) path names, regardless of how `project_dir` itself
    # was spelled.
    real = tmp_path / "real"
    (real / "sub").mkdir(parents=True)
    (real / "common.h").write_text("#define COMMON 1\n")
    src = real / "sub" / "x.c"
    src.write_text('#include "../common.h"\nint f(int x) { return x + COMMON; }\n')
    (tmp_path / "link").symlink_to(real)
    spelled_project = str(tmp_path / "link")  # ...but the source path is resolved

    in_place = gate.extract_functions(str(src), project_dir=spelled_project)
    snapshotted = [
        d.name
        for d in gate.extract_function_defs(
            str(src), project_dir=spelled_project, content=src.read_bytes()
        )
    ]

    assert in_place == ["f"]
    assert snapshotted == in_place


@pytest.mark.skipif(not _HAVE_ESBMC, reason="needs esbmc + forseti on PATH")
def test_a_component_walked_twice_is_not_a_loop(tmp_path: Path) -> None:
    # `self -> .` is a real idiom, and `self/self/x.c` is a perfectly ordinary path
    # the kernel resolves in two hops — no `..` involved, so `_kernel_dir` leaves
    # it spelled and `tempfile.mkstemp` stages it via a real filesystem write,
    # which the kernel resolves the same way it resolves the source's own path.
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "self").symlink_to(".")
    (proj / "common.h").write_text("#define COMMON 1\n")
    (proj / "x.c").write_text(
        '#include "common.h"\nint f(int x) { return x + COMMON; }\n'
    )
    given = "self/self/x.c"

    in_place = gate.extract_functions(given, project_dir=str(proj))
    snapshotted = [
        d.name
        for d in gate.extract_function_defs(
            given, project_dir=str(proj), content=(proj / "x.c").read_bytes()
        )
    ]

    assert in_place == ["f"]
    assert snapshotted == in_place


def test_an_expanding_symlink_cycle_blocks_instead_of_spinning(tmp_path: Path) -> None:
    # There is no custom cycle guard left to test directly — same-directory
    # staging has no chain to walk, so nothing in `_enumerable_source` can spin.
    # What still has to hold is that a `..` in the *given* path through a cyclic
    # symlink component (`a -> a/x`, one component LONGER every hop, so no
    # repeat-detector would ever recur) fails closed rather than hanging: `_kernel_dir`
    # calls `os.path.realpath`, which never reports ELOOP itself and instead gives
    # up and returns something unresolved, but `tempfile.mkstemp`'s own `open`
    # against that path *does* hit the kernel's `MAXSYMLINKS` bound (measured) —
    # surfacing as the one `OSError` this function already converts to a blocking
    # `UnitsUnavailable`, never a hang.
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a").symlink_to(proj / "a" / "x")

    with (
        pytest.raises(gate.UnitsUnavailable),
        gate._enumerable_source(
            "a/../x.c", b"int f(void) { return 0; }\n", project_dir=str(proj)
        ),
    ):
        pass


@pytest.mark.skipif(not _HAVE_ESBMC, reason="needs esbmc + forseti on PATH")
def test_a_nested_source_alias_is_a_known_residual(tmp_path: Path) -> None:
    # Characterization, not an endorsement, and the boundary of the guarantee the
    # tests above establish. Same-directory staging only touches the source's own
    # directory — it neither creates nor redirects anything a level down, so an
    # alias below a subdirectory (`sub/alias.c -> ../x.c`) still resolves through
    # the real filesystem straight back to the live file, as does an absolute
    # include of the source (clang opens the spelled path, which no staging can
    # intercept). Immutability is the top-level file's; a self-include reaching
    # around it lands back in the A -> B -> A residual the verify loop already
    # carries.
    #
    # Closing it would mean rewriting the source's whole subtree to redirect any
    # alias back to the snapshot — `scandir` and `stat` on every edit,
    # disproportionate to a translation unit that includes itself through a
    # nested alias, and no cleaner in Core: ESBMC 8.3.0 has no stdin convention
    # (measured — `esbmc - --parse-tree-only` fails outright), so there is no
    # "hand it content directly" escape from staging a real file somewhere.
    proj = tmp_path / "proj"
    (proj / "sub").mkdir(parents=True)
    src = proj / "x.c"
    src.write_text(
        "#ifdef SELF\n"
        "#define PICK 1\n"
        "#else\n"
        "#define SELF\n"
        '#include "sub/alias.c"\n'
        "#if PICK\n"
        "int from_the_snapshot(int x) { return x; }\n"
        "#else\n"
        "int from_the_live_file(int x) { return x; }\n"
        "#endif\n"
        "#endif\n"
    )
    snapshotted = src.read_bytes()
    (proj / "sub" / "alias.c").symlink_to("../x.c")
    src.write_bytes(snapshotted.replace(b"#define PICK 1", b"#define PICK 0"))

    names = [
        d.name
        for d in gate.extract_function_defs(
            "x.c", project_dir=str(proj), content=snapshotted
        )
    ]

    assert names == ["from_the_live_file"]  # the residual: not the snapshot's branch


@pytest.mark.skipif(not _HAVE_ESBMC, reason="needs esbmc + forseti on PATH")
def test_dotdot_in_the_given_path_stages_where_the_kernel_lands(
    tmp_path: Path,
) -> None:
    # The same divergence one level earlier: not a `..` inside an `#include`, but
    # one in the path the hook was handed. `abspath` collapses `proj/link/../x.c`
    # to `proj/x.c` lexically, while the read and the verify both go through the
    # kernel, which resolves `link` first and lands on `vendor/x.c`. Staged at the
    # collapsed path, the snapshot would wrap the *other* file's bytes in the
    # project's headers — a different `#if` branch, so enumeration reports a unit
    # the verify never sees, prunes the rest, and stamps the file.
    proj = tmp_path / "proj"
    vendor = tmp_path / "vendor"
    (vendor / "pkg").mkdir(parents=True)
    proj.mkdir()
    (proj / "link").symlink_to(vendor / "pkg")
    (vendor / "selector.h").write_text("#define PICK_TARGET 1\n")
    (proj / "selector.h").write_text("#define PICK_SPELLED 1\n")
    src = vendor / "x.c"
    src.write_text(
        '#include "selector.h"\n'
        "#ifdef PICK_TARGET\n"
        "int target_won(int x) { return x; }\n"
        "#endif\n"
        "#ifdef PICK_SPELLED\n"
        "int spelled_won(int x) { return x; }\n"
        "#endif\n"
    )
    given = "link/../x.c"

    in_place = gate.extract_functions(given, project_dir=str(proj))
    snapshotted = [
        d.name
        for d in gate.extract_function_defs(
            given, project_dir=str(proj), content=src.read_bytes()
        )
    ]

    assert in_place == ["target_won"]  # what the kernel — and so the verify — sees
    assert snapshotted == in_place


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
