"""Shared logic for the Forseti Claude Code verify-gate hooks.

Forseti stays a stateless verdict oracle: this module shells out to the
``forseti verify`` CLI once per edited C function and records the resulting
verdict in a small per-project gate file (``.forseti/gate_state.json``). The
*gate* is what is stateful — the write→verify→fix loop is owned by the harness
(the PostToolUse + Stop hooks), never by Forseti.

Function-level, no harness: ESBMC is invoked with ``--function <name>`` so it
havocs the parameters and checks the built-in safety properties (memory safety,
signed overflow, array bounds, division by zero, UB). Semantic/functional
contracts — which *do* need an expressed harness — are the v1 property path, not
this gate.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

# The safety-property profile. Bounds / pointer / div-by-zero are ESBMC defaults;
# signed overflow is opt-in, so we add it. Unsigned overflow is intentionally
# left OFF — wraparound is legal and common (hashes, counters) and enabling it
# yields false positives. Tune here; this is the one knob that defines "safe".
SAFETY_FLAGS: tuple[str, ...] = ("--overflow-check",)


class UnitsUnavailable(RuntimeError):
    """A file's function definitions could not be enumerated.

    Distinct from "the file defines no functions" (an empty list): a failed
    enumeration must surface as a blocking `error` verdict, never be mistaken for
    a clean pass (kill-safety — a not-verified unit can't silently slip through).
    Raised when `forseti list-units` cannot be run or its payload is unusable, and
    when the gate's own configuration is (`FORSETI_BUILD_FLAGS`).
    """


def _build_flags() -> tuple[str, ...]:
    """The project's build flags (`-I`, `-D`, ...) from ``FORSETI_BUILD_FLAGS``.

    Read per call, not once at import, so a hook process that sets the variable
    after this module loads still sees it. Split with `shlex` (stdlib — the hooks
    stay dependency-free) so a quoted path with spaces survives as one argument.

    Kept separate from `SAFETY_FLAGS` because the two have different destinations:
    build flags describe how the *translation unit* is constructed, so they must
    reach the `list-units` parse as well as the verify, while `--overflow-check`
    is a property-checking flag that means nothing to a `--parse-tree-only` run.
    Passing the wrong set to either is the failure this split exists to prevent —
    without its `-I` the enumeration fails outright (a blocking `error` on every
    edited file), and a missing `-D` silently changes *which* functions the file
    even defines, so the gate would verify a different unit list than the project
    builds.

    Unbalanced quoting raises `UnitsUnavailable` rather than escaping as a bare
    `ValueError`: quoting *is* this knob's interface (that is the whole reason for
    `shlex`), so a typo is the expected user error, and it has to land as the
    blocking verdict the gate is built around instead of a hook traceback.
    Degrading to no flags would be worse than either — it drops the `-I` and
    resurfaces as a baffling `PARSING ERROR` on a file that is perfectly fine.
    """
    raw = os.environ.get("FORSETI_BUILD_FLAGS", "")
    try:
        return tuple(shlex.split(raw))
    except ValueError as exc:
        raise UnitsUnavailable(
            f"FORSETI_BUILD_FLAGS is not parseable as a shell word list ({exc}): "
            f"{raw!r} — check for an unbalanced quote"
        ) from exc


# The default loop-unwind bound k. A VERIFIED is only ever "verified up to k".
# Override per project with FORSETI_UNWIND; functions with loops need a higher k
# (a k below the trip count can report a spurious verdict — roadmap Risk 1).
DEFAULT_K = int(os.environ.get("FORSETI_UNWIND", "1"))

# Per-function verify budget. Passed to `forseti verify --timeout` so ESBMC
# itself honors it — without it the Core CLI falls back to its 30s default and
# this knob is inert. The subprocess is bounded a little higher (below) so ESBMC
# self-terminates with UNKNOWN before the hard kill.
VERIFY_TIMEOUT_S = float(os.environ.get("FORSETI_VERIFY_TIMEOUT_S", "110"))
_SUBPROCESS_MARGIN_S = 15.0

# Budget for the one `forseti list-units` parse per edited file. A `--parse-tree-only`
# run does no solving, so it is fast; keep it well under the verify budget.
LIST_UNITS_TIMEOUT_S = float(os.environ.get("FORSETI_LIST_UNITS_TIMEOUT_S", "30"))

# How many times the Stop-gate blocks before it gives up and lets the turn end
# with a LOUD unverified residual (never a silent pass, but never an infinite
# loop either).
MAX_STOP_ATTEMPTS = 3

# How many times the same file content may be *started* through `verify_and_record`
# without its verdicts landing — a mid-run hook kill, or a scan that ended in a
# blocking `error` — before the scan stops retrying it (issue #140). The same trade
# as MAX_STOP_ATTEMPTS, one layer down: retrying is what recovers a killed verify,
# but an unbounded retry of a file that can never finish inside the hook budget
# would reset `stop_attempts` every round and loop forever. Once exhausted, the
# file's still-pending `unknown` units keep blocking and reach the loud residual
# via MAX_STOP_ATTEMPTS.
MAX_PENDING_VERIFY_ATTEMPTS = 3

# Symlinked directory components the snapshot's mirror plan will follow before it
# calls the chain a loop — Linux's own `MAXSYMLINKS` for one path resolution. A
# source reached through more than this could not have been `open`ed either.
_MAX_SYMLINK_HOPS = 40

C_SUFFIXES = {".c", ".h"}

# Out-of-band discovery (issue #99): a C file written via the `Bash` tool
# (`cat > f.c`, a generator script, `sed -i`) never fires the Write/Edit/MultiEdit
# PostToolUse hook, so it is never recorded and the Stop-gate lets the turn end
# with unverified C. Discovery uses `git status` to find changed C, keyed on a
# content hash (no mtime `cp -p`/`tar` hole) against a `scanned` baseline that
# `baseline_scanned` seeds at session start — so the gate fires on C changed
# *during* the session, never on pre-existing dirty or committed/third-party C the
# agent never touched (the over-reach a whole-tree scan couldn't avoid). The
# baseline has two halves, because the gate reads C from two places: `scanned`
# holds each dirty file's *worktree* bytes, and `baseline_blobs` holds the *index*
# and `HEAD` blob hashes the blob scan (`divergent_blob_sources`) compares against
# — without the second half a session opening at `MM foo.c` (staged WIP, worktree
# reverted) would block on the user's own pre-session staged blob (issue #139).
# `FORSETI_GATE_INCLUDE`/`FORSETI_GATE_EXCLUDE` narrow it further; a bare path
# segment (`vendor`) prunes any directory of that name, a glob (`*_generated.c`,
# `test/*`) is matched against the project-relative path.
_DEFAULT_EXCLUDE_GLOBS: tuple[str, ...] = ("third_party", "vendor", "node_modules")

_STATE_DIR = ".forseti"
_STATE_FILE = "gate_state.json"
_LOCK_FILE = "gate_state.lock"

# Verdict string for a unit the gate declines to check at the function level: it
# takes a pointer/array parameter, so an unconstrained (havoc'd) caller makes the
# function-level memory-safety verdict meaningless — a *sound* but unactionable
# `dereference failure`. Rather than feed that phantom back as a fixable
# counterexample, the gate marks the unit NEEDS_CONTRACT: not verified, but
# non-blocking and loudly reported. A generated memory precondition/harness
# (issue #122, stage S2) is what will actually verify these. See RFC-0003.
NEEDS_CONTRACT = "needs_contract"
_NEEDS_CONTRACT_DETAIL = (
    "pointer/array parameter(s); function-level safety is unreliable without a "
    "memory precondition/harness — not gated (see issue #122)"
)


@dataclass(frozen=True)
class UnitVerdict:
    """One function's verdict from a single ``forseti verify`` call.

    ``argv`` (the exact ESBMC command line) and ``duration_s`` (wall-clock of the
    verify) come from the CLI's ``--json`` payload and are carried for the loop
    trace (``event_log``); they are ``None`` when the call never reached ESBMC
    (CLI missing, timeout, unparseable output).
    """

    unit_id: str  # "relpath::symbol"
    file: str
    function: str
    verdict: str  # verified | violated | unknown | error | needs_contract
    k: int
    counterexample: str | None = None
    detail: str | None = None
    argv: tuple[str, ...] | None = None
    duration_s: float | None = None

    @property
    def passed(self) -> bool:
        return self.verdict == "verified"


def resolve_forseti_cmd() -> list[str]:
    """The command prefix for the Forseti CLI: the installed script, else the module."""
    found = shutil.which("forseti")
    if found:
        return [found]
    return [sys.executable, "-m", "forseti.core"]


def is_c_source(path: str | os.PathLike[str]) -> bool:
    return Path(path).suffix.lower() in C_SUFFIXES


@dataclass(frozen=True)
class FuncDef:
    """A top-level function definition the gate found.

    `takes_pointer` is true when any parameter is a pointer or array — the case
    the function-level gate cannot verify without a materialized backing object
    (`NEEDS_CONTRACT`). It is read from `forseti list-units`' canonical,
    typedef-resolved parameter types, so a pointer hidden behind a typedef counts.
    """

    name: str
    takes_pointer: bool


def _func_def(unit: object) -> FuncDef:
    """One `units` entry → `FuncDef`, with both fields type-checked, never coerced.

    `bool()` on a non-boolean silently *inverts* the classification: the JSON
    string `"false"` is truthy, so a scalar function reported by an incompatible
    `forseti` build would be parked in non-blocking `NEEDS_CONTRACT` and never
    verified — the edited file then passes the gate unchecked. A wrong type is an
    unusable payload, not a value with a sensible default, so it blocks. A
    *missing* `takes_pointer` blocks for the same reason (it used to default to
    "scalar"), mirroring how an absent `units` key is treated.
    """
    if not isinstance(unit, dict):
        raise UnitsUnavailable(
            f"malformed list-units payload: units entry is not an object: {unit!r}"
        )
    name = unit.get("function")
    if not isinstance(name, str) or not name:
        raise UnitsUnavailable(
            "malformed list-units payload: units entry has no `function` name: "
            f"{unit!r}"
        )
    takes_pointer = unit.get("takes_pointer")
    if not isinstance(takes_pointer, bool):
        raise UnitsUnavailable(
            f"malformed list-units payload: `takes_pointer` for {name!r} is not a "
            f"JSON boolean: {takes_pointer!r}"
        )
    return FuncDef(name, takes_pointer)


def _kernel_dir(path: str) -> str:
    """`path` with each ``..`` resolved the way the kernel walks it, nothing else.

    `os.path.abspath` collapses ``a/link/..`` to ``a`` lexically, but the kernel
    resolves ``link`` before it climbs, landing on the parent of the link's
    *target*. Since the gate reads and verifies through that same kernel walk, a
    snapshot staged at the collapsed path would stand in for a different file's
    neighbourhood — or a different file altogether.

    Only ``..`` forces a resolution. A symlinked component *not* followed by one
    stays spelled, so `_mirror_plan` still sees it and reproduces it as a symlink
    — which is what keeps the two ways of resolving a ``..`` inside an ``#include``
    agreeing (see there). Paths without ``..`` come back untouched.
    """
    cur = os.sep if os.path.isabs(path) else os.getcwd()
    for part in path.split(os.sep):
        if not part or part == os.curdir:
            continue
        if part == os.pardir:
            # `dirname` of the *resolved* path: ``/`` is its own parent, which is
            # also what a climb past the root does in place.
            cur = os.path.dirname(os.path.realpath(cur))
        else:
            cur = os.path.join(cur, part)
    return cur


def _contains(root: str, path: str) -> bool:
    """Is `path` `root` itself or lexically below it?"""
    prefix = root if root.endswith(os.sep) else root + os.sep
    return path == root or path.startswith(prefix)


def _mirror_root(src_dir: str, project_dir: str) -> str:
    """The highest ancestor of `src_dir` whose entries the snapshot mirrors.

    `project_dir` when it contains `src_dir` — the gate's whole universe is the
    project, and mirroring stops there rather than walking to ``/``, which would
    mean a `scandir` of `$HOME` (thousands of entries, and an unreadable one turns
    into a blocking `error`) for every edit. Otherwise the source's own directory:
    a file outside the project has no ancestry the gate can claim.

    Containment is tried against the project dir as spelled *and* as resolved,
    because the two spellings do not have to agree: with `proj -> /data/proj`, a
    hook may be handed `<cwd>/proj/src/x.c` while out-of-band discovery builds its
    paths on the git root, which `git rev-parse` reports resolved. Lexically, one
    of those is not under the other — and calling a file physically inside the
    project "outside" costs it its whole ancestry, so an ordinary
    ``#include "../common.h"`` stops resolving and enumeration blocks (or, with an
    `-I` to fall through to, quietly reads a different header). Whichever test
    matches returns a prefix of `src_dir`, so the source keeps its own spelling.
    """
    root = os.path.abspath(project_dir)
    if _contains(root, src_dir):
        return root
    real_root = os.path.realpath(project_dir)
    return real_root if _contains(real_root, src_dir) else src_dir


def _staged(tmp: str, path: str) -> str:
    """`path`'s place inside the snapshot tree: the same depth, rooted at `tmp`."""
    return os.path.join(tmp, os.path.relpath(path, os.sep))


