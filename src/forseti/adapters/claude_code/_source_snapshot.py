"""Intent-specific source snapshots for enumeration and verification.

The two operations keep their placement, byte, metadata, git protection and
cleanup policies together. One private lifetime owns the temporary descriptor
and path; callers receive only a fully prepared source path.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile
from collections.abc import Generator
from pathlib import Path
from typing import Literal

from ._errors import UnitsUnavailable
from .git import _git, _git_probe_env

# The enumeration snapshot's filename prefix (issue #151). `_untracked_snapshot`
# rejects a path whose basename starts with it *and* that git cannot show as
# tracked — ahead of `FORSETI_GATE_EXCLUDE`/`FORSETI_GATE_INCLUDE` (a project can
# replace, not extend, that env var), a snapshot a killed hook could not clean up
# must never be offered back to `verify_and_record` as a source in its own right.
# `.` leads so `is_c_source`'s suffix check (`.c`/`.C` — see `enumeration_source`)
# still lets it through the discovery scans as a dotfile, which is exactly what
# that exclusion exists to catch.
_ENUM_SNAPSHOT_PREFIX = ".forseti-units-"

# The verify snapshot's filename prefix (issue #150). `_untracked_snapshot`
# rejects a path whose basename starts with it *and* that git cannot show as
# tracked — ahead of `FORSETI_GATE_EXCLUDE`/`FORSETI_GATE_INCLUDE` (a project can
# replace, not extend, that env var), a snapshot a killed hook could not clean up
# must never be offered back to `verify_and_record` as a source in its own right.
# `.` leads so `is_c_source`'s suffix check (always true here — the snapshot's
# suffix always case-insensitively matches `file_path`'s own, see
# `verification_source`) still lets it through the discovery scans as a dotfile,
# which is exactly what that exclusion exists to catch.
_VERIFY_SNAPSHOT_PREFIX = ".forseti-verify-"
_UTF8_BOM = b"\xef\xbb\xbf"


def _kernel_dir(path: str) -> str:
    """`path` with each ``..`` resolved the way the kernel walks it, nothing else.

    `os.path.abspath` collapses ``a/link/..`` to ``a`` lexically, but the kernel
    resolves ``link`` before it climbs, landing on the parent of the link's
    *target*. Since the gate reads and verifies through that same kernel walk, a
    snapshot staged at the collapsed path would stand in for a different file's
    neighbourhood — or a different file altogether. `tempfile.mkstemp` cannot be
    trusted to do this on its own: it normalizes its `dir` argument with
    `os.path.abspath` before opening anything, so handing it a raw
    ``proj/link/..`` reproduces exactly the lexical mistake this function exists
    to avoid (measured, not assumed — see `enumeration_source`).

    Only ``..`` forces a resolution. A symlinked component *not* followed by one
    stays spelled and is returned unresolved: staging directly at that spelled
    path is itself a real filesystem write, and the kernel resolves it exactly
    as it would resolve the real file's own `open()` — nothing left for this
    function to do. Paths without ``..`` come back untouched.
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


