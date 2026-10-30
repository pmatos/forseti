"""Project index and context-pack builder (#423): slices, budget, reach, hygiene."""

from __future__ import annotations

import sqlite3
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from forseti.core._proposal_request import prepare_proposal_request
from forseti.properties import (
    RECIPES,
    SINGLE_FILE_RECIPES,
    Property,
    PropertyKind,
    PropertyStatus,
    PropertyStore,
    Provenance,
    RecipeError,
    build_context,
    make_property_id,
)
from forseti.properties.index import (
    GitTree,
    MappingTree,
    OverlayTree,
    ProjectIndex,
    ProjectIndexError,
    Reach,
    is_test_path,
)
from forseti.properties.pack import (
    PackConfig,
    SliceKind,
    build_context_pack,
    estimate_tokens,
    store_contracts,
)

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"

RING_H = """\
#define CAP 4
#define SLOTS (CAP * 2)

typedef struct {
    int buf[SLOTS];
    unsigned head, tail, count;
} ring_t;

/* Initialise an empty ring. */
void rb_init(ring_t *r);
int rb_push(ring_t *r, int v);
void log_full(int count);
"""

RING_C = """\
#include "ring.h"

/* Moves the tail one slot on. */
static void advance(ring_t *r) {
    r->tail = (r->tail + 1) % SLOTS; /* secret_body_marker */
}

/* Push v; returns 0 when full. */
int rb_push(ring_t *r, int v) {
    if (r->count == SLOTS) {
        log_full(r->count);
        return 0;
    }
    r->buf[r->tail] = v;
    advance(r);
    r->count++;
    return 1;
}

void rb_init(ring_t *r) {
    r->head = r->tail = r->count = 0;
}
"""

APP_C = """\
#include "ring.h"

int enqueue_all(ring_t *r, int n) {
    int stored = 0;
    for (int i = 0; i < n; i++) {
        if (!rb_push(r, i)) {
            break;
        }
        stored++;
    }
    return stored;
}

int main(void) {
    ring_t r;
    rb_init(&r);
    return rb_push(&r, 1);
}
"""

TEST_C = """\
#include "ring.h"

/* A full ring refuses another push. */
static void test_push_when_full(void) {
    ring_t r;
    rb_init(&r);
    for (int i = 0; i < SLOTS; i++) {
        rb_push(&r, i);
    }
    assert(rb_push(&r, 9) == 0);
}
"""

PROJECT: Mapping[str, str] = {
    "include/ring.h": RING_H,
    "src/ring.c": RING_C,
    "src/app.c": APP_C,
    "tests/test_ring.c": TEST_C,
    "README.md": "rb_push is documented here",
}

UNIT = "src/ring.c::rb_push"


def build(
    config: PackConfig | None = None,
    files: Mapping[str, str] = PROJECT,
    unit: str = UNIT,
    **kwargs: object,
) -> str:
    index = ProjectIndex(MappingTree(files))
    return build_context_pack(index, unit, config, **kwargs).text  # ty: ignore[invalid-argument-type]


def test_pack_has_every_slice() -> None:
    text = build()
    assert "int rb_push(ring_t *r, int v) {" in text
    assert "#define CAP 4" in text and "} ring_t;" in text
    assert "static void advance(ring_t *r);" in text
    assert "/* Moves the tail one slot on. */" in text
    assert "void log_full(int count);" in text
    assert "src/app.c::enqueue_all" in text and "if (!rb_push(r, i)) {" in text
    assert "tests/test_ring.c::test_push_when_full" in text
    assert "src/ring.c::rb_init" in text


def test_callees_show_signatures_never_bodies() -> None:
    text = build()
    assert "secret_body_marker" not in text
    assert (
        "r->tail = (r->tail + 1)"
        not in text.split("how it is called")[0].split("=== functions it calls")[1]
    )