def _file_id(path: str) -> tuple[int, int] | None:
    """``(st_dev, st_ino)`` for `path`, links followed; ``None`` if it cannot stat."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_dev, st.st_ino


def _mirror_entries(
    real_dir: str,
    into: str,
    *,
    source_id: tuple[int, int] | None = None,
    snapshot: str | None = None,
) -> None:
    """Symlink every entry of `real_dir` into `into` that is not already there.

    Whatever the caller has already put in `into` is the mirrored tree itself —
    a level of the chain down to the snapshot, a reproduced symlink component, or
    the snapshot file — and must never be overwritten by a link back to the real
    entry of the same name. `lexists`, so a *dangling* link already staged counts
    too. A directory is linked whole, so ``#include "sub/h.h"`` resolves through
    it.

    An entry that is **another name for the source** — a sibling
    ``alias.c -> x.c``, or a hard link — is linked to `snapshot` instead. Left
    pointing at the real file it would be a door back out of the immutable copy:
    a translation unit that includes itself under that name reads whatever is on
    disk *now*, and its ``#define``s then decide which units the snapshot
    enumerates (issue #141's own race, one include deep). Compared by
    ``(st_dev, st_ino)`` so a hard link counts, and an entry that cannot be
    stat'd is still linked to the real path — one extra `stat` per entry beside
    the `lexists` already spent.
    """
    with os.scandir(real_dir) as entries:
        for entry in entries:
            dest = os.path.join(into, entry.name)
            if os.path.lexists(dest):
                continue
            checkable = snapshot is not None and source_id is not None
            if checkable and _file_id(entry.path) == source_id:
                os.symlink(str(snapshot), dest)  # another name for the source
            else:
                os.symlink(entry.path, dest)


def _mirror_plan(
    src_dir: str, project_dir: str
) -> tuple[list[str], list[tuple[str, str]]]:
    """How to reproduce `src_dir`'s ancestry: real directories, symlink components.

    Returns the directories to recreate as *real* directories (each mirroring its
    own entries) and the ``(spelled link, its target)`` pairs to recreate as
    *symlinks*, so that both ways of resolving a ``..`` agree with the in-place
    parse:

    * **The kernel walks it.** ``..`` from a directory reached through a symlink
      is the parent of the link's *target*, not of the link — clang concatenates
      the including file's directory with the spelled path and hands the result
      to ``open``, so this is the resolution that actually happens (measured, not
      assumed). Reproducing a symlinked component as a real directory would make
      ``..`` climb the spelled chain instead, silently selecting a different
      header — which flips ``#if`` branches, so enumeration reports units the
      verify never sees, prunes the rest, and stamps the file.
    * **The caller normalizes it lexically.** The spelled chain is reproduced
      *as spelled*, so ``foo/../bar`` collapsed before the open lands on the
      mirror of the same directory it lands on in place.

    The walk stops at the first symlinked component, hops to its target, and
    resumes from that target's own `_mirror_root` — so a link into the project
    keeps the full ancestry, and a link out of it mirrors the target directory
    (entries yes, parent no) exactly as a source outside the project does.
    """
    real_dirs: list[str] = []
    links: list[tuple[str, str]] = []
    hops = 0
    base = _mirror_root(src_dir, project_dir)
    steps = [] if src_dir == base else os.path.relpath(src_dir, base).split(os.sep)
    while True:
        if base not in real_dirs:
            real_dirs.append(base)
        cur, hop = base, None
        for i, step in enumerate(steps):
            nxt = os.path.join(cur, step)
            if os.path.islink(nxt):
                hop = (nxt, steps[i + 1 :])
                break
            cur = nxt
            if cur not in real_dirs:
                real_dirs.append(cur)
        if hop is None:
            return real_dirs, links
        link, rest = hop
        # A cyclic component would loop here forever, and `os.path.realpath` will
        # not say so: it gives up and hands back a path that is *not* resolved —
        # `a -> a` unchanged, `a -> a/x` one component longer every time. The
        # second shape is why this is a **budget**, not a seen-set: with the target
        # growing, no (link, rest) state ever recurs, so a repeat-detector spins.
        #
        # The budget is the kernel's own: `MAXSYMLINKS` traversals for one path
        # resolution. Anything past it could not have been `open`ed either — this
        # walk only runs for a source already read — so blocking is the honest
        # answer, and it leaves room for the legitimate repeat this must not
        # reject (`self -> .` walked as `self/self/x.c` visits one component
        # twice, and the kernel counts that the same way).
        hops += 1
        if hops > _MAX_SYMLINK_HOPS:
            raise OSError(errno.ELOOP, "symlinked directory component loops", link)
        target = os.path.realpath(link)
        # Deduped: the same component can legitimately be reached twice (the
        # `self -> .` walk again), and staging one plan entry twice would raise
        # `FileExistsError` at `os.symlink` — a blocking `error` by another route.
        if (link, target) not in links:
            links.append((link, target))
        # Against the *resolved* project dir: `target` is fully resolved, so that
        # is the apples-to-apples comparison, and a link that lands back inside a
        # project which is itself reached through a symlink keeps its full
        # ancestry rather than being treated as foreign.
        base = _mirror_root(target, os.path.realpath(project_dir))
        below = [] if target == base else os.path.relpath(target, base).split(os.sep)
        # `target` is fully resolved, so nothing in `below` can be a symlink; only
        # the not-yet-walked `rest` can hop again.
        steps = below + rest


@contextlib.contextmanager
def _enumerable_source(
    file_path: str | os.PathLike[str], content: bytes | None, *, project_dir: str
) -> Iterator[str]:
    """Yield the path to enumerate for `file_path` — itself, or an immutable copy.

    With `content` ``None`` the file is enumerated where it lies. Given `content`,
    those exact bytes are written to a snapshot in a private temp directory and
    *that* is what gets parsed — so the enumeration is of the caller's bytes by
    construction, not of whatever the file happens to hold when the CLI re-reads
    it (issue #141). Write the bytes; never `shutil.copy` or re-read `file_path`,
    or the race walks straight back in.

    The temp tree is then made to **stand in for the source's own
    neighbourhood**, because clang resolves a quoted ``#include`` against the
    directory of the file it is reading — so the snapshot has to *be* in an
    equivalent directory, including for headers reached through it, which clang
    opens by their linked path and whose own quoted includes therefore resolve
    the same way. Two things make it equivalent:

    * **Siblings.** Every other entry of the source's directory is symlinked
      beside the snapshot, so ``#include "sibling.h"`` resolves to the real one —
      except a sibling that *is* the source under another name, which leads back
      to the snapshot (`_mirror_entries`).

    Immutability is the *top-level* file's, and two ways of naming the source
    still reach the live one: an **absolute** include (clang opens the spelled
    path, which no mirrored neighbourhood can stand in for) and an alias *below*
    a mirrored directory (``sub/alias.c -> ../x.c``, since a directory is linked
    whole). A translation unit that includes itself that way, during a transient
    A -> B -> A, can enumerate B — the same residual the verify loop already
    carries, and the same fix: enumeration of content the parser cannot reach
    around, which means a Core CLI taking content on stdin with an explicit
    include root, not a deeper mirror. Mirroring subdirectories entry-by-entry
    instead would `scandir` and `stat` the source's whole subtree on every edit.
    Pinned by ``test_a_nested_source_alias_is_a_known_residual``.
    * **Ancestry.** The chain from `_mirror_root` down to the source's directory
      is reproduced level by level, each mirroring its own entries bar the names
      the chain itself already occupies, so ``#include "../common.h"`` — the
      ordinary shape for a ``src/foo.c`` — resolves too. A real directory where
      the source has a real directory and a *symlink* where it has a symlink
      (`_mirror_plan`): ``..`` is then the same directory the in-place parse
      reaches whether the caller normalizes ``foo/../bar`` lexically or lets the
      kernel walk it.

    The tree also reproduces the source's **absolute depth** (``/a/b/x.c`` stages
    at ``<tmp>/a/b/x.c``), with the levels above the mirror root real but empty.
    A ``..`` chain that climbs past the root then lands on an empty private
    directory instead of on ``/tmp``, where a same-named file — anyone's, the
    directory is world-writable — would silently be included.

    Above the mirror root, resolution is **not** reproduced, and the miss is not
    the end of it: clang falls through to the ``-I`` search with the *spelled*
    path, so ``#include "../above.h"`` from a file at the project root ends at
    whatever ``<-I dir>/../above.h`` finds — a blocking `error` when there is no
    such flag, and otherwise a header the in-place parse need not have picked.
    ``-I.`` is the spelling that reproduces it (the CLI runs with
    ``cwd=project_dir``). Unchanged from the siblings-only staging this replaces;
    what the padding removes is the far worse variant where the miss landed in
    ``/tmp`` itself. Pinned by
    ``test_include_above_the_mirror_root_is_a_known_residual``.

    An ``-I <source dir>`` looks like a cheaper substitute for all this and is not
    one: ``-I`` also joins the **angle-bracket** search, and it appends *after*
    any ``-iquote`` from `FORSETI_BUILD_FLAGS` instead of taking the source
    directory's first-place precedence. Either one silently selects a different
    header than the in-place parse would — ``#include <config.h>`` next to a
    generated ``config.h`` is the standard shape — which flips ``#if`` branches,
    so enumeration reports units the verify never sees, prunes the rest, and
    stamps the file. (ESBMC 8.3.0 offers no quoted-only include flag: it rejects
    clang's ``-iquote``, exposing only ``-I``/``--idirafter``.) Mirroring needs no
    flag at all and leaves every search path exactly as it was.

    The source's own path is derived **as spelled** — absolutized against
    `project_dir` (the subprocess's cwd) but never `resolve()`d — and the
    snapshot is staged there. clang searches the directory of the path it was
    *given*, so for a symlinked source file resolving would mirror the link
    target's directory instead: a header beside the link would go missing, and a
    same-named header beside the target would be silently preferred.

    One part of it is not spelled: a ``..`` in the given path is resolved the way
    the kernel walks it (`_kernel_dir`). The kernel is what picked the file whose
    bytes were hashed and what will pick it again for the verify, so collapsing
    ``proj/link/../x.c`` lexically to ``proj/x.c`` would surround content read
    from the link target's parent with the *project's* headers — a different
    translation unit, which is the failure this staging exists to prevent.
    Symlinked directory components not followed by a ``..`` stay spelled and are
    reproduced as symlinks; see `_mirror_plan`.
    """
    if content is None:
        yield str(file_path)
        return
    spelled = os.path.join(project_dir, os.fspath(file_path))
    src_dir, name = _kernel_dir(os.path.dirname(spelled)), os.path.basename(spelled)
    target = os.path.join(src_dir, name)
    # Every `OSError` the staging can raise — an unwritable/missing `TMPDIR`,
    # `ENOSPC`, `EDQUOT`, an unreadable directory anywhere on the mirrored chain,
    # a failed cleanup — has to land as `UnitsUnavailable`, the one exception
    # `verify_and_record` turns into a blocking `error`. Left bare it escapes to
    # the hook, which installs no handler: the process dies with a traceback and
    # exit 1 (not the blocking exit 2) before any verdict or `scanned` stamp is
    # written, so the edit passes with the file unverified. Outside a git work
    # tree nothing backstops that — the same reason the post-verify drift check
    # blocks rather than deferring to the out-of-band scan. Mirrors how
    # `_list_units` already converts `OSError` from the spawn.
    try:
        with tempfile.TemporaryDirectory(prefix="forseti-units-") as tmp:
            real_dirs, links = _mirror_plan(src_dir, project_dir)
            # Order is load-bearing. The whole skeleton — every level of the
            # chain, every reproduced symlink component, and the snapshot itself
            # — has to exist before any level mirrors its entries, because
            # `_mirror_entries` yields to whatever is already staged. Mirror the
            # source's directory first and its own name would become a link back
            # to the real file, which `write_bytes` would then follow and
            # *truncate the user's source*.
            for real in real_dirs:
                os.makedirs(_staged(tmp, real), exist_ok=True)
            for link, link_target in links:
                os.symlink(_staged(tmp, link_target), _staged(tmp, link))
            snapshot = Path(_staged(tmp, target))
            snapshot.write_bytes(content)
            # Taken before the mirroring, off the real file: any entry that turns
            # out to be this same inode is another name for the source and must
            # lead to the snapshot, not back out to the live file.
            source_id = _file_id(target)
            for real in real_dirs:
                _mirror_entries(
                    real,
                    _staged(tmp, real),
                    source_id=source_id,
                    snapshot=str(snapshot),
                )
            yield str(snapshot)
    except OSError as exc:
        raise UnitsUnavailable(
            f"could not stage a snapshot of {name} for enumeration (check "
            f"TMPDIR, free space, and read access to {src_dir} and the "
            f"directories `_mirror_plan` reproduces for it): {exc}"
        ) from exc


def extract_function_defs(
    file_path: str | os.PathLike[str],
    *,
    project_dir: str,
    content: bytes | None = None,
) -> list[FuncDef]:
    """Enumerate `file_path`'s function definitions via ``forseti list-units``.

    Shells out to the same authoritative clang-based frontend that *verifies* the
    unit (``forseti list-units --json``) rather than a regex, so typedef'd
    pointers, K&R and multi-line signatures, function-like macros, ``#if`` blocks,
    and a ``*`` inside a comment are all classified correctly (issue #131). The
    hook stays dependency-free — it only spawns the CLI, never imports the
    package. Raises `UnitsUnavailable` when the CLI cannot be run or the parse
    fails (missing binary, C parse error, timeout, unreadable file), so the caller
    records a blocking `error` verdict instead of silently skipping the file.

    Pass `content` to enumerate exactly those bytes rather than re-reading the
    file (`_enumerable_source`): the caller has already hashed them, and a
    concurrent rewrite between the hash and the CLI's read is what issue #141
    closes. Omit it to parse the file in place.

    Only ``.c`` translation units are enumerated: ESBMC cannot parse a header
    standalone (``forseti verify`` errors on a ``.h`` too — "failed to figure out
    type of file"), and clang's path-match keeps a header-resident definition out
    of its includer's unit list, so functions defined in a ``.h`` are simply out
    of gate scope. A non-``.c`` file yields ``[]`` (a clean pass) rather than an
    unresolvable block.
    """
    if Path(file_path).suffix.lower() != ".c":
        return []
    with _enumerable_source(file_path, content, project_dir=project_dir) as source:
        return _list_units(source, project_dir=project_dir)


def _list_units(source: str, *, project_dir: str) -> list[FuncDef]:
    """Run ``forseti list-units`` on `source` and map its payload to `FuncDef`s."""
    argv = [
        *resolve_forseti_cmd(),
        "list-units",
        source,
        "--json",
        # Pass the float through, don't truncate. `list-units` hands its --timeout
        # straight to `subprocess.run(timeout=...)` (esbmc's own --timeout is not
        # used on the parse-tree-only path), where `0` expires immediately rather
        # than meaning "unbounded" — so `int(0.5)` would turn a half-second budget
        # into a blocking `error` on every edited `.c`. `str()` round-trips exactly;
        # argparse reparses it as the float the CLI declares.
        "--timeout",
        str(LIST_UNITS_TIMEOUT_S),
    ]
    # The parse needs the project's own `-I`/`-D` — a translation unit whose
    # `#include` cannot be resolved makes esbmc exit nonzero, which is a blocking
    # `error` on every edited file rather than a listing. Only the build flags go
    # here: `SAFETY_FLAGS` is for the verify, and a property flag on a
    # `--parse-tree-only` run is at best inert. Nothing is added for a snapshot —
    # `_enumerable_source` mirrors the source's directory precisely so the search
    # paths stay identical to the in-place parse.
    build_flags = _build_flags()
    if build_flags:
        argv += ["--", *build_flags]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=LIST_UNITS_TIMEOUT_S + _SUBPROCESS_MARGIN_S,
            cwd=project_dir,
        )
    except OSError as exc:  # missing CLI, etc. — resolve_forseti_cmd falls back to -m
        raise UnitsUnavailable(
            "forseti CLI could not be launched; install the forseti package "
            f"(pip install -e .) so `forseti` is on PATH: {exc}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise UnitsUnavailable(
            f"list-units exceeded {LIST_UNITS_TIMEOUT_S:g}s (raise "
            "FORSETI_LIST_UNITS_TIMEOUT_S)"
        ) from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()[:800] or f"exit {proc.returncode}"
        raise UnitsUnavailable(detail)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise UnitsUnavailable(
            (proc.stderr or proc.stdout).strip()[:800] or "no JSON output"
        ) from exc
    # Require an explicit list-valued `units`. Defaulting a missing key to `[]`
    # would let an older/incompatible `forseti` build — one that exits 0 with a
    # JSON object that has no `units` member — read as "this file defines no
    # functions" (a clean pass), so an edited `.c` slips through unverified. An
    # *empty* list is still a legitimate no-functions pass; only absence blocks.
    if not isinstance(payload, dict) or not isinstance(payload.get("units"), list):
        raise UnitsUnavailable(
            "list-units payload has no list-valued `units` key (incompatible "
            f"`forseti` build?): {proc.stdout.strip()[:800] or '<empty>'}"
        )
    return [_func_def(u) for u in payload["units"]]


def extract_functions(
    file_path: str | os.PathLike[str], *, project_dir: str
) -> list[str]:
    """Names of `file_path`'s top-level functions (via ``forseti list-units``)."""
    return [d.name for d in extract_function_defs(file_path, project_dir=project_dir)]


def unit_id(project_dir: str, file_path: str) -> str:
    """The gate's identity key for `file_path`: `project_dir`-relative, `..` resolved.

    A plain `os.path.relpath` normalizes `..` lexically, which is wrong whenever a
    component before it is a symlink: `proj/link/../x.c` with `link -> external/pkg`
    collapses to `proj/x.c`, aliasing the key onto a real `proj/x.c` even though the
    kernel — which is what actually opens, hashes and verifies this path — lands on
    `external/x.c`. Resolving `..` the way `_kernel_dir` does first (issue #152) makes
    the key track the same file the rest of the gate reads; a source outside the
    project then keys as `../external/x.c` rather than colliding with an in-project
    name. A path with no `..` is untouched by `_kernel_dir`, so its key is unchanged —
    no migration needed for the common case, only a one-off re-verify for the rare
    `..`-through-a-symlink unit whose key now differs.
    """
    kernel_dir = _kernel_dir(os.path.dirname(file_path))
    resolved = os.path.join(kernel_dir, os.path.basename(file_path))
    try:
        return os.path.relpath(resolved, project_dir)
    except ValueError:
        return resolved


def content_hash(path: str | os.PathLike[str]) -> str | None:
    """SHA-256 of a file's bytes, or ``None`` if it cannot be read.

    The freshness key for the out-of-band scan: a file is re-verified only when
    its content hash differs from the one recorded at its last verify. Content —
    not mtime — so a `cp -p`/`tar` that preserves an old timestamp cannot sneak an
    unverified change past the gate, and (load-bearing) an *unchanged* file is
    never re-verified, which is what keeps the Stop-gate's `stop_attempts` counter
    from being reset every turn.
    """
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _globs(value: str | None) -> tuple[str, ...]:
    """Split a ``:``/``,``-separated include/exclude setting into patterns."""
    if not value:
        return ()
    return tuple(p.strip() for p in re.split(r"[:,]", value) if p.strip())


def _matches(rel: str, patterns: Iterable[str]) -> bool:
    """True if project-relative path `rel` matches any include/exclude `patterns`.

    A pattern with a glob metacharacter or a ``/`` is matched against the whole
    relative path (``fnmatch``); a bare name (``vendor``) matches when it is any
    path segment, so it prunes a directory of that name at any depth.
    """
    parts = set(PurePosixPath(rel).parts)
    for pat in patterns:
        if "/" in pat or any(ch in pat for ch in "*?["):
            if fnmatch.fnmatch(rel, pat):
                return True
        elif pat in parts:
            return True
    return False


def _included(rel: str) -> bool:
    """Apply the `FORSETI_GATE_INCLUDE`/`FORSETI_GATE_EXCLUDE` globs to `rel`.

    Exclude wins over include. When `FORSETI_GATE_EXCLUDE` is unset the built-in
    `_DEFAULT_EXCLUDE_GLOBS` apply; setting it replaces (not extends) them.
    """
    exclude = _globs(os.environ.get("FORSETI_GATE_EXCLUDE")) or _DEFAULT_EXCLUDE_GLOBS
    if _matches(rel, exclude):
        return False
    include = _globs(os.environ.get("FORSETI_GATE_INCLUDE"))
    return not include or _matches(rel, include)


def _iter_porcelain_z(out: str) -> Iterator[tuple[str, str]]:
    """Yield ``(XY status, path)`` for each ``git status --porcelain -z`` record.

    Each record is ``XY<space>path``; a rename/copy record is followed by a second
    NUL-separated field holding the *original* path, which we skip (the current
    path — the first field — is what we verify). ``-z`` means paths are never
    quoted, so no unescaping is needed.
    """
    tokens = out.split("\0")
    i = 0
    while i < len(tokens):
        entry = tokens[i]
        i += 1
        if not entry or len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        if "R" in status or "C" in status:
            i += 1  # the following token is the rename/copy source — skip it
        yield status, path


def _parse_porcelain_z(out: str) -> list[str]:
    """Relative paths from ``git status --porcelain -z`` (current path per record)."""
    return [path for _, path in _iter_porcelain_z(out)]


def _staged_paths_from_porcelain(out: str) -> list[str]:
    """Paths with a STAGED change: porcelain X-status neither clean (space) nor ``?``.

    An ``X`` of ``M``/``A``/``R``/``C``/… means the index holds a blob differing from
    ``HEAD`` (a `git add` of new or modified content) — the blob a plain `git commit`
    would ship. A clean ``X`` (space) is a worktree-only edit the porcelain scan
    already covers; ``?`` is untracked (no index entry).
    """
    return [path for status, path in _iter_porcelain_z(out) if status[0] not in " ?"]


def _git(project_dir: str, *args: str) -> str | None:
    """Run a git subcommand in `project_dir`; its stdout, or ``None`` on failure.

    ``None`` covers git being absent, `project_dir` not being a work tree, or a
    non-zero exit — the caller treats all three as "out-of-band discovery is
    unavailable" (distinct from a clean tree), so it can report the degraded scope
    loudly instead of silently skipping.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", project_dir, *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _git_bytes(project_dir: str, *args: str) -> bytes | None:
    """Like `_git`, but return raw stdout bytes (no text decoding), or ``None``.

    Used to hash a git blob (``git cat-file blob <ref>``): the hash must be taken
    over the exact bytes so it lines up with `content_hash`'s ``read_bytes`` digest —
    a text decode/re-encode (newline translation) would spuriously diverge.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", project_dir, *args],
            capture_output=True,
            timeout=30,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def git_blob_hash(project_dir: str, ref: str) -> str | None:
    """SHA-256 of the git blob at `ref` (``:path`` = index, ``HEAD:path`` = commit).

    ``None`` when the ref does not resolve to a blob — a staged deletion, a rename's
    stale source, or a path absent from ``HEAD`` — so the caller treats "no blob
    there" as nothing to gate rather than a divergence.
    """
    out = _git_bytes(project_dir, "cat-file", "blob", ref)
    return hashlib.sha256(out).hexdigest() if out is not None else None


def staged_source_paths(project_dir: str) -> list[str]:
    """Repo-root-relative paths with a STAGED change in the index (best-effort).

    The blobs a plain `git commit` would ship. Empty when not a git work tree.
    """
    out = _git(project_dir, "status", "--porcelain", "-z", "-uall")
    return [] if out is None else _staged_paths_from_porcelain(out)


def git_changed_files(project_dir: str) -> list[str] | None:
    """Repo-root-relative paths `git status` reports as changed, or ``None``.

    ``--untracked-files=all`` so a brand-new Bash-written C file (untracked) is
    seen; git's own ignore rules already drop gitignored build/vendor output, so
    the scan never sweeps generated trees. Paths are relative to the repository
    root (git's porcelain contract), not `project_dir`.
    """
    out = _git(project_dir, "status", "--porcelain", "-z", "-uall")
    return None if out is None else _parse_porcelain_z(out)


def git_head(project_dir: str) -> str | None:
    """The current ``HEAD`` commit SHA, or ``None``.

    ``None`` when `project_dir` is not a work tree *or* the repo has no commits
    yet (`git rev-parse HEAD` fails) — the SessionStart baseline stores this, and a
    ``None`` baseline simply disables the committed-since scan (below).
    """
    out = _git(project_dir, "rev-parse", "HEAD")
    return out.strip() if out else None


def git_committed_files_since(project_dir: str, baseline_head: str | None) -> list[str]:
    """Repo-root-relative paths committed since `baseline_head` (best-effort).

    ``git status`` reports only working-tree/index state, so a Bash command that
    writes a C file *and* commits it in one shot (``cat > f.c && git commit ...``)
    leaves a clean tree the porcelain scan cannot see (issue #99 review). Diffing
    the session baseline HEAD against the current HEAD recovers those committed
    paths. Best-effort and purely additive: an empty list when there is no
    baseline, HEAD has not moved, or the diff fails (e.g. the baseline commit was
    rewritten) — the porcelain scan still covers uncommitted work either way.

    ``-z`` for unquoted NUL-separated paths; ``--name-only`` emits one entry per
    changed path (a rename's stale source, if listed, is dropped later by the
    existence filter), so no rename bookkeeping is needed here.
    """
    if not baseline_head:
        return []
    out = _git(project_dir, "diff", "--name-only", "-z", baseline_head, "HEAD")
    if out is None:
        return []
    return [p for p in out.split("\0") if p]


def _in_scope_c_abspath(
    project_dir: str, root: str, proj_real: str, rel: str
) -> str | None:
    """Absolute path for repo-root-relative `rel` if it is an in-scope, included C
    source under `project_dir`, else ``None``.

    Applies the C-suffix, project-subtree, and include/exclude-glob filters shared by
    the worktree scan (`discover_changed_c_sources`) and the staged/committed-blob
    scan (`divergent_blob_sources`). Deliberately does **not** check file existence —
    a staged or committed blob can outlive its worktree file (a `git add`-then-`rm`),
    and the blob scan must still gate it.
    """
    abspath = os.path.join(root, rel)
    if not is_c_source(abspath):
        return None
    try:
        if os.path.commonpath([proj_real, os.path.realpath(abspath)]) != proj_real:
            return None  # changed outside this project subtree — out of scope
    except ValueError:
        return None  # different drive/root — cannot be under proj
    if not _included(os.path.relpath(abspath, project_dir)):
        return None
    return abspath


def discover_changed_c_sources(
    project_dir: str, *, baseline_head: str | None = None
) -> list[str] | None:
    """Absolute paths of changed, still-present C sources under `project_dir`.

    git reports paths relative to the *repository root*, which need not be
    `project_dir`, so they are joined to the resolved root and then scoped back to
    `project_dir` (a subdir checkout gates only its own changes). Include/exclude
    globs are applied to the project-relative path, matching `unit_id`. ``None``
    when discovery is unavailable (not a git repo).

    Discovery is the union of the working-tree/index changes (``git status``) and
    C committed since the session `baseline_head` (issue #99 review): a Bash
    command that writes and commits a C file in one shot leaves a clean tree the
    porcelain scan alone would miss. The union is deduped and content-hash
    freshness (`stale_sources`) remains the real gate, so a file committed
    *unchanged* since the baseline is filtered back out. A ``None`` baseline (no
    commits yet, or SessionStart never seeded one) degrades cleanly to the
    porcelain-only scan.
    """
    root = _git(project_dir, "rev-parse", "--show-toplevel")
    rels = git_changed_files(project_dir)
    if root is None or rels is None:
        return None
    root = root.strip()
    # Both git subcommands emit repo-root-relative paths, so the committed-since
    # set unions straight into `rels` before the shared join/scope/filter below.
    # dict.fromkeys dedups while preserving order (a path both dirty and committed
    # is verified once).
    committed = git_committed_files_since(project_dir, baseline_head)
    rels = list(dict.fromkeys([*rels, *committed]))
    # `realpath` is used only to compare against the (possibly symlinked) project
    # subtree; the returned path keeps git's own root, so scoping never restages a
    # file. That is *not* the same as the `unit_id` key agreeing with the one a
    # PostToolUse edit produces: `git rev-parse --show-toplevel` reports the root
    # resolved, so when `project_dir` is a symlinked spelling the same file keys as
    # `../real/src/x.c` here and `src/x.c` there — measured, and filed as #161, the
    # other half of #152's aliasing class, since canonicalizing `project_dir` would
    # change the persisted key for every symlinked-root project, not just this one.
    proj_real = os.path.realpath(project_dir)
    found: list[str] = []
    for rel in rels:
        abspath = _in_scope_c_abspath(project_dir, root, proj_real, rel)
        # A path git reports changed but that is gone from disk (a Bash `rm`) is
        # skipped here — there is nothing to *verify*. Its recorded units are
        # reconciled separately by `prune_deleted_units`, which keys off actual
        # file existence so it also catches an untracked file git never tracked
        # (issue #99 review): keep discovery about "what to verify", pruning about
        # "what no longer exists". A staged/committed blob that outlives its worktree
        # file is gated instead by `divergent_blob_sources`.
        if abspath is not None and os.path.isfile(abspath):
            found.append(abspath)
    return found


def _pending_attempts(entry: object, digest: str) -> int | None:
    """Unfinished-verify count `entry` records for `digest`, else ``None``.

    ``None`` means "no unfinished verify of this content" — the entry is absent,
    malformed, or records different bytes. A present entry with an unreadable
    counter reads as ``0`` (retry) rather than as exhausted, and the retry rewrites
    the counter as an int, so a corrupt value can never loop.
    """
    if not isinstance(entry, dict) or entry.get("hash") != digest:
        return None
    attempts = entry.get("attempts")
    return attempts if isinstance(attempts, int) and attempts > 0 else 0


def _pending_owner(entry: object) -> tuple[object, object] | None:
    """Which run created marker `entry`: its content and its process, else ``None``.

    The ownership test for the *success* cleanup — a run may clear only the marker it
    stored itself. `hash` separates runs on different content; `pid` separates two
    live runs of the *same* content, and separates a cleared-then-recreated marker
    from the byte-identical one it replaced.

    `attempts` is deliberately excluded. It is a shared budget, not identity: an
    erroring run charges an attempt to a marker it does not own (`_blocking_error`),
    so counting it here would let that charge orphan the owner's own claim.
    """
    if not isinstance(entry, dict):
        return None
    return entry.get("hash"), entry.get("pid")


def stale_sources(project_dir: str, state: dict, files: Iterable[str]) -> list[str]:
    """Subset of `files` needing a (re-)verify: changed content, or a killed verify.

    A file is stale when it has never been verified (`scanned` has no entry), when
    its current content hash differs from the recorded one — i.e. it was written or
    modified out-of-band since the gate last saw it — or when an **unfinished**
    verify of exactly this content is on record (issue #140).

    That last case closes the kill hole: `verify_and_record` stamps `scanned[rel]`
    up front (the dedup that protects `stop_attempts`), so a hook killed after the
    pre-record but before the verdicts landed leaves the file content-*fresh* while
    its units sit at pending `unknown`. The scan would then skip the file forever
    and the gate could only keep blocking on units nothing would ever retry, until
    the attempt cap turned them into a residual. `pending` marks such a file for
    retry — bounded by `MAX_PENDING_VERIFY_ATTEMPTS`, after which the pending units
    reach that loud residual rather than the retry looping forever. Keyed on the
    recorded hash, so it is a pending verify *of this content*, never a blanket
    "any `unknown` unit is stale" (a genuine ESBMC-timeout `unknown` is a final
    verdict — re-verifying it every scan would never terminate).
    """
    scanned = state.get("scanned", {})
    pending = state.get("pending", {})
    stale: list[str] = []
    for abspath in files:
        digest = content_hash(abspath)
        if digest is None:
            continue
        rel = unit_id(project_dir, abspath)
        attempts = _pending_attempts(pending.get(rel), digest)
        unfinished = attempts is not None and attempts < MAX_PENDING_VERIFY_ATTEMPTS
        if scanned.get(rel) != digest or unfinished:
            stale.append(abspath)
    return stale


def pending_retry_sources(project_dir: str, state: dict) -> list[str]:
    """Absolute paths whose bytes *now on disk* have an unfinished verify on record.

    A discovery source in its own right, and the one git cannot provide (PR #148
    review). Retrying an interrupted verify only works if some scan offers the file
    to `stale_sources`, and both scans feed on `discover_changed_c_sources` — so a
    file git never reports as changed (a non-git project, a gitignored path, one
    edited back to its `HEAD` blob) was never even a candidate, and the `unknown`
    units its killed run pre-recorded could only block their way to a residual. The
    `pending` marker *is* the record of "a run started verifying exactly these bytes
    and never finished", so it names the file without git's help.

    Deliberately not narrowed by the include/exclude globs or the git scope those
    scans apply: a marker exists only for a file `verify_and_record` already ran on,
    and its half-recorded units block the Stop-gate however the file was first
    found — leaving them unretryable because a glob hides the file from *out-of-band*
    discovery is the same hole one layer over. Returns every file whose current
    content matches its marker; whether that unfinished verify still has attempts
    left stays `stale_sources`' single decision.
    """
    found: list[str] = []
    for rel, entry in state.get("pending", {}).items():
        abspath = os.path.join(project_dir, rel)
        digest = content_hash(abspath)
        if digest is not None and _pending_attempts(entry, digest) is not None:
            found.append(abspath)
    return found


def sources_needing_verify(
    project_dir: str, state: dict, discovered: Iterable[str] | None
) -> list[str]:
    """Everything a scan must (re-)verify: stale discovered C + interrupted verifies.

    The one call both the ``post_bash`` PostToolUse scan and the Stop-gate make.
    `discovered` is `discover_changed_c_sources`' result — ``None`` when this is not
    a git work tree, which disables out-of-band *discovery* but not the pending
    retries, whose files are named by the gate's own state.
    """
    files = [*(discovered or []), *pending_retry_sources(project_dir, state)]
    # Discovery joins the *git root* and the pending scan joins `project_dir`, so one
    # file can arrive under two spellings; dedup on the id both resolve to, keeping
    # the first (discovery's) spelling.
    unique: dict[str, str] = {}
    for abspath in files:
        unique.setdefault(unit_id(project_dir, abspath), abspath)
    return stale_sources(project_dir, state, unique.values())


def _baselined_blobs(baseline: object, rel: str) -> tuple[str, ...]:
    """The session-start blob hashes recorded for `rel` — empty if unusable.

    Reads the `baseline_blobs` half of the SessionStart baseline defensively: a
    hand-edited or truncated `gate_state.json` yields ``()``, which *gates* the blob
    rather than exempting it. The unusable-input direction has to be the blocking
    one here — an exemption is the only thing this map can grant.
    """
    if not isinstance(baseline, dict):
        return ()
    entry = baseline.get(rel)
    if not isinstance(entry, list):
        return ()
    return tuple(h for h in entry if isinstance(h, str))


def divergent_blob_sources(
    project_dir: str, state: dict, *, baseline_head: str | None = None
) -> list[dict]:
    """C whose STAGED or COMMITTED blob was never verified (issue #99 review).

    `stale_sources` hashes only the worktree copy, so a Bash command that stages one
    C blob and then reverts the worktree — ``git status`` reports ``MM`` yet the
    worktree hashes as the last-verified content — could commit the staged blob
    unverified; likewise a divergent blob committed while the worktree is reverted
    leaves ``HEAD`` holding C the gate never saw, still worktree-fresh. This scans the
    index (staged) and the committed-since-baseline (``HEAD``) blobs **directly** —
    not `discover_changed_c_sources`, whose existence filter would drop a
    staged/committed-then-``rm``ed path (its blob still ships) — and reports each blob
    whose SHA-256 differs from the recorded `scanned` (last-verified) hash.

    Keyed by content, so a blob equal to verified content is deduped straight out (no
    over-gating a `git add`/commit of already-verified C). A blob equal to one the
    SessionStart baseline recorded (`baseline_blobs` — the index/``HEAD`` blobs of the
    pre-session dirty tree) dedups out the same way, and stays exempt for the whole
    session: it is the user's own WIP, unchanged since session start, which is exactly
    what the baseline exists to keep out of scope (issue #139). The agent staging or
    committing *different* bytes yields a hash no baseline holds, so it still gates.
    Only the staged set (porcelain X-status) and the committed-since set are consulted,
    never every tracked file, so a merely worktree-edited-but-uncommitted unit is *not*
    dragged in by a stale ``HEAD``. Degrades to empty when not a git repo. Each entry is
    ``{"rel": <project-relative path>, "reason": "staged" | "committed"}``.
    """
    root = _git(project_dir, "rev-parse", "--show-toplevel")
    if root is None:
        return []
    root = root.strip()
    proj_real = os.path.realpath(project_dir)
    scanned = state.get("scanned", {})
    baseline = state.get("baseline_blobs", {})
    # committed first: an already-shipped divergence is the graver of the two, and
    # dedup keeps a single (rel, reason) entry per path.
    candidates = [
        (rel, f"HEAD:{rel}", "committed")
        for rel in git_committed_files_since(project_dir, baseline_head)
    ]
    candidates += [
        (rel, f":{rel}", "staged") for rel in staged_source_paths(project_dir)
    ]

    results: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for repo_rel, ref, reason in candidates:
        abspath = _in_scope_c_abspath(project_dir, root, proj_real, repo_rel)
        if abspath is None:
            continue
        rel = unit_id(project_dir, abspath)
        key = (rel, reason)
        if key in seen:
            continue
        digest = git_blob_hash(project_dir, ref)
        if digest is None:
            continue  # no blob at that ref (staged deletion / rename source) — skip
        if digest == scanned.get(rel) or digest in _baselined_blobs(baseline, rel):
            continue  # already-verified content, or the pre-session baseline
        seen.add(key)
        results.append({"rel": rel, "reason": reason})
    return results


def baseline_blob_hashes(project_dir: str) -> dict[str, list[str]] | None:
    """Session-start hashes of the INDEX and ``HEAD`` blobs of the dirty C tree.

    The blob half of the SessionStart baseline (issue #139), companion to the
    worktree hashes `baseline_scanned` seeds into `scanned`. `divergent_blob_sources`
    compares the index (``:path``) and ``HEAD`` (``HEAD:path``) blobs, which the
    worktree bytes need not equal: a repo opening at ``MM foo.c`` (or ``MD`` — staged,
    worktree deleted) holds a staged blob that `content_hash` never sees, so the Stop
    check would fire on the user's pre-existing WIP the moment the session began.

    Enumerated from `git_changed_files` — every porcelain-reported path — rather than
    `discover_changed_c_sources`, whose existence filter drops exactly the
    staged-then-deleted case this fixes. Paths run through the same
    `_in_scope_c_abspath` filter (and are keyed by the same `unit_id`) as the consumer,
    so the two sets cannot drift apart into a hole or a permanent over-gate. Refs that
    hold no blob are skipped, so an untracked path (neither ref resolves) records
    nothing and a pre-session rename records just its index blob. ``None`` when not a
    git work tree. Costs two ``git cat-file`` calls per dirty C path, once per session.
    """
    root = _git(project_dir, "rev-parse", "--show-toplevel")
    rels = git_changed_files(project_dir)
    if root is None or rels is None:
        return None
    root = root.strip()
    proj_real = os.path.realpath(project_dir)
    baseline: dict[str, list[str]] = {}
    for repo_rel in dict.fromkeys(rels):
        abspath = _in_scope_c_abspath(project_dir, root, proj_real, repo_rel)
        if abspath is None:
            continue
        digests: list[str] = []
        for ref in (f":{repo_rel}", f"HEAD:{repo_rel}"):
            digest = git_blob_hash(project_dir, ref)
            if digest is not None and digest not in digests:
                digests.append(digest)
        if digests:
            baseline[unit_id(project_dir, abspath)] = digests
    return baseline


def baseline_scanned(project_dir: str) -> int | None:
    """Mark the currently-dirty C tree as "seen" at session start (issue #99).

    The out-of-band scan gates C whose content differs from the recorded
    `scanned` hash. Without a baseline that is *everything* `git status` reports —
    including C that was dirty before the session and the agent never touched, the
    over-reach this issue was careful to avoid. Seeding `scanned` with each
    pre-session dirty file's current hash scopes the gate to "changed **since
    session start**": those files are gated only once the agent actually modifies
    them. The baseline HEAD is recorded too, so the committed-since scan can later
    catch C a Bash command writes *and* commits in one shot (issue #99 review).

    The worktree bytes are only half of it: the blob scan reads the index and ``HEAD``,
    so `baseline_blob_hashes` seeds those hashes into `baseline_blobs` in the same
    write (issue #139) — otherwise a session opening at ``MM``/``MD foo.c`` blocks
    immediately on the user's pre-existing staged blob, which `content_hash` never saw.
    Returns the number of files whose worktree content was baselined (the `scanned`
    entries), or ``None`` if not a git repo.
    """
    discovered = discover_changed_c_sources(project_dir)
    if discovered is None:
        return None
    # Both resolve outside the lock — they spawn git subprocesses. A `None` blob
    # baseline (the work tree vanished between the two calls) records nothing, which
    # over-gates pre-session staged WIP rather than exempting a blob we never hashed.
    head = git_head(project_dir)
    blobs = baseline_blob_hashes(project_dir) or {}
    with gate_lock(project_dir):
        state = load_state(project_dir)
        baseline: dict[str, str] = {}
        for abspath in discovered:
            digest = content_hash(abspath)
            if digest is not None:
                baseline[unit_id(project_dir, abspath)] = digest
        state["scanned"] = baseline
        state["baseline_blobs"] = blobs
        state["baseline_head"] = head
        save_state(project_dir, state)
    return len(baseline)


def verify_function(
    file_path: str, function: str, *, project_dir: str, k: int = DEFAULT_K
) -> UnitVerdict:
    """Run ``forseti verify`` on one function and map its JSON payload to a verdict."""
    rel = unit_id(project_dir, file_path)
    uid = f"{rel}::{function}"
    try:
        # Same build flags the enumeration parsed with, so the verify sees the
        # same translation unit the unit list was taken from. Unparseable config
        # becomes this unit's `error` verdict rather than a hook traceback — a
        # direct caller (not coming through `verify_and_record`, which fails at
        # enumeration first) must still get a verdict back.
        build_flags = _build_flags()
    except UnitsUnavailable as exc:
        return UnitVerdict(uid, rel, function, "error", k, detail=str(exc))
    argv = [
        *resolve_forseti_cmd(),
        "verify",
        file_path,
        "--function",
        function,
        "--unwind",
        str(k),
        "--timeout",
        str(int(VERIFY_TIMEOUT_S)),
        "--json",
        "--",
        *SAFETY_FLAGS,
        *build_flags,
    ]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=VERIFY_TIMEOUT_S + _SUBPROCESS_MARGIN_S,
            cwd=project_dir,
        )
    except FileNotFoundError:
        return UnitVerdict(
            uid,
            rel,
            function,
            "error",
            k,
            detail="forseti CLI not found; install the forseti package "
            "(pip install -e .) so `forseti` is on PATH",
        )
    except subprocess.TimeoutExpired:
        return UnitVerdict(
            uid,
            rel,
            function,
            "unknown",
            k,
            detail=f"verify exceeded {VERIFY_TIMEOUT_S:g}s (raise "
            "FORSETI_VERIFY_TIMEOUT_S, raise k, or simplify the unit)",
        )

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return UnitVerdict(
            uid,
            rel,
            function,
            "error",
            k,
            detail=(proc.stderr or proc.stdout).strip()[:800] or "no output",
        )

    verdict = str(payload.get("verdict", "error"))
    raw_argv = payload.get("argv")
    return UnitVerdict(
        uid,
        rel,
        function,
        verdict,
        int(payload.get("unwind", k)),
        counterexample=payload.get("counterexample"),
        detail=payload.get("reason") or payload.get("message"),
        argv=tuple(raw_argv) if isinstance(raw_argv, list) else None,
        duration_s=payload.get("duration_s"),
    )


def _gate_path(project_dir: str) -> Path:
    return Path(project_dir) / _STATE_DIR / _STATE_FILE


@contextlib.contextmanager
def gate_lock(project_dir: str) -> Iterator[None]:
    """Serialize gate-state read-modify-write across concurrent hook processes.

    Parallel PostToolUse hooks (one per edited file in a batch) each do
    load_state → mutate → save_state; without a lock the last writer wins and a
    concurrently-recorded `violated` unit can be dropped, letting the Stop-gate
    pass silently. An exclusive advisory lock on a sidecar file makes the whole
    sequence atomic between processes. POSIX-only — the platform ESBMC and this
    gate target.
    """
    path = Path(project_dir) / _STATE_DIR / _LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def load_state(project_dir: str) -> dict:
    path = _gate_path(project_dir)
    if path.exists():
        try:
            state = json.loads(path.read_text())
            state.setdefault("units", {})
            state.setdefault("stop_attempts", 0)
            state.setdefault("scanned", {})
            state.setdefault("pending", {})
            state.setdefault("baseline_blobs", {})
            state.setdefault("baseline_head", None)
            return state
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "units": {},
        "stop_attempts": 0,
        "scanned": {},
        "pending": {},
        "baseline_blobs": {},
        "baseline_head": None,
    }


def save_state(project_dir: str, state: dict) -> None:
    # Write atomically (temp + os.replace) so a hook killed mid-write can never
    # leave a truncated gate_state.json — load_state fails open to an empty unit
    # set, which would make the Stop-gate forget outstanding violations.
    path = _gate_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    os.replace(tmp, path)


def record(state: dict, verdict: UnitVerdict) -> None:
    state["units"][verdict.unit_id] = asdict(verdict)


def prune_missing_units(
    state: dict, project_dir: str, file_path: str, keep: set[str]
) -> None:
    """Drop tracked units for `file_path` whose function is not in `keep`.

    `record` only ever upserts, so a function renamed or removed as part of a fix
    would leave its stale (often `violated`) entry behind and the Stop-gate would
    block forever on a unit that no longer exists. Reconciling against the set of
    functions the file *still* defines (`keep`) — at the end of a run rather than
    by blanket-pruning up front — clears those without a mid-run hook kill being
    able to drop a still-unverified violation.
    """
    prefix = f"{unit_id(project_dir, file_path)}::"
    stale = [
        u
        for u in state["units"]
        if u.startswith(prefix) and u[len(prefix) :] not in keep
    ]
    for uid in stale:
        del state["units"][uid]


def prune_deleted_units(state: dict, project_dir: str) -> list[str]:
    """Drop recorded units whose backing C source no longer exists on disk.

    `record`/`verify_and_record` only ever upsert per file, so a C source removed
    out-of-band — `rm f.c` via Bash, whether it was committed or written earlier
    this session — leaves its `violated`/`unknown` units in the gate state and the
    Stop-gate would block forever (then only emit a residual after the attempt cap)
    on a unit whose file is gone. Discovery correctly skips a missing file *for
    verification*; this reconciles the recorded side, dropping units whose file is
    absent and clearing every record keyed on such a file: its `scanned` **and**
    `baseline_blobs` baselines (so a same-name file recreated later re-verifies from
    scratch — the two are cleared together, or content matching a dropped one could
    still slip past the blob scan) and its `pending` marker (a deleted file leaves
    nothing to retry).

    Keys off each unit's recorded `file` (project-relative), not `git status`, so
    it also catches an untracked Bash-written file git never knew existed — the
    case a git-scoped deletion scan would miss. Only ever *removes* already-recorded
    units (files the agent touched), so it can never over-reach into gating C the
    agent left alone. Returns the pruned unit ids.
    """
    units = state.get("units", {})
    scanned = state.get("scanned", {})
    pending = state.get("pending", {})
    baseline_blobs = state.get("baseline_blobs", {})
    pruned: list[str] = []
    gone_rels: set[str] = set()
    for uid, unit in list(units.items()):
        rel = unit.get("file")
        if not rel:
            continue  # cannot locate the backing file — keep it, never guess
        if not os.path.isfile(os.path.join(project_dir, rel)):
            del units[uid]
            pruned.append(uid)
            gone_rels.add(rel)
    for rel in gone_rels:
        scanned.pop(rel, None)
        pending.pop(rel, None)  # nothing left to retry — the file is gone
        if isinstance(baseline_blobs, dict):
            baseline_blobs.pop(rel, None)
    return pruned


_NON_BLOCKING_VERDICTS = frozenset({"verified", NEEDS_CONTRACT})


def blocking_units(state: dict) -> list[dict]:
    """Units the Stop-gate must block on: not `verified` and not `needs_contract`.

    `verified` passed; `needs_contract` is honestly-unverified but is not something
    a source fix can resolve (it needs a generated harness — issue #122), so it is
    reported loudly yet never blocks. Everything else (`violated` / `unknown` /
    `error`, incl. the pre-recorded pending `unknown`) blocks — preserving the
    kill-safety guarantee that a not-yet-verified unit cannot silently pass.
    """
    return [
        u
        for u in state["units"].values()
        if u.get("verdict") not in _NON_BLOCKING_VERDICTS
    ]


def needs_contract_units(state: dict) -> list[dict]:
    """Units marked `needs_contract` — reported as a loud residual, never blocking."""
    return [u for u in state["units"].values() if u.get("verdict") == NEEDS_CONTRACT]


def _needs_contract_verdict(rel: str, function: str, k: int) -> UnitVerdict:
    """The `NEEDS_CONTRACT` verdict for a pointer/array-taking unit (no ESBMC run)."""
    return UnitVerdict(
        f"{rel}::{function}",
        rel,
        function,
        NEEDS_CONTRACT,
        k,
        detail=_NEEDS_CONTRACT_DETAIL,
    )


def verify_and_record(
    file_path: str, *, project_dir: str, k: int = DEFAULT_K
) -> list[UnitVerdict]:
    """Verify each function in `file_path`, persisting every verdict as it lands.

    Kill-safety: the hook has a wall-clock timeout, and verifying a file with
    several functions can exceed it. Up front (under the lock) every current
    function is reconciled and pre-recorded as `unknown`; then each real verdict
    overwrites its entry the moment it lands. So a hook kill at any point leaves
    the not-yet-verified functions as `unknown` — which the Stop-gate blocks on —
    rather than absent; it can never drop an already-found or still-pending
    violation and pass silently. The same up-front block marks the file's scan
    unfinished in `pending`, cleared only once every verdict is final — and only by
    the run that recorded that marker, never by one that finds a concurrent run's —
    so a killed run is *retried* by the next scan instead of being skipped as
    content-fresh (issue #140), bounded by `MAX_PENDING_VERIFY_ATTEMPTS`.

    None of that happens when the bytes this run enumerated have already been
    superseded on disk by a concurrent run whose `scanned` stamp vouches for what
    is there now: that run owns the file, so this one returns **no verdicts and
    writes nothing at all** — no stamp, no `pending` marker, no reconcile, not even
    a `stop_attempts` reset. An empty return is therefore not by itself "the file
    defines no functions"; see the deferral below for why blocking instead would
    strand a `rel::?` nothing can clear.

    The same deference applies wherever else this run would publish a block: an
    enumeration that failed for bytes now gone, and a post-verify drift check that
    has just withdrawn this run's own stamp. Each is re-decided atomically with the
    record it would write (`unless_superseded`) rather than by whatever the state
    said when the lock was last held.

    One more path returns nothing without deferring to anyone: a run whose content
    drifted away and whose stamp is *not* its own to withdraw — whether a
    concurrent run replaced it or nothing holds one at all. Its verdicts describe
    bytes that are no longer on disk, and the returned list is what the hooks
    report and act on, so it says nothing. Safety there is not the return value's:
    the per-unit writes are ownership-scoped, so an un-overwritten unit is still
    the owner's pending `unknown`, and with no stamp the file reads stale and is
    re-offered.
    """
    rel = unit_id(project_dir, file_path)

    def _blocking_error(
        detail: str, *, digest: str | None = None, unless_superseded: bool = False
    ) -> list[UnitVerdict]:
        """Record a blocking `error` verdict for the whole file and return it.

        No caller stamps `scanned`: a file we could not read or enumerate must not
        be recorded as already-scanned, or the out-of-band scan would treat it as
        handled and the edit would pass unverified.

        `unless_superseded` drops the *verdict* — never the charge below — when a
        `scanned` stamp vouches for the bytes on disk at that instant. Only two
        things write a stamp: this function, in the same lock as the units it
        pre-records as blocking and its `pending` claim, and the session baseline's
        deliberate "already handled". So a stamp equal to what is on disk means
        some run has gated exactly that content, and this run's failure to
        enumerate it adds nothing — while the `rel::?` it would record cannot be
        cleared: the file hashes equal to the stamp, so once the owning run
        finishes and drops its claim, `stale_sources` reads the file as fresh, the
        reconcile that prunes `?` never runs, and the Stop-gate blocks its way to a
        loud residual on a file that was legitimately verified. The condition is
        tested *here*, not by the caller: outside the lock it would only move the
        gap to between the test and this one.

        What still blocks in the meantime is the owning run's own record — its
        pre-recorded `unknown` units if it was killed, its real verdicts if it
        finished. That is why deferring is not a silent pass, and it is what makes
        the *charge* independent: a killed run's claim keeps its file a retry
        candidate, so a persistently-erroring file has to spend that budget or
        re-verify forever (issue #140/#148). Suppressing the verdict without
        charging would spin; charging without suppressing is the stranded `?`. So
        this path does both — quiet, but one attempt poorer.

        A stamp for *other* content is not superseding: the file reads stale, the
        `?` is clearable by the next scan, and nobody has gated what is on disk —
        that block must land.

        An unfinished-verify marker for `digest` is *spent one attempt*, never
        deleted (PR #148 review). Deleting is what the ownership rule forbids: this
        path can never own the marker — an error caller either returns before the
        block that creates one or (the post-verify drift caller) has already
        released its own claim, and `pending` holds a single claim per file — so the
        marker it would drop always belongs to another run, whose pre-recorded `unknown`
        units would then sit content-fresh (that run stamped `scanned` with these
        same bytes) with nothing left to retry, the exact hole issue #140 closes.
        Bumping keeps that retry claim alive while still making progress: leaving the
        counter untouched would let a persistently-erroring file re-verify on every
        scan forever — each error resets `stop_attempts` — instead of going quiet at
        `MAX_PENDING_VERIFY_ATTEMPTS` and blocking its way to the loud residual. The
        bump goes through `_pending_attempts`, so a corrupt counter normalizes to 1
        rather than freezing, and stops at the cap so a marker no scan will retry
        again cannot count up forever. `hash`/`pid` are left as the creating run
        wrote them: this run owns nothing and will never clear the marker — and
        because those two fields alone are `_pending_owner`'s identity, the charge
        leaves the creating run still able to clear it.

        Only a marker recording exactly `digest` is charged: a concurrent run of
        *other* content is verifying bytes this error says nothing about.

        Three callers pass no `digest` and so touch nothing. The read failure never
        learned which bytes it was scanning, so it cannot name the claim to charge —
        and such a file also fails `content_hash`, so `stale_sources` skips it
        entirely and no frozen counter can spin. The other two are the drift checks,
        around the enumeration and around the verify loop, and they share a reason:
        each fires precisely *because* the file no longer hashes to `digest`, so
        charging that content's retry budget would spend a claim on bytes this run is
        no longer speaking for. The counter is bumped by the next run that scans
        whatever the file now holds.
        """
        verdict = UnitVerdict(f"{rel}::?", rel, "?", "error", k, detail=detail)
        with gate_lock(project_dir):
            state = load_state(project_dir)
            on_disk_now = content_hash(file_path)
            superseded = unless_superseded and (
                on_disk_now is not None
                and state.get("scanned", {}).get(rel) == on_disk_now
            )
            pending = state.setdefault("pending", {})
            attempts = _pending_attempts(pending.get(rel), digest) if digest else None
            if superseded and attempts is None:
                return []  # nothing to record, and no claim to charge
            if not superseded:
                record(state, verdict)
                state["stop_attempts"] = 0
            if attempts is not None:
                pending[rel]["attempts"] = min(
                    attempts + 1, MAX_PENDING_VERIFY_ATTEMPTS
                )
            save_state(project_dir, state)
        return [] if superseded else [verdict]

    try:
        raw = Path(file_path).read_bytes()
    except OSError as exc:
        return _blocking_error(str(exc))

    # Hash the bytes we read before anything else can fail: every path below needs
    # to name the content it operated on, both to stamp `scanned` and to tell its
    # own unfinished-verify marker from a concurrent run's.
    digest = hashlib.sha256(raw).hexdigest()

    try:
        # `content=raw` — enumerate a snapshot of the very bytes we are about to
        # hash, never a re-read of the file. Without it `list-units` reads the
        # path again and can land on a transient rewrite: an *empty* `.c`
        # enumerates as a successful empty list (exit 0), so the zero-byte
        # instant a `> f.c`/heredoc passes through would prune every unit this
        # file has — dropping an already-recorded blocking verdict — and then
        # stamp `scanned` with the final content's digest, leaving the Stop-gate
        # and the out-of-band scan both satisfied (issue #141).
        defs = extract_function_defs(file_path, project_dir=project_dir, content=raw)
    except UnitsUnavailable as exc:
        # Couldn't enumerate the file's units (esbmc missing, C parse error, a
        # snapshot that would not stage, …). Record a blocking `error` verdict
        # rather than skip: a file that was edited but can't be parsed must not
        # pass silently. Unless a stamp already vouches for what is on disk — then
        # those bytes are gated by a run that *did* enumerate them, and a block
        # here would be an unclearable one on top of a verified file, whether the
        # bytes moved on or this is a no-op edit of content already verified.
        return _blocking_error(str(exc)[:800], digest=digest, unless_superseded=True)

    # Acquiring the stamp is a claim to be authoritative for this file, so the
    # check that we *are* happens under the same lock that writes it — a
    # concurrent hook can stamp in any gap left between the two. The snapshot
    # guarantees we enumerated `raw`; the re-hash guarantees `raw` is still what
    # the file holds at the instant we claim it. Together they give the stamping
    # invariant: if `scanned[rel]` was set to H, the units recorded alongside it
    # were enumerated from content hashing to H *and* the file still hashed to H
    # when the stamp was taken. A rewrite that lands and stays (A → B) fails
    # closed here rather than relying on the out-of-band scan to re-gate B later —
    # which it cannot do at all outside a git work tree. Compared by content, not
    # by `stat` metadata, so it holds on a filesystem with coarse timestamp
    # granularity too. The block clears on the next edit's reconcile.
    #
    # It is also the ownership test. Hashing equal means the bytes we enumerated
    # are the bytes on disk *now*, so re-stamping over a concurrent run's entry is
    # correct — that run's content has been superseded. Hashing unequal means it
    # has superseded ours, and the reconcile below would then prune and pre-record
    # against content nobody has on disk.
    marker: dict[str, object] = {}  # only ever read on the path that fills it
    with gate_lock(project_dir):
        state = load_state(project_dir)
        on_disk = content_hash(file_path)
        if on_disk == digest:
            # Reconcile + record every current function BEFORE the slow verifies:
            # drop functions the file no longer defines, reset the Stop-gate's
            # patience, and pre-record each — a pointer/array-taking unit as its
            # final `needs_contract` (we skip its meaningless function-level
            # verify), every other as pending `unknown` so a mid-run kill leaves
            # the not-yet-verified ones blocking rather than absent.
            state["stop_attempts"] = 0
            # Stamp the content hash so a later out-of-band scan treats this exact
            # content as already verified — that dedup is what keeps the Stop-gate
            # from re-blocking (and resetting its patience) on a file nothing has
            # touched since.
            state.setdefault("scanned", {})[rel] = digest
            # Mark this content's scan unfinished, counting the start. The stamp
            # above makes the file content-fresh, so without this marker a kill
            # before the verdicts land would leave the pending `unknown` units
            # unretryable (issue #140). Cleared once every verdict is final.
            #
            # The marker doubles as this run's ownership token so the cleanup below
            # can tell it from one a *concurrent* run stored — see `_pending_owner`
            # for what identifies it. The counter is bumped under the lock, so a
            # concurrent run of the same content still reads this start when it
            # computes its own.
            prior = _pending_attempts(state.setdefault("pending", {}).get(rel), digest)
            marker = {"hash": digest, "attempts": (prior or 0) + 1, "pid": os.getpid()}
            state["pending"][rel] = marker
            prune_missing_units(state, project_dir, file_path, {d.name for d in defs})
            for d in defs:
                if d.takes_pointer:
                    record(state, _needs_contract_verdict(rel, d.name, k))
                else:
                    record(
                        state,
                        UnitVerdict(
                            f"{rel}::{d.name}",
                            rel,
                            d.name,
                            "unknown",
                            k,
                            detail="verification pending",
                        ),
                    )
            save_state(project_dir, state)

    if on_disk != digest:
        # Someone else's content is on disk. If a stamp already vouches for
        # exactly that content, a concurrent run owns this file: it enumerated
        # what is there and pre-recorded every unit as blocking until it verifies
        # them, so defer to it — silently. Blocking anyway would strand a `rel::?`
        # error nothing can clear, the same trap the post-verify withdrawal below
        # avoids: the file hashes equal to the surviving stamp, so the out-of-band
        # scan reads it as fresh and never re-runs the reconcile that prunes `?`.
        # With nothing vouching for it, nobody has gated what is on disk — block.
        #
        # Both halves of that test are `_blocking_error`'s, re-read under the lock
        # that records the verdict. Deciding it out here — off the `on_disk` read
        # from the stamp lock, now released — would leave a concurrent run room to
        # stamp in between and strand the `?`.
        return _blocking_error(
            "source changed while its units were being enumerated; not recording "
            "a scan of content that was not enumerated — re-edit to re-verify",
            unless_superseded=True,
        )

    verdicts: list[UnitVerdict] = []
    for d in defs:
        if d.takes_pointer:
            # Signature-based, a priori: skip the (meaningless) function-level
            # verify — no ESBMC call — and report NEEDS_CONTRACT. Classifying by
            # signature, never by matching "dereference failure" in a cex, keeps a
            # genuine out-of-bounds bug (same string) from being suppressed.
            verdicts.append(_needs_contract_verdict(rel, d.name, k))
            continue
        verdict = verify_function(file_path, d.name, project_dir=project_dir, k=k)
        verdicts.append(verdict)
        with gate_lock(project_dir):  # overwrite the pending entry
            state = load_state(project_dir)
            # Ownership-scoped, exactly like the stamp withdrawal below: a
            # concurrent hook that has re-stamped `scanned` for this file owns
            # its units too — it re-enumerated and pre-recorded them. This run's
            # verdict describes content that hook has already superseded, so
            # writing it could replace that run's fresh `violated` with a stale
            # `verified`, after which the file hashes fresh and nothing blocks.
            # Dropping it is safe in the other direction: the owner pre-records
            # every unit as pending `unknown`, so an un-overwritten unit still
            # blocks. (The stale verdict stays in the returned list — the
            # PostToolUse message — but the Stop-gate reads state, not this.)
            if state.get("scanned", {}).get(rel) == digest:
                record(state, verdict)
                save_state(project_dir, state)

    # The verifies read the *real* path, not the snapshot: a verdict has to
    # describe the translation unit that actually ships, and the snapshot's
    # mirrored neighbourhood reproduces in-place include resolution only up to
    # its root — an include reaching above that fails to resolve, which is a
    # tolerable blocking `error` for an enumeration and an intolerable one for
    # the verify the whole gate rests on. Plus every counterexample and the
    # trace's `argv` would name a temp file that no longer exists. So this
    # boundary is guarded, not eliminated: re-hash once after the
    # loop and fail closed on drift. Un-stamping is what makes the out-of-band
    # scan re-gate the file, and the `error` blocks when there is no scan to fall
    # back on (outside a git work tree); both clear on the next edit's reconcile.
    #
    # What this catches: a rewrite that lands and *stays* (A → B) during the
    # verifies. What it does NOT catch — the acknowledged residual — is a
    # transient A → B → A: a verdict computed against B stays attached to A's
    # stamp, because the final bytes compare equal. Detecting that needs
    # verification of immutable content, which costs more than it buys (above).
    # So the invariant is precisely: if `scanned[rel]` is H, the units were
    # enumerated from content hashing to H and the file hashed to H both then and
    # after the loop — NOT that every verdict was computed against H.
    withdrawn = False
    drifted = content_hash(file_path) != digest
    if drifted:
        with gate_lock(project_dir):
            state = load_state(project_dir)
            scanned = state.setdefault("scanned", {})
            # Ownership-scoped, and the *whole* response hinges on it. A stamp
            # that is still ours means nothing else vouches for this file, so
            # withdraw it (that is what makes the out-of-band scan re-gate) and
            # block. A stamp some concurrent hook has replaced belongs to that
            # run: it stamped content it is itself reconciling, so leave it be
            # AND do not block. Blocking anyway would strand a `rel::?` error
            # that nothing can clear — the file now hashes equal to the surviving
            # stamp, so the out-of-band scan reads it as fresh and never re-runs
            # the reconcile that prunes `?`, and the Stop-gate would block on a
            # file a newer run legitimately verified.
            withdrawn = scanned.get(rel) == digest
            if withdrawn:
                scanned.pop(rel, None)
                save_state(project_dir, state)

    # Every verdict for this content is final — or, on the drift path just above,
    # this run has concluded that none of them can be trusted. Either way the run
    # *finished*: clear THIS RUN's unfinished-verify marker so later scans trust
    # the up-front `scanned` stamp again. Only a kill should leave a claim behind,
    # because a claim is precisely what makes the next scan retry (issue #140).
    #
    # Only this run's — the gate explicitly supports concurrent PostToolUse hooks
    # on the same path across successive edits, so an older run can reach this
    # point after a newer one has stamped `scanned` with *its* digest and stored
    # its own marker. Popping that newer marker would leave the file hashing fresh
    # with nothing to retry, so a kill of the newer run would never be re-verified
    # — the exact hole this closes (PR #148 review) — and its pre-recorded
    # `unknown` units, which this run's verdicts for the older bytes overwrote,
    # would never be re-decided either. A kill in the sliver between the last
    # verdict and this write just costs one redundant re-verify — the safe
    # direction. Ownership is `_pending_owner`, not whole-marker equality: a
    # concurrent `_blocking_error` may have charged this marker an attempt, which
    # changes its bytes without changing whose claim it is.
    with gate_lock(project_dir):
        state = load_state(project_dir)
        pending = state.setdefault("pending", {})
        if _pending_owner(pending.get(rel)) == _pending_owner(marker):
            del pending[rel]
        save_state(project_dir, state)

    # Blocking comes *after* releasing the claim, which is what keeps
    # `_blocking_error`'s no-ownership rule true for this caller too: by the time
    # it looks, any marker under `rel` belongs to a concurrent run. (It is passed
    # no `digest` regardless — the bytes it would name are no longer the ones on
    # disk, so charging their retry budget would spend a claim on content this
    # run is no longer speaking for.)
    #
    # The withdrawal above released the lock, so a concurrent hook can stamp and
    # fully verify what is on disk before this block lands — after which a
    # `rel::?` would be unclearable. `unless_superseded` re-tests ownership under
    # the same lock that records the verdict; a test out here would only shrink
    # that window, not close it.
    if withdrawn:
        return _blocking_error(
            "source changed while its units were being verified; the verdicts "
            "describe content that is no longer on disk — re-edit to re-verify",
            unless_superseded=True,
        )
    if drifted:
        # Not ours to withdraw, so nothing is written — and nothing is *said*
        # either. The returned list is the PostToolUse message, and the hooks act
        # on it: a stale `violated` exits 2 and hands Claude a counterexample for
        # code that is no longer on disk, a stale `verified` is logged and shown as
        # a pass for content this run never saw. The verdicts stay out of the
        # state by the same ownership test in the loop above, so the honest answer
        # here is the same one every other supersession gives — say nothing, and
        # let the run that owns the file speak.
        return []
    return verdicts
