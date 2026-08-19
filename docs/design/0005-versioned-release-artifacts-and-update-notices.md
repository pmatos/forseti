# Design RFC 0005 — Versioned release artifacts and update notices

- **Status:** Draft / RFC (thinking aid — not yet an ADR)
- **Date:** 2026-08-19

## Problem

Forseti already uses Conventional Commit checks and semantic-release, but the release pipeline
stops at GitHub tags, release notes, and `CHANGELOG.md`. The Python distribution has a literal
`version = "0.0.0"` in `pyproject.toml`, and no semantic-release step changes it. Consequently,
installing the current checkout with `uv tool install .` reports `forseti==0.0.0`, even though the
repository has stable releases through `v1.7.5`.

The existing GitHub Releases also contain no installable Forseti artifact. A running CLI therefore
has neither trustworthy installed-version metadata nor a canonical artifact it can recommend when
a newer release exists.

We want one release version to flow through the source tree, wheel metadata, Git tag, and GitHub
Release. User-facing CLI commands should periodically discover a newer release and print an exact,
copyable update command without ever modifying their installation automatically.

## Decision

### Synchronize `pyproject.toml` during semantic-release

Forseti follows JSSE's static-version release pattern. `pyproject.toml` is bootstrapped from
`0.0.0` to the current release, `1.7.5`. Thereafter, semantic-release's calculated
`${nextRelease.version}` is the only input to the release preparation command.

The semantic-release configuration gains `@semantic-release/exec`. Its `prepareCmd` invokes a
repository-owned preparation script with the next version. The script:

1. accepts exactly one `MAJOR.MINOR.PATCH` version;
2. replaces only `[project].version` in `pyproject.toml`, failing if it cannot make exactly one
   replacement;
3. removes stale wheel output from `dist/`;
4. builds one pure-Python wheel; and
5. verifies that the resulting filename and wheel `METADATA` both contain the requested version.

`@semantic-release/github` attaches `dist/forseti-<version>-py3-none-any.whl` to the release.
`@semantic-release/git` commits `pyproject.toml` together with `CHANGELOG.md`, then semantic-release
tags that release commit. The source metadata, artifact metadata, tag, and GitHub Release therefore
cannot intentionally diverge. Any validation or build failure stops the release before publication.

The release workflow adds Python setup and the wheel-build tooling required by the preparation
script. It keeps full Git history and the existing Node/semantic-release setup. PyPI publication,
signing, prerelease channels, platform-specific artifacts, and automatic installation are out of
scope.

### Treat the GitHub wheel as the update source

The update source is GitHub's latest-release endpoint for `pmatos/forseti`. A usable candidate must
have all of the following:

- a stable `vMAJOR.MINOR.PATCH` release tag;
- a matching `forseti-MAJOR.MINOR.PATCH-py3-none-any.whl` asset; and
- an asset URL returned by GitHub, rather than a URL reconstructed locally.

Versions are compared numerically by major, minor, and patch. Forseti's semantic-release branch
configuration only produces stable releases, so prerelease and build-metadata comparison is not
part of this interface.

When a usable release is newer than the installed distribution version, the notice contains the
exact artifact URL and this command:

```text
uv tool install --force https://github.com/pmatos/forseti/releases/download/v1.8.0/forseti-1.8.0-py3-none-any.whl
```

The URL is shell-quoted when necessary. The old `v1.7.5` release has no wheel, so it does not
produce a misleading update notice. Notices begin with the first artifact-bearing release.

### Put update policy behind one deep module

A dependency-free update-notice module owns version discovery, release parsing, numeric comparison,
cache freshness, failure throttling, and banner rendering. Callers ask it for the notice appropriate
to the installed version; they do not reproduce any GitHub or cache rules.

Its external adapters are the only variable seams inside the implementation:

- GitHub HTTP access;
- wall-clock time; and
- the user cache directory.

Production uses `urllib`, `time`, and the XDG cache convention. Tests substitute these external
adapters; they do not mock Forseti's own internal helpers.