def test_main_is_not_a_caller() -> None:
    text = build()
    assert "rb_init(&r);\n    return rb_push(&r, 1);" not in text
    assert "src/app.c::main" not in text
    with_main = build(PackConfig(skip_callers=()))
    assert "src/app.c::main" in with_main


def test_pack_is_deterministic() -> None:
    first = build()
    assert first == build()
    assert first == build(files=dict(reversed(list(PROJECT.items()))))


def test_pack_is_deterministic_across_slice_config_order() -> None:
    slices = [SliceKind.TESTS, SliceKind.TARGET, SliceKind.TYPES]
    assert build(PackConfig(slices=slices)) == build(PackConfig(slices=slices[::-1]))


def test_file_reach_stays_in_the_units_file() -> None:
    text = build(PackConfig(reach=Reach.FILE))
    assert "src/app.c" not in text and "test_ring.c" not in text
    assert "#define CAP" not in text
    assert "void log_full" not in text
    assert "static void advance(ring_t *r);" in text
    assert "src/ring.c::rb_init" in text


def test_slice_selection_and_blind_target() -> None:
    blind = build(PackConfig(slices={SliceKind.SIGNATURE, SliceKind.CALLERS}))
    assert "int rb_push(ring_t *r, int v);" in blind
    assert "r->buf[r->tail] = v;" not in blind
    assert "ring_t;" not in blind
    assert "src/app.c::enqueue_all" in blind


def test_target_and_signature_are_exclusive() -> None:
    config = PackConfig(slices={SliceKind.TARGET, SliceKind.SIGNATURE})
    with pytest.raises(ProjectIndexError, match="not both"):
        build(config)


def test_types_follow_the_target_and_respect_depth() -> None:
    # ring_t -> SLOTS -> CAP is three levels of nesting.
    deep = build(PackConfig(slices={SliceKind.TARGET, SliceKind.TYPES}, type_depth=3))
    assert "#define CAP 4" in deep and "#define SLOTS" in deep and "ring_t;" in deep
    shallow = build(
        PackConfig(slices={SliceKind.TARGET, SliceKind.TYPES}, type_depth=1)
    )
    assert "ring_t;" in shallow
    assert "#define SLOTS" in shallow  # the body names SLOTS directly
    assert "#define CAP" not in shallow


def test_budget_keeps_the_target_and_reports_the_rest_omitted() -> None:
    index = ProjectIndex(MappingTree(PROJECT))
    full = build_context_pack(index, UNIT)
    assert full.omitted == 0
    tight = build_context_pack(index, UNIT, PackConfig(max_tokens=1))
    assert [e.kind for e in tight.entries] == [SliceKind.TARGET]
    assert tight.omitted == len(full.entries) - 1


def test_budget_is_respected_and_monotonic() -> None:
    index = ProjectIndex(MappingTree(PROJECT))
    sizes = []
    for budget in (150, 250, 400, 1000):
        pack = build_context_pack(index, UNIT, PackConfig(max_tokens=budget))
        target = build_context_pack(
            index, UNIT, PackConfig(slices={SliceKind.TARGET})
        ).tokens
        assert pack.tokens <= max(budget, target)
        sizes.append(len(pack.entries))
    assert sizes == sorted(sizes)
    assert sizes[0] < sizes[-1]


def test_an_entry_too_big_for_the_budget_does_not_block_smaller_ones() -> None:
    big_test = "static void test_big(void) {\n" + "    rb_push(0, 1);\n" * 400 + "}\n"
    files = {**PROJECT, "tests/test_big.c": big_test}
    index = ProjectIndex(MappingTree(files))
    config = PackConfig(
        slices={SliceKind.TARGET, SliceKind.TESTS},
        test_max_lines=1000,
        max_tests=3,
        max_tokens=400,
    )
    pack = build_context_pack(index, UNIT, config)
    labels = [e.label for e in pack.entries]
    assert "tests/test_ring.c::test_push_when_full" in labels
    assert "tests/test_big.c::test_big" not in labels
    assert pack.omitted == 1


