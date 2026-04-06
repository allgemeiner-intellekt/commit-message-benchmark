from cmb.categorize import categorize, infer_change_type
from cmb.dataset import passes_filters

FILTERS = {
    "min_files": 1,
    "max_files": 30,
    "min_patch_chars": 200,
    "max_patch_chars": 20000,
    "min_message_chars": 10,
    "drop_message_patterns": ["^Merge ", "^Bump "],
}


def _meta(**kw):
    base = {
        "sha": "a" * 40,
        "parent_sha": "b" * 40,
        "message": "feat(api): add v2 endpoint",
        "files": ["src/api.py"],
        "additions": 12,
        "deletions": 1,
        "patch_chars": 500,
    }
    base.update(kw)
    return base


def test_categorize_basic():
    assert categorize("src/foo/bar.py") == "source"
    assert categorize("tests/test_x.py") == "test"
    assert categorize("docs/index.md") == "docs"
    assert categorize("package-lock.json") == "lock"
    assert categorize("dist/app.min.js") == "generated"
    assert categorize("config/settings.yaml") == "config"


def test_infer_change_type_from_cc_prefix():
    assert infer_change_type("feat(api): add x", []) == "feat"
    assert infer_change_type("fix: handle null", []) == "fix"
    assert infer_change_type("docs: typo", []) == "docs"
    assert infer_change_type("perf!: rewrite hot loop", []) == "refactor"


def test_infer_change_type_falls_back_to_files():
    assert infer_change_type("misc updates", ["docs/a.md", "README.md"]) == "docs"
    assert infer_change_type("misc updates", ["tests/test_a.py"]) == "test"


def test_passes_filters_happy_path():
    assert passes_filters(_meta(), FILTERS)


def test_passes_filters_drops_merge_message():
    assert not passes_filters(_meta(message="Merge pull request #42"), FILTERS)


def test_passes_filters_drops_oversized_patch():
    assert not passes_filters(_meta(patch_chars=999_999), FILTERS)


def test_passes_filters_drops_lockfile_only():
    assert not passes_filters(_meta(files=["package-lock.json"]), FILTERS)


def test_passes_filters_drops_short_message():
    assert not passes_filters(_meta(message="x"), FILTERS)


def test_passes_filters_drops_too_many_files():
    assert not passes_filters(_meta(files=[f"f{i}.py" for i in range(50)]), FILTERS)