The cache lives at `$XDG_CACHE_HOME/forseti/update-check.json`, falling back to
`~/.cache/forseti/update-check.json`. It records the last check time and the last known usable
candidate. A result is fresh for 12 hours. A stale cache triggers at most one GitHub request; a
success replaces the candidate, while a failure preserves any previously known candidate and moves
the check timestamp forward so every CLI call does not retry a failing network request.

Writes use a temporary sibling followed by atomic replacement. A missing home directory, read-only
cache, malformed JSON, an interrupted write, a backwards or implausibly forward clock jump, an API
error, a timeout, or an unexpected response is non-fatal. The requested Forseti command continues.
The HTTP request has a short finite timeout and identifies the installed Forseti version in its user
agent.

### Warn on user-facing commands, before their handlers run

The unified `forseti` entry point consults the update module before dispatching a user-facing
subcommand. `forseti-esbmc` does the same before verification. A known update prints a compact banner
to stderr on every such invocation, even when the cached result remains within its 12-hour freshness
window. Stdout therefore remains valid for `--json` and other machine-readable output.

`forseti claude-code-hook ...` and `forseti mcp` are excluded. The former can run hundreds of times
per agent session, and the latter is a protocol process rather than an interactive CLI invocation.
The exclusion is decided from the requested command before any update work. Forseti currently has no
global `--quiet` flag, so this RFC adds no quiet behavior. If a global quiet flag is introduced later,
the notice must honor it.

The package's installed version comes from `importlib.metadata`, not from a second source constant.
The CLIs also expose that same installed value through their version option, making the version seen
by users identical to the value used for update comparison.

## TDD seams and vertical slices

Tests are written only at these agreed seams:

1. **Installed-distribution seam.** Build or install the repository through standard Python/uv
   packaging and observe its public distribution metadata. The permanent regression asserts that
   the installed version equals `pyproject.toml` and is not `0.0.0`.
2. **Release-preparation command seam.** Invoke the same repository command semantic-release uses
   against an isolated project copy, then observe `pyproject.toml`, the wheel filename, and the
   wheel's public `METADATA`. Invalid input must fail without publishing a plausible artifact.
3. **CLI-process seam.** Invoke `forseti`/`forseti-esbmc` through their existing `main(argv)`
   interfaces and observe exit status, stdout, and stderr. HTTP, clock, and cache-directory adapters
   are controlled at their external seams. Tests do not call or assert against private parsing or
   cache helpers.

Implementation proceeds as vertical red-green slices:

1. a package-metadata test reproduces `0.0.0`, then the bootstrap version makes it pass;
2. a release-preparation test fails because no synchronized wheel command exists, then the script
   and semantic-release wiring make it pass;
3. one user-facing CLI call receives a known newer release and fails for lack of a banner, then the
   minimal notice path makes it pass;
4. a second call within 12 hours must still warn without another request;
5. equal/older/malformed/missing-artifact and external-failure cases are added one behavior at a
   time; and
6. internal hook and MCP invocations are shown not to enter the update-notice path.

No implementation refactor is mixed into these red-green cycles. Any cleanup follows only after the
behavioral slices are green and is reviewed separately.

## Validation

The completed change must pass:

- the package-metadata and release-preparation regression tests;
- update-notice and CLI integration tests;
- `ruff check src tests`;
- `ruff format --check src tests`;
- `ty check src tests`;
- `pytest -q`; and
- the original isolated `uv tool install --force .` reproduction, which must report
  `forseti v1.7.5` instead of `forseti v0.0.0` before the next semantic release is cut.

The semantic-release JSON and GitHub Actions workflow are also syntax-checked. A dry-run or fixture
exercise must establish that a calculated future version produces exactly one matching wheel asset.

## Consequences

- Source installs report the latest released version until the next release commit, matching JSSE's
  static-version model rather than inventing development versions.
- Every release becomes directly installable from a stable GitHub artifact without PyPI credentials.
- Update discovery adds no runtime dependency and never turns GitHub availability into a Forseti
  command dependency.
- A stale check can delay one user-facing invocation by the short HTTP timeout; subsequent calls are
  cache-only for 12 hours.
- Users receive a command on every user-facing invocation while an update remains outstanding, but
  Forseti never changes its own executable or environment.