def test_estimate_tokens_rounds_up() -> None:
    assert [estimate_tokens(s) for s in ("", "a", "abcd", "abcde")] == [0, 1, 1, 2]


def test_caps_limit_entries_per_slice() -> None:
    callers = "".join(
        f"int c{i}(ring_t *r) {{ return rb_push(r, {i}); }}\n" for i in range(9)
    )
    files = {**PROJECT, "src/many.c": '#include "ring.h"\n' + callers}
    text = build(PackConfig(slices={SliceKind.CALLERS}, max_callers=2), files)
    assert text.count("// src/") == 2
    assert "src/app.c::enqueue_all" in text and "src/many.c::c0" in text
    assert "src/many.c::c1" not in text


def test_max_callees_caps_the_slice() -> None:
    src = "int a(void);\nint b(void);\nint f(void) { return a() + b(); }\n"
    text = build(
        PackConfig(slices={SliceKind.CALLEES}, max_callees=1), {"x.c": src}, "x.c::f"
    )
    assert "int a(void);" in text and "int b(void);" not in text


def test_callers_ranked_unit_file_first_then_path() -> None:
    files = {
        **PROJECT,
        "src/ring.c": RING_C + "int again(ring_t *r) { return rb_push(r, 2); }\n",
    }
    text = build(PackConfig(slices={SliceKind.CALLERS}, max_callers=2), files)
    assert text.index("src/ring.c::again") < text.index("src/app.c::enqueue_all")


def test_caller_windows_merge_and_clip_to_the_calling_function() -> None:
    body = "".join(f"    int v{i} = {i};\n" for i in range(12))
    caller = (
        "int far(ring_t *r) {\n"
        "    rb_push(r, 0);\n"
        "    rb_push(r, 1);\n"
        f"{body}"
        "    return rb_push(r, 2);\n"
        "}\n"
        "int after(void) { return 77; }\n"
    )
    files = {**PROJECT, "src/far.c": '#include "ring.h"\n' + caller}
    text = build(
        PackConfig(slices={SliceKind.CALLERS}, caller_context_lines=1, max_callers=9),
        files,
    )
    entry = text.split("// src/far.c::far\n")[1].split("\n// src/")[0]
    assert entry.count("// ...") == 1
    assert "rb_push(r, 0);\n    rb_push(r, 1);" in entry
    assert "int after" not in entry
    assert "int far(ring_t *r) {" in entry  # the window may reach the signature
    assert "int v5" not in entry


def test_static_functions_only_have_callers_in_their_own_file() -> None:
    files = {
        "a.c": "static int h(int x) { return x; }\nint f(void) { return h(1); }\n",
        "b.c": "static int h(int x) { return x; }\nint g(void) { return h(2); }\n",
        "c.h": "static inline int s(int x) { return x; }\n",
        "d.c": '#include "c.h"\nint k(void) { return s(3); }\n',
    }
    only = PackConfig(slices={SliceKind.CALLERS})
    assert "a.c::f" in build(only, files, "a.c::h")
    assert "b.c::g" not in build(only, files, "a.c::h")
    assert "d.c::k" in build(only, files, "c.h::s")


def test_a_same_named_static_elsewhere_is_neither_callee_nor_caller() -> None:
    files = {
        "a.c": "int f(void) { return helper(); }\n",
        "b.c": (
            "static int helper(void) { return 1; }\nint g(void) { return helper(); }\n"
        ),
        "c.h": "int helper(void);\n",
        "z.c": "int helper(void) { return 2; }\n",
    }
    callees = build(PackConfig(slices={SliceKind.CALLEES}), files, "a.c::f")
    assert "z.c::helper" in callees and "b.c::helper" not in callees
    callers = build(PackConfig(slices={SliceKind.CALLERS}), files, "z.c::helper")
    assert "a.c::f" in callers and "b.c::g" not in callers