def _index_ignore_snapshot(start_dir: str, prefix: str) -> bool:
    """Register `prefix*` in `start_dir`'s own ``.git/info/exclude``, idempotently.

    `enumeration_source` calls this *before* staging a snapshot, so a
    concurrent `git add -A`/`git status` never has a window in which the
    snapshot exists but the *pattern* is not yet registered. (Registering the
    pattern is not by itself proof of protection — see `enumeration_source`
    for the real-path `check-ignore` verification that catches a
    higher-precedence tracked ``.gitignore`` overriding it.) ``.git/info/exclude``
    — not the tracked `.gitignore` — is the right place: it is local,
    un-versioned repo state, exactly like the snapshot itself, so no tracked
    file is touched and nothing needs to be undone. `start_dir` must be the
    directory the snapshot is actually staged in, not necessarily the Claude
    project root: `git rev-parse` resolves upward from `start_dir` to
    whichever repository actually contains it, which may differ from the
    project root's repository (a submodule, a sibling checkout reached via a
    symlink).

    Returns whether `start_dir` is inside a git work tree at all — the
    caller needs this to know whether the follow-up `check-ignore`
    verification even applies. Genuinely outside a work tree this returns
    `False`, but a non-zero exit alone does not mean that: `git rev-parse`
    exits the same 128 for a real repository-level error inside a directory
    an *ancestor* repository still tracks normally — a malformed nested
    ``.git`` gitfile (``fatal: invalid gitfile format``), a stale linked
    worktree (``fatal: not a git repository: (null)``), a bad ``GIT_DIR``
    (``fatal: not a git repository: '<path>'``) — and the parent's own
    `git add -A` still stages this directory's contents in every one of
    those cases (measured, not assumed). A fourth non-genuine case is
    environmental rather than repository-level: `GIT_CEILING_DIRECTORIES` set
    in the ambient environment can stop `git rev-parse` from discovering an
    ancestor repository that is still very much there and still tracks
    `start_dir` normally — `_git_probe_env` strips it (alongside forcing
    `LC_ALL=C`) so this probe answers "does a repository genuinely exist
    anywhere in the ancestry", not "what does the caller's ambient discovery
    boundary happen to allow". Only git's own affirmative "no repository
    anywhere in the ancestry" diagnostic is trusted: both phrasings it uses
    (``not a git repository (or any parent up to mount point`` /
    ``...(or any of the parent directories)``) share the ``not a git
    repository (or any`` anchor, which the four false-positive cases above do
    not. Every other non-zero exit — an unrecognized message,
    the anchor absent — raises `UnitsUnavailable` instead of assuming safety,
    matching the gate's convention (`_untracked_snapshot`) that an
    indeterminate git query must never relax a guard. `git rev-parse` failing
    to run at all (the binary missing, a timeout, some other subprocess-level
    error) is the same kind of indeterminate answer and fails closed the same
    way (review feedback, issue #151).

    Writing the exclude entry fails the same closed way: an `OSError` —
    including one from creating a missing ``.git/info/`` (legal for a repo
    made with an empty template, or by non-git tooling) — raises
    `UnitsUnavailable` rather than silently proceeding to stage the snapshot
    unprotected.

    Read and written as bytes, never decoded: git treats exclude patterns as
    raw bytes, and a non-UTF-8 byte sequence already in the file (e.g. a
    non-ASCII path pattern written under a non-UTF-8 locale) is legal there.
    Decoding it as text would raise `UnicodeDecodeError` — a `ValueError`, not
    an `OSError` — which would escape the `except` below uncaught and crash
    the hook process outright instead of failing closed.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", start_dir, "rev-parse", "--git-path", "info/exclude"],
            capture_output=True,
            text=True,
            timeout=30,
            env=_git_probe_env(),
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError) as exc:
        raise UnitsUnavailable(
            f"could not determine whether {start_dir} is a git work tree "
            f"(git rev-parse failed to run: {exc}); refusing to stage the "
            f"snapshot unprotected"
        ) from exc
    if proc.returncode != 0:
        if "not a git repository (or any" in proc.stderr:
            return False  # git affirmatively confirmed this is not a work tree
        raise UnitsUnavailable(
            f"could not determine whether {start_dir} is a git work tree "
            f"(git rev-parse failed: {proc.stderr.strip()}); refusing to "
            f"stage the snapshot unprotected"
        )
    exclude_rel = proc.stdout.strip()
    exclude_path = (
        exclude_rel
        if os.path.isabs(exclude_rel)
        else os.path.join(start_dir, exclude_rel)
    )
    pattern = f"{prefix}*"
    pattern_bytes = pattern.encode("utf-8")
    try:
        os.makedirs(os.path.dirname(exclude_path), exist_ok=True)
        with open(exclude_path, "a+b") as handle:
            handle.seek(0)
            existing = handle.read()
            if pattern_bytes not in existing.splitlines():
                if existing and not existing.endswith(b"\n"):
                    handle.write(b"\n")
                handle.write(pattern_bytes + b"\n")
    except OSError as exc:
        raise UnitsUnavailable(
            f"could not register {pattern!r} in {exclude_path} to keep the "
            f"enumeration snapshot out of the git index: {exc}"
        ) from exc
    return True


@contextlib.contextmanager
def _staged_source(
    file_path: str,
    *,
    directory: str,
    purpose: Literal["enumeration", "verification"],
    chunks: tuple[bytes, ...],
    check_ignored: bool = False,
    mtime_ns: int | None = None,
) -> Generator[str]:
    """Own a snapshot from allocation through cleanup, preserving primary errors.

    Only staging operations translate OSError: consumer errors at the yield
    cross this seam unchanged. Enumeration requires cleanup after success;
    verification cleanup and cleanup during any failure remain best effort.
    """
    enumeration = purpose == "enumeration"
    prefix = _ENUM_SNAPSHOT_PREFIX if enumeration else _VERIFY_SNAPSHOT_PREFIX
    stage_error = (
        f"could not stage a snapshot of {file_path} for enumeration (check "
        f"free space and write access to {directory})"
        if enumeration
        else f"could not stage a verify snapshot beside {file_path} (check free "
        f"space and write access to {directory})"
    )
    try:
        fd, path = tempfile.mkstemp(
            prefix=prefix, suffix=Path(file_path).suffix, dir=directory
        )
    except OSError as exc:
        raise UnitsUnavailable(f"{stage_error}: {exc}") from exc
    try:
        try:
            try:
                handle = os.fdopen(fd, "wb")
            except BaseException:
                # fdopen owns fd only after it returns a handle. Even an
                # interrupt during that transfer must not leak the descriptor.
                with contextlib.suppress(OSError):
                    os.close(fd)
                raise
            with handle:
                # Registration alone is insufficient: a tracked .gitignore
                # negation can override it. Check the actual empty snapshot
                # before exposing any source bytes in the user's work tree.
                if (
                    check_ignored
                    and _git(
                        directory,
                        "check-ignore",
                        "-q",
                        "--no-index",
                        "--",
                        path,
                        env=_git_probe_env(),
                    )
                    is None
                ):
                    raise UnitsUnavailable(
                        f"{path} is registered in {directory}'s .git/info/exclude "
                        "but a "
                        f"tracked .gitignore rule appears to override it — refusing to "
                        f"leave an unprotected snapshot staged"
                    )
                for chunk in chunks:
                    handle.write(chunk)
            if mtime_ns is not None:
                os.utime(path, ns=(mtime_ns, mtime_ns))
        except OSError as exc:
            raise UnitsUnavailable(f"{stage_error}: {exc}") from exc
        yield path
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(path)
        raise
    else:
        try:
            os.unlink(path)
        except OSError as exc:
            if enumeration:
                raise UnitsUnavailable(
                    f"enumerated {file_path} but could not remove the snapshot left "
                    f"at {path} (check write access to {directory}): {exc}"
                ) from exc


@contextlib.contextmanager
def enumeration_source(
    file_path: str | os.PathLike[str], content: bytes | None, *, project_dir: str
) -> Generator[str]:
    """Yield the path to enumerate for `file_path` — itself, or an immutable sibling.

    With `content` ``None`` the file is enumerated where it lies. Given `content`,
    those exact bytes are written to a snapshot and *that* is what gets parsed —
    so the enumeration is of the caller's bytes by construction, not of whatever
    the file happens to hold when the CLI re-reads it (issue #141). Write the
    bytes; never `shutil.copy` or re-read `file_path`, or the race walks straight
    back in.

    Staged as a **sibling of `file_path`, in its own real directory** — not a
    tree mirrored elsewhere, which is what this replaced (issue #151). clang
    resolves a quoted ``#include`` against the directory of the file it is
    reading, and a same-directory snapshot *is* that directory: every real
    sibling, and everything a ``..`` chain can reach, resolves exactly as the
    in-place parse would — nothing to reproduce, and no boundary to fall off.
    The mirrored design this replaces had to stop somewhere short of the
    filesystem root (walking to it would mean a `scandir` of ``$HOME`` on every
    edit) and so left a residual: a quoted include climbing above that boundary
    missed the mirror and fell through to the ``-I`` search with the *spelled*
    path, silently landing on a different translation unit. Nothing needs
    mirroring now, so that boundary — and every helper that built and walked the
    synthetic tree it required — is gone with it. The old
    ``test_include_above_the_mirror_root_is_a_known_residual`` pinned the miss;
    ``test_include_above_the_project_root_is_no_longer_a_residual`` is its
    replacement, since there is no boundary left to climb past.

    The trade is a narrower residual in its place: this snapshot cannot occupy
    `file_path`'s own name (`tempfile.mkstemp` gives it a random one, so two
    concurrent enumerations of the same file cannot collide with each other
    either), so a translation unit that ``#include``s the source under any name
    that resolves to its own inode — its literal filename, a same-directory
    symlink, or a hard link — still reaches the live file for that nested read,
    not the frozen snapshot. Pinned by
    ``test_a_source_alias_leads_back_into_the_live_file_is_a_known_residual``
    and, one level deeper and unaffected by this change either way (a nested
    alias below a subdirectory was never protected here),
    ``test_a_nested_source_alias_is_a_known_residual``.

    The source's own path is derived **as spelled** — joined onto `project_dir`
    (the subprocess's cwd) but never `resolve()`d — and the snapshot is staged
    beside it. clang searches the directory of the path it was *given*, so for a
    symlinked source file resolving would mirror the link target's directory
    instead: a header beside the link would go missing, and a same-named header
    beside the target would be silently preferred.

    One part of it is not spelled: a ``..`` in the given path is resolved the
    way the kernel walks it (`_kernel_dir`), never left to
    `tempfile.mkstemp`'s own lexical `os.path.abspath` normalization — see there
    for why the difference is real, not academic. The kernel is what picked the
    file whose bytes were hashed and what will pick it again for the verify, so
    collapsing ``proj/link/../x.c`` lexically to ``proj/x.c`` would stage the
    snapshot beside the *project's* headers instead of the link target's — a
    different translation unit, which is the failure this staging exists to
    prevent. A symlinked component not followed by a ``..`` stays spelled, since
    staging directly at that spelled path is itself a real filesystem write and
    the kernel resolves it correctly without any help from this function.

    An explicit include root piped to ``forseti list-units`` on stdin — no
    staging at all — looks like a cleaner fix than either design and was
    considered (issue #151). It is not buildable against ESBMC 8.3.0: there is
    no stdin convention (``esbmc - --parse-tree-only`` fails with "failed to
    figure out type of file -", measured, not assumed) and no flag that
    separates a quoted include's search base from the file's own location
    (``-iquote`` is rejected; only ``-I``/``--idirafter`` are exposed). A real
    file beside the source is the only way left to make ESBMC resolve
    ``#include``s from the place the source actually lives.

    The name carries `_ENUM_SNAPSHOT_PREFIX` so `_untracked_snapshot` excludes it
    from the gate's own discovery for as long as it is untracked — see there for
    why a *tracked* file sharing the prefix must not be exempted the same way.
    That guard only keeps the snapshot out of the *gate's own* scans, though;
    nothing about it stops a concurrent `git add -A`, IDE, or automation job from
    staging the file while it exists on disk.
    `_index_ignore_snapshot` registers `_ENUM_SNAPSHOT_PREFIX*` in `src_dir`'s own
    `.git/info/exclude` *before* `mkstemp` runs (below), so the pattern is always
    in place — inside a git work tree, `git status`/`git add -A` never see a
    matching name, so a `finally`-block failure to clean up (or this process
    being killed first) cannot be committed by an unrelated `git add -A` in
    the meantime. It targets `src_dir`, not `project_dir`, because that is
    where `mkstemp` actually stages the file: when the source lives in a
    different repository than `project_dir` (a submodule, a sibling checkout
    reached via a symlink), `project_dir`'s exclude file is the wrong one to
    write.

    Registering the pattern is not by itself proof it protects anything:
    gitignore(5) gives a repository's own tracked ``.gitignore`` files higher
    precedence than ``$GIT_DIR/info/exclude``, so a project that re-includes
    dotfiles (``!.*``), whitelists ``.c`` sources (``*`` then ``!*.c``), or
    even specifically targets `mkstemp`'s own generated shape (an 8-``?``
    glob negation) can silently override this exclude entry — measured
    empirically: `git check-ignore -v` names the
    `.gitignore` rule as the decider even with the exclude pattern already
    present, and `git add -A` then stages the "excluded" snapshot. Guessing at
    `mkstemp`'s random-name shape to build a synthetic probe was tried and
    rejected: the length is a private, undocumented `tempfile` implementation
    detail, and a probe of the wrong shape can itself be fooled by a rule
    targeting the real shape. So the check below runs against the *real*
    snapshot path immediately after `mkstemp` creates it — no guessing — and
    unlinks it and fails closed as `UnitsUnavailable` if `git check-ignore`
    cannot confirm it is actually excluded (review feedback, issue #151). That
    leaves a narrow, already-accepted residual: an explicit, forced
    `git add -f <path>` is the one thing left that can still stage it (the
    same residual `verification_source` documents at the verify boundary,
    issue #150), and a concurrent `git add -A` racing the few instructions
    between `mkstemp` and this check completing — negligible next to the
    whole-enumeration-timeout window that existed before any of this staging
    existed.
    """

    if content is None:
        yield str(file_path)
        return
    spelled = os.path.join(project_dir, os.fspath(file_path))
    src_dir = _kernel_dir(os.path.dirname(spelled))
    is_work_tree = _index_ignore_snapshot(src_dir, _ENUM_SNAPSHOT_PREFIX)
    with _staged_source(
        os.fspath(file_path),
        directory=src_dir,
        purpose="enumeration",
        chunks=(content,),
        check_ignored=is_work_tree,
    ) as path:
        yield path


def _line_directive(path: str) -> bytes:
    r"""A `#line 1 "path"` directive, escaped as a single C string literal.

    Backslashes, double quotes, and newlines are the characters that would
    otherwise end the literal early or corrupt it (a Windows-style path, a
    `"` in a filename, or a `\n`/`\r`, all of which are legal in a POSIX
    path); everything else passes through unescaped, same as a C string
    literal allows.
    """
    escaped = (
        path.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
    return f'#line 1 "{escaped}"\n'.encode()


@contextlib.contextmanager
def verification_source(
    file_path: str | os.PathLike[str],
    content: bytes,
    *,
    project_dir: str,
    mtime_ns: int,
) -> Generator[str]:
    """Yield the path to verify `file_path`'s content against: an immutable sibling.

    One boundary over from `enumeration_source` (issue #150): the *verify* step
    reads the real path today, guarded only by a re-hash after the loop
    (`verify_and_record`) — which a transient A -> B -> A rewrite slips past,
    because the bytes it verified and the bytes it finds afterward compare
    equal. Writing `content` to a private snapshot and verifying THAT instead
    means every verdict is computed against content hashing to the digest this
    run stamped, full stop — there is no "afterward" for the verify itself to
    race.

    Staged as a **sibling of `file_path`, in its own real directory** — not a
    mirrored copy elsewhere, which is what `enumeration_source` uses for
    enumeration. That choice is deliberate, not laziness: mirroring stops at the
    project root (`_mirror_root`), so a source *at* the root with
    `#include "../above.h"` misses the mirror and falls through to the `-I`
    search with the spelled path — silently a different translation unit, not a
    block (measured: `test_include_above_the_mirror_root_is_a_known_residual`).
    Tolerable for an enumeration; not for the verify the whole gate rests on. A
    same-directory snapshot has no mirror root to fall off: it *is* the
    includer's own directory, real siblings included, so a quoted `#include`
    resolves exactly as the in-place parse would — nothing to approximate.

    The trade is the mirror's own residual for a narrower one: this snapshot
    cannot occupy `file_path`'s own name (`tempfile.mkstemp` gives it a random
    one), so a translation unit that `#include`s **itself by its own literal
    name** reaches the live file during the verify, not the snapshot — the
    self-alias class `enumeration_source` closes by having the snapshot occupy
    the source's exact spelled path. Pinned by
    `test_self_include_by_own_name_is_a_known_residual`. Narrower because it
    needs an unusual C construct (a `.c` including itself by name) rather than
    an ordinary `#include "../common.h"` from a project-root source.

    The random name is also a problem the moment a translation unit reads its
    own identity back: `__FILE__` (standard) and `__BASE_FILE__` (a GCC/clang
    extension) are the *presumed* source name, and without correction that
    presumed name would be the snapshot's random one — not `file_path` — so
    code that branches on it (`strcmp(__FILE__, "expected.c")`, an `#include`
    built from `__BASE_FILE__`, …) would take a different path than the real
    file does, silently verifying a different program. A leading `#line 1
    "file_path"` directive fixes the *presumed* name back to `file_path`
    without touching the physical bytes `content` holds or where they sit:
    verified empirically against a live `esbmc` — a `strcmp(__FILE__, ...)`
    branch (and the same for `__BASE_FILE__`) taken by the real file is taken
    identically by the snapshot once the directive is prepended, and every
    reported counterexample line number still lines up with `file_path`'s own
    (`#line 1` re-bases the immediately following line to 1, matching
    `content`'s own first line).

    This directive must stay verify-only, never grow onto `enumeration_source`'s
    snapshot: `forseti list-units` (`parse_units`) attributes a `FunctionDecl` to
    the source by a normalized match against the literal CLI path it was given,
    and a `#line`-carrying file reports every location as the directive's target
    instead — so every function would fail that match and `list-units` would
    report a clean, function-free file, which `_list_units` (correctly, for a
    file that genuinely has none) treats as a legitimate pass. `verify_function`
    has no such filter — it looks a function up by name in the whole compiled
    unit — which is exactly why this directive is safe here and would not be
    there.

    `__TIMESTAMP__` (a GCC/clang extension expanding to the source's last
    modification time, `ctime`-formatted) is the same class of problem one
    layer down from `__FILE__`, and — unlike `__FILE__` — the `#line` directive
    does *nothing* for it: measured directly against clang, `__TIMESTAMP__`
    follows the snapshot's own physical mtime regardless of what the directive
    presumes the name to be. Left alone, a freshly-`mkstemp`'d snapshot always
    reads "now", diverging from `file_path`'s real mtime on every verify.
    `mtime_ns` is required, not optional, and is applied to the snapshot with
    `os.utime` right after writing it, so `__TIMESTAMP__` expands to the same
    value in place or through the snapshot — a caller cannot silently opt back
    into the "now" mtime by omitting it (review feedback on PR #159, issue
    #150).

    That pairing is only guaranteed at stage time: nothing re-checks it once the
    snapshot is written. If the live file's mtime moves afterward with
    unchanged content, `__TIMESTAMP__` inside the verify and outside it diverge
    — a separate, documented residual at `verify_and_record`'s post-verify
    drift check, not this function's to close (issue #164).

    A leading UTF-8 BOM is written at byte zero, ahead of the directive,
    instead of being pushed into the middle of the file: clang only recognizes
    a BOM there, and one sitting in front of the directive is read as part of
    the directive's own first token, corrupting it. Verified empirically:
    without this, a valid BOM-prefixed `.c` fails to compile through the
    snapshot with the BOM fused onto whatever follows (e.g. `int` becomes an
    unrecognized `﻿int`) even though the same file compiles fine in place
    (review feedback on PR #159, issue #150).

    The snapshot keeps `file_path`'s own suffix, case included, rather than a
    fixed `.c`: `is_c_source`/`extract_function_defs` both lowercase before
    comparing, so an uppercase `.C` reaches here too, and the two spellings are
    not interchangeable to `esbmc`'s own frontend — the one `forseti verify`
    actually runs, not just GCC's documented convention. Verified empirically:
    the same bytes parse as C++ through a `.C` name (`class Foo { ... };`
    parses; the identical `.c` name fails with "use of undeclared identifier")
    and diverge at runtime too (`sizeof('a') == sizeof(int)` — true in C,
    false in C++ — flips `esbmc`'s verdict on an otherwise identical file
    depending only on which suffix it was run with). Forcing `.c` would verify
    a `.C` file as C while the real translation unit — the one that ships —
    is C++, letting a safety branch that depends on the difference diverge
    from the recorded verdict. It would also part ways with
    `enumeration_source`, which already stages the enumeration snapshot under
    the source's real basename, suffix and all (review feedback on PR #159,
    issue #150).

    The name carries `_VERIFY_SNAPSHOT_PREFIX` so `_untracked_snapshot`
    excludes it from the gate's own discovery for as long as it is untracked —
    see there for why a *tracked* file sharing the prefix must not be exempted
    the same way. `git status` still lists it until the cleanup below runs
    (or, if this process is killed first, until something else removes it) —
    the cost of writing into the user's tree at all, and cheaper than the race
    this exists to close.
    """

    spelled = os.path.join(project_dir, os.fspath(file_path))
    src_dir = os.path.dirname(spelled) or "."
    directive = _line_directive(os.fspath(file_path))
    bom, body = (
        (content[: len(_UTF8_BOM)], content[len(_UTF8_BOM) :])
        if content.startswith(_UTF8_BOM)
        else (b"", content)
    )
    with _staged_source(
        os.fspath(file_path),
        directory=src_dir,
        purpose="verification",
        chunks=(bom, directive, body),
        mtime_ns=mtime_ns,
    ) as path:
        yield path