def test_static_in_a_parameter_does_not_make_a_function_file_local() -> None:
    files = {
        "a.c": "int sum(const int a[static 4]) { return a[0]; }\n",
        "b.c": (
            "int sum(const int a[static 4]);\n"
            "int use(const int *p) { return sum(p); }\n"
        ),
    }
    text = build(PackConfig(slices={SliceKind.CALLERS}), files, "a.c::sum")
    assert "b.c::use" in text


def test_a_static_functions_tests_may_include_its_file() -> None:
    files = {
        "src/ring.c": "static int advance(int x) { return x + 1; }\n",
        "tests/test_ring.c": (
            '#include "../src/ring.c"\nstatic void test_advance(void) { advance(1); }\n'
        ),
    }
    text = build(PackConfig(slices={SliceKind.TESTS}), files, "src/ring.c::advance")
    assert "tests/test_ring.c::test_advance" in text


def test_top_level_macro_invocations_are_not_functions() -> None:
    src = (
        "int rb_push(int);\n"
        "EXPORT_SYMBOL(rb_push);\n"
        'MODULE_LICENSE("GPL");\n'
        "static DEFINE_MUTEX(lock);\n"
        "module_init(foo_init);\n"
    )
    index = ProjectIndex(MappingTree({"a.c": src}))
    assert [r.name for r in index.functions("a.c")] == ["rb_push"]


def test_a_blind_pack_is_chosen_by_signature_not_by_a_missing_target() -> None:
    bodyless = build(PackConfig(slices={SliceKind.TYPES}))
    assert "#define SLOTS" in bodyless


def test_tests_slice_truncates_long_tests() -> None:
    long_test = "static void test_long(void) {\n" + "    rb_push(0, 1);\n" * 50 + "}\n"
    files = {"src/ring.c": RING_C, "tests/ring_test.c": long_test}
    text = build(PackConfig(slices={SliceKind.TESTS}, test_max_lines=5), files)
    assert "// ... (truncated)" in text
    assert text.count("rb_push(0, 1);") == 4


def test_a_file_in_a_test_directory_is_a_test_not_a_caller() -> None:
    callers = build(PackConfig(slices={SliceKind.CALLERS}))
    assert "test_push_when_full" not in callers
    tests = build(PackConfig(slices={SliceKind.TESTS}))
    assert "enqueue_all" not in tests


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/x.c", True),
        ("lib/kunit/x.c", True),
        ("src/test_ring.c", True),
        ("src/ring_test.c", True),
        ("src/ring-tests.c", True),
        ("lib/ring_kunit.c", True),
        ("src/ring.c", False),
        ("src/contest.c", False),
        ("src/latest_ring.c", False),
    ],
)
def test_is_test_path(path: str, expected: bool) -> None:
    assert is_test_path(path) is expected


def test_contracts_seam_adds_accepted_lines() -> None:
    def contracts(unit_id: str) -> list[str]:
        return ["result >= 0\n  && x"] if unit_id == "src/ring.c::advance" else []

    text = build(contracts=contracts)
    assert "// accepted: result >= 0 && x" in text


def test_store_contracts_reads_accepted_properties_only() -> None:
    unit = "src/ring.c::advance"
    with PropertyStore(sqlite3.connect(":memory:")) as store:

        def prop(
            expression: str, status: PropertyStatus, domain: tuple[str, ...]
        ) -> None:
            store.add(
                Property(
                    property_id=make_property_id(
                        unit, PropertyKind.SEMANTIC, expression, domain
                    ),
                    unit_id=unit,
                    kind=PropertyKind.SEMANTIC,
                    expression=expression,
                    status=status,
                    provenance=Provenance("p", "1"),
                    domain=domain,
                )
            )

        prop(
            "tail < SLOTS",
            PropertyStatus.ACCEPTED,
            ("count < SLOTS", "tail < SLOTS"),
        )
        prop("tail == 0", PropertyStatus.ACCEPTED, ())
        prop("head == 7", PropertyStatus.CANDIDATE, ())
        contracts = store_contracts(store, root=Path("/project"))
        assert contracts(unit) == [
            "tail < SLOTS (given count < SLOTS; tail < SLOTS)",
            "tail == 0",
        ]
        assert contracts("src/ring.c::rb_init") == []
        text = build(PackConfig(slices={SliceKind.CALLEES}), contracts=contracts)
    assert "// accepted: tail == 0" in text


@pytest.mark.parametrize(
    "unit", ["src/ring.c::nope", "src/missing.c::rb_push", "README.md::rb_push"]
)
def test_unknown_unit_is_an_error(unit: str) -> None:
    with pytest.raises(ProjectIndexError, match="no definition"):
        build(unit=unit)


def test_unit_path_is_normalised() -> None:
    assert build(unit="./src/ring.c::rb_push") == build().replace(
        "// src/ring.c::rb_push\n", "// ./src/ring.c::rb_push\n", 1
    )


def test_a_prototype_is_not_a_definition() -> None:
    with pytest.raises(ProjectIndexError):
        build(unit="include/ring.h::rb_push")


def test_own_file_wins_when_a_name_is_defined_twice() -> None:
    other = "int advance(int x) { return x; }\n"
    files = {"a.c": "static void advance(void) {}\nint f(void){advance();return 0;}\n"}
    files["b.c"] = other
    text = build(PackConfig(slices={SliceKind.CALLEES}), files, unit="a.c::f")
    assert "static void advance(void);" in text and "int advance(int x)" not in text


def test_recursion_is_not_a_callee_and_strings_and_comments_are_not_calls() -> None:
    src = (
        "int g(int);\n"
        "int f(int n) {\n"
        '    /* g(1) */ const char *s = "g(2)";\n'
        "    return n ? f(n - 1) : 0;\n"
        "}\n"
    )
    index = ProjectIndex(MappingTree({"a.c": src}))
    (ref,) = [r for r in index.functions("a.c") if r.is_definition]
    assert index.callees(ref) == ()
    assert index.call_sites("a.c", "g") == ()


def test_calls_through_members_are_not_calls() -> None:
    src = (
        "int h(int);\n"
        "int f(struct ops *o) { return o->h(1) + o.h(2); }\n"
        "int k(void) { return h(3); }\n"
    )
    index = ProjectIndex(MappingTree({"a.c": src}))
    assert [s.caller for s in index.call_sites("a.c", "h")] == ["k"]


def test_function_names_survive_decorations() -> None:
    src = (
        "__attribute__((noreturn)) void die(void) { for (;;) {} }\n"
        "__printf(1, 2) static int logf2(const char *fmt, ...) { return 0; }\n"
        "static int table[2] = { 1, 2 };\n"
        "int (*handler)(int) = 0;\n"
        "typedef int (*cb_t)(int);\n"
        "extern int proto(int, int);\n"
        "extern int plain;\n"
    )
    index = ProjectIndex(MappingTree({"a.c": src}))
    got = {(r.name, r.is_definition) for r in index.functions("a.c")}
    assert got == {("die", True), ("logf2", True), ("proto", False)}
    (proto,) = [r for r in index.functions("a.c") if r.name == "proto"]
    assert index.callees(proto) == ()


def test_only_c_files_are_indexed() -> None:
    index = ProjectIndex(MappingTree(PROJECT))
    assert index.files == (
        "include/ring.h",
        "src/app.c",
        "src/ring.c",
        "tests/test_ring.c",
    )
    with pytest.raises(ProjectIndexError, match="not a source file"):
        index.source("README.md")


@pytest.mark.parametrize(
    ("unit", "callers", "callees"),
    [
        ("merge_sort.c::merge", ["msort"], []),
        ("merge_sort.c::msort", [], ["merge"]),
        ("ring_buffer.c::rb_push", [], []),
        ("frame_checksum.c::sum_bytes", ["frame_checksum", "payload_checksum"], []),
    ],
)
def test_examples_pack_has_no_harness_leak(
    unit: str, callers: list[str], callees: list[str]
) -> None:
    files = {p.name: p.read_text() for p in EXAMPLES.glob("*.c")}
    index = ProjectIndex(MappingTree(files))
    pack = build_context_pack(index, unit)
    labels = {kind: [] for kind in SliceKind}
    for entry in pack.entries:
        labels[entry.kind].append(entry.label.split("::")[-1])
    assert labels[SliceKind.CALLERS] == callers
    assert labels[SliceKind.CALLEES] == callees
    assert "main" not in labels[SliceKind.SIBLINGS]
    assert "assert(" not in pack.text
    assert "Latent edge case" not in pack.text
    assert pack.text == build_context_pack(index, unit).text


def test_examples_pack_includes_types() -> None:
    files = {p.name: p.read_text() for p in EXAMPLES.glob("*.c")}
    text = build(PackConfig(slices={SliceKind.TYPES}), files, "ring_buffer.c::rb_push")
    assert "#define CAP 4" in text and "} ring_t;" in text


def git(root: Path, *args: str) -> str:
    done = subprocess.run(
        [
            "git",
            "-c",
            "user.name=T",
            "-c",
            "user.email=t@example.com",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


def commit(root: Path, files: Mapping[str, str], message: str) -> str:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(root, "add", "--", *files)
    git(root, "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    return tmp_path


def pack_at(repo: Path, revision: str, config: PackConfig | None = None) -> str:
    index = ProjectIndex(GitTree.at(repo, revision))
    return build_context_pack(index, UNIT, config).text


def test_git_pack_matches_the_in_memory_pack(repo: Path) -> None:
    commit(repo, PROJECT, "v1")
    assert pack_at(repo, "HEAD") == build()


def test_temporal_hygiene_only_the_given_commit_is_read(repo: Path) -> None:
    first = commit(repo, PROJECT, "v1")
    v1_text = pack_at(repo, first)

    later_caller = (
        '#include "ring.h"\nint later_caller(ring_t *r) { return rb_push(r, 42); }\n'
    )
    later_test = '#include "ring.h"\nstatic void test_later(void) { rb_push(0, 7); }\n'
    second = commit(
        repo,
        {
            "src/later.c": later_caller,
            "tests/test_later.c": later_test,
            "src/ring.c": RING_C.replace(
                "/* Push v; returns 0 when full. */", "/* A comment from the fix. */"
            ),
            "include/ring.h": RING_H.replace("#define CAP 4", "#define CAP 99"),
        },
        "v2: fix",
    )
    (repo / "src/ring.c").write_text(RING_C + "/* uncommitted edit */\n")
    (repo / "src/untracked.c").write_text("int u(void) { return rb_push(0, 1); }\n")

    at_first = pack_at(repo, first)
    assert at_first == v1_text
    for later in (
        "later_caller",
        "test_later",
        "from the fix",
        "CAP 99",
        "uncommitted",
    ):
        assert later not in at_first
    assert "untracked" not in at_first

    at_second = pack_at(repo, second)
    for present in ("later_caller", "test_later", "from the fix", "CAP 99"):
        assert present in at_second
    assert "uncommitted" not in at_second
    assert pack_at(repo, f"{second}~1") == v1_text


def test_git_tree_pins_the_revision_to_a_sha(repo: Path) -> None:
    first = commit(repo, PROJECT, "v1")
    tree = GitTree.at(repo, "HEAD")
    commit(repo, {"src/extra.c": "int e(void) { return 0; }\n"}, "v2")
    assert tree.commit == first
    assert "src/extra.c" not in tree.paths()
    assert tree.read("src/app.c") == APP_C


def test_git_tree_in_a_subdirectory_reads_the_whole_commit(repo: Path) -> None:
    commit(repo, PROJECT, "v1")
    tree = GitTree.at(repo / "src", "HEAD")
    assert "src/ring.c" in tree.paths() and "include/ring.h" in tree.paths()
    assert tree.read("src/ring.c") == RING_C


def test_git_tree_rejects_a_bad_revision(repo: Path) -> None:
    commit(repo, PROJECT, "v1")
    with pytest.raises(ProjectIndexError, match="rev-parse"):
        GitTree.at(repo, "no-such-ref")
    with pytest.raises(ProjectIndexError, match="rev-parse"):
        GitTree.at(repo.parent / "elsewhere", "HEAD")


def recipe_text(recipe: str, repo: Path, source: str | None = None) -> str:
    path = repo / "src/ring.c"
    return build_context(
        recipe,
        unit_id=f"{path}::rb_push",
        source_text=path.read_text() if source is None else source,
        project_root=repo,
    ).text


def test_project_recipes_are_registered_beside_the_single_file_ones() -> None:
    assert SINGLE_FILE_RECIPES == ("baseline", "sighted", "blind")
    refs = {name: RECIPES[name].ref for name in RECIPES}
    assert refs == {
        "baseline": "baseline/v1",
        "sighted": "sighted/v1",
        "blind": "blind/v1",
        "callers": "callers/v1",
        "tests": "tests/v1",
        "full": "full/v1",
    }


def test_each_project_recipe_selects_its_slices(repo: Path) -> None:
    commit(repo, PROJECT, "v1")
    callers = recipe_text("callers", repo)
    assert "src/app.c::enqueue_all" in callers
    assert "test_push_when_full" not in callers
    assert "functions it calls" not in callers
    assert "#define CAP 4" in callers and "r->buf[r->tail] = v;" in callers

    tests = recipe_text("tests", repo)
    assert "test_push_when_full" in tests and "enqueue_all" not in tests

    full = recipe_text("full", repo)
    for part in (
        "enqueue_all",
        "test_push_when_full",
        "static void advance",
        "rb_init",
    ):
        assert part in full


def test_the_unit_file_is_the_current_text_everything_else_is_head(
    repo: Path,
) -> None:
    commit(repo, PROJECT, "v1")
    edited = RING_C.replace("return 0;", "return -1; /* edited */")
    (repo / "src/ring.c").write_text(edited)
    (repo / "src/app.c").write_text(
        APP_C + "int uncommitted(ring_t *r) { return rb_push(r, 5); }\n"
    )
    (repo / "tests/test_new.c").write_text(
        "static void test_new(void) { rb_push(0, 1); }\n"
    )
    text = recipe_text("full", repo, source=edited)
    assert "return -1; /* edited */" in text
    assert "uncommitted" not in text and "test_new" not in text
    assert "src/app.c::enqueue_all" in text


def test_recipe_packs_see_only_the_head_commit(repo: Path) -> None:
    first = commit(repo, PROJECT, "v1")
    v1_text = recipe_text("full", repo)
    commit(
        repo,
        {
            "src/later.c": (
                '#include "ring.h"\nint later(ring_t *r) { return rb_push(r, 1); }\n'
            ),
            "tests/test_later.c": "static void test_later(void) { rb_push(0, 7); }\n",
        },
        "v2",
    )
    at_v2 = recipe_text("full", repo)
    assert "later" in at_v2 and "test_later" in at_v2

    git(repo, "checkout", "-q", first)
    assert recipe_text("full", repo) == v1_text
    assert "later" not in v1_text


def test_recipe_accepts_cwd_relative_paths_and_subdirectory_roots(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commit(repo, PROJECT, "v1")
    expected = recipe_text("callers", repo)
    monkeypatch.chdir(repo / "src")
    text = build_context(
        "callers",
        unit_id="ring.c::rb_push",
        source_text=(repo / "src/ring.c").read_text(),
        project_root=Path(),
    ).text
    assert text.replace("src/ring.c::rb_push", "X") == expected.replace(
        "src/ring.c::rb_push", "X"
    )


def test_project_recipes_fail_with_recipe_errors(
    repo: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    with pytest.raises(RecipeError, match="rev-parse"):
        recipe_text("callers", repo, source=RING_C)  # no commit yet
    commit(repo, PROJECT, "v1")
    with pytest.raises(RecipeError, match="no definition of 'nope'"):
        build_context(
            "callers",
            unit_id=f"{repo / 'src/ring.c'}::nope",
            source_text=RING_C,
            project_root=repo,
        )
    outside = tmp_path_factory.mktemp("outside")
    (outside / "x.c").write_text("int f(void) { return 0; }\n")
    with pytest.raises(RecipeError, match="rev-parse"):
        build_context(
            "callers",
            unit_id=f"{outside / 'x.c'}::f",
            source_text="int f(void) { return 0; }\n",
            project_root=repo,
        )


def test_the_repository_is_the_unit_files_not_the_project_roots(
    repo: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    commit(repo, PROJECT, "v1")
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    path = repo / "src/ring.c"
    text = build_context(
        "callers",
        unit_id=f"{path}::rb_push",
        source_text=RING_C,
        project_root=elsewhere,
    ).text
    assert "src/app.c::enqueue_all" in text


def test_a_symlinked_unit_file_keeps_its_own_path(repo: Path) -> None:
    commit(repo, PROJECT, "v1")
    (repo / "src/link.c").symlink_to("ring.c")
    git(repo, "add", "--", "src/link.c")
    git(repo, "commit", "-q", "-m", "link")
    text = build_context(
        "callers",
        unit_id=f"{repo / 'src/link.c'}::rb_push",
        source_text=RING_C + "/* via link */\n",
        project_root=repo,
    ).text
    assert "src/link.c::rb_push" in text
    assert "src/ring.c::rb_push" not in text


def test_overlay_tree_replaces_and_adds_files() -> None:
    tree = OverlayTree(
        MappingTree({"a.c": "old", "b.c": "b"}), {"a.c": "new", "c.c": "c"}
    )
    assert tree.paths() == ["a.c", "b.c", "c.c"]
    assert [tree.read(p) for p in tree.paths()] == ["new", "b", "c"]


def test_recipes_do_not_read_a_store(repo: Path) -> None:
    commit(repo, PROJECT, "v1")
    assert "accepted:" not in recipe_text("full", repo)
    assert not (repo / ".forseti").exists()


def test_store_contracts_maps_tree_paths_to_the_stores_spelling(
    tmp_path: Path,
) -> None:
    root, cwd = tmp_path / "proj", tmp_path / "proj" / "sub"
    cwd.mkdir(parents=True)
    spellings = {
        "src/a.c": "by_tree_path",
        "../src/a.c": "by_cwd_path",
        str(root / "src/a.c"): "by_absolute_path",
    }
    with PropertyStore(sqlite3.connect(":memory:")) as store:
        for spelling, expression in spellings.items():
            unit = f"{spelling}::f"
            store.add(
                Property(
                    property_id=make_property_id(
                        unit, PropertyKind.SEMANTIC, expression, ()
                    ),
                    unit_id=unit,
                    kind=PropertyKind.SEMANTIC,
                    expression=expression,
                    status=PropertyStatus.ACCEPTED,
                    provenance=Provenance("p", "1"),
                )
            )
        found = store_contracts(store, root=root, cwd=cwd)("src/a.c::f")
    assert sorted(found) == sorted(spellings.values())


def test_a_project_recipe_flows_into_the_proposal_request(repo: Path) -> None:
    commit(repo, PROJECT, "v1")
    request = prepare_proposal_request(
        repo / "src/ring.c", function="rb_push", recipe="callers", project_root=repo
    )
    assert request.context is not None
    assert request.context.recipe == "callers/v1"
    assert "src/app.c::enqueue_all" in request.context.text
