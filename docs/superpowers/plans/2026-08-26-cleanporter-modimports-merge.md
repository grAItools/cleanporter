# cleanporter / modimports Merge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Merge two §2.2 enforcement tools into one, taking `modimports` as the code base and porting `cleanporter`'s fixer safety model onto it, under the `cleanporter` name.

**Architecture:** `modimports`' modules move into `src/cleanporter/` and become the skeleton: visitor-based import collection, a stdlib-only out-of-process classifier probe, filesystem-first classification, and `FlattenSentinel` line replacement. `cleanporter` contributes a `[tool.cleanporter]` config layer, an argparse CLI with 0/1/2 exit codes, filesystem extensions to `ModuleMap`, and — the substantial part — an all-or-nothing rewrite gate plus a set of standalone whole-file safety guards.

**Tech Stack:** Python >= 3.10, libcst >= 1.1, pytest, hatchling. No Typer after Task 5.

**Spec:** `docs/superpowers/specs/2026-08-26-cleanporter-modimports-merge.md`

## Global Constraints

- Python floor is `>=3.10`. Do not use `match` statements or 3.12-only syntax.
- The only runtime dependency is `libcst>=1.1`. Typer is removed in Task 5 and must not be reintroduced.
- `src/cleanporter/_probe.py` must import nothing outside the standard library and nothing from `cleanporter`. It runs inside the *target* interpreter.
- The probe imports the parent module only. It must never import the leaf name and never import an object.
- Never guess. An import that cannot be classified is reported (CP002) and never rewritten.
- Finding codes: `CP001` = `Status.VIOLATION`, `CP002` = `Status.UNRESOLVED`, `CP003` = `Status.SKIPPED`.
- Exit codes: `0` clean, `1` violations remain, `2` operational error.
- Every task ends with a commit. Run the full suite (`uv run pytest`) before each commit, not just the new test.

## File Structure

Final layout of `src/cleanporter/`:

| File | Responsibility | Origin |
| --- | --- | --- |
| `__init__.py` | public API re-exports | modimports |
| `__main__.py` | `python -m cleanporter` | either |
| `model.py` | `Status`, `Kind`, `Finding`, code mapping | modimports + new `Kind` |
| `config.py` | `[tool.cleanporter]` loading, exemptions, validation | cleanporter + modimports |
| `discover.py` | path expansion, exclude globs, skip dirs | cleanporter (new file) |
| `_imports.py` | libcst import-node helpers | modimports |
| `_bindings.py` | `ast`-based top-level binding extraction | cleanporter (`_top_level_bindings`) |
| `firstparty.py` | `ModuleMap`: filesystem first-party classification | modimports + cleanporter |
| `_probe.py` | stdlib-only interpreter classifier | modimports |
| `resolver.py` | layered resolution, batching, reasons | modimports |
| `analyze.py` | `FileRecord`, `iter_units`, `analyze_record` | modimports |
| `guards.py` | whole-file safety predicates | cleanporter (new file) |
| `rewrite.py` | the fixer: planning, guards, all-or-nothing gate | modimports + cleanporter |
| `cli.py` | argparse entry point, reporting, exit codes | cleanporter |

Tests mirror this: `tests/test_config.py`, `tests/test_discover.py`, `tests/test_firstparty.py`, `tests/test_probe.py`, `tests/test_analyze.py`, `tests/test_guards.py`, `tests/test_rewrite.py`, `tests/test_cli.py`, `tests/test_perf.py`.

---

### Task 1: Baseline commit and graft `modimports` as `cleanporter`

**Files:**
- Create: `.gitignore`
- Create: `src/cleanporter/{__init__,__main__,model,config,_imports,firstparty,_probe,resolver,analyze,rewrite,cli}.py`, `src/cleanporter/py.typed`
- Delete: `src/cleanporter/{checker,fixer}.py` and the old `resolver.py`, `config.py`, `cli.py`
- Create: `tests/fixtures/pkg/__init__.py`, `tests/fixtures/pkg/sub/__init__.py`, `tests/fixtures/pkg/sub/mod.py`
- Create: `tests/test_analyze.py`, `tests/test_probe.py`, `tests/test_traversal.py`
- Delete: `tests/{test_checker,test_fixer,test_resolver,test_cli,conftest}.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: nothing.
- Produces: the entire module surface later tasks build on. Key names:
  `model.Status` (`VIOLATION` / `UNRESOLVED` / `SKIPPED`), `model.Finding(path, line, column, parent, name, status, detail="")` with `.code -> str` and `.format() -> str`;
  `config.Config(exempt_modules, exempt_names, python, extra_roots)` with `.is_exempt(parent, name) -> bool`;
  `firstparty.ModuleMap.from_paths(paths) -> ModuleMap` with `.classify(parent, name) -> bool | None`, `.is_first_party(dotted) -> bool`, `.qualname_for(path) -> str | None`;
  `resolver.Resolver(module_map, python=None)` with `.is_module(parent, name) -> bool | None` and `.warm(pairs) -> None`;
  `analyze.FileRecord(path, source, tree, base_pkg)`, `analyze.iter_units(tree, base_pkg)`, `analyze.analyze_record(rec, resolver, config) -> list[Finding]`, `analyze.build(paths, config) -> tuple[list[FileRecord], Resolver, list[Finding]]`;
  `rewrite.fix_record(rec, resolver, config) -> tuple[str, int]`.

This task is a mechanical graft plus a rename. The only behavioural change is the `Status` member names and the code mapping.

- [ ] **Step 1: Commit both trees as-is, so the merge is diffable**

```bash
cd /home/enriqueg/Projects/grAItools/cleanporter
cat > .gitignore <<'EOF'
__pycache__/
*.py[cod]
.venv/
.pytest_cache/
.ruff_cache/
.mypy_cache/
dist/
build/
EOF
git add -A
git commit -m "chore: baseline — cleanporter and modimports as written, pre-merge"
```

- [ ] **Step 2: Move the modimports sources into the cleanporter package**

```bash
git rm -q src/cleanporter/checker.py src/cleanporter/fixer.py \
          src/cleanporter/resolver.py src/cleanporter/config.py \
          src/cleanporter/cli.py src/cleanporter/__init__.py \
          src/cleanporter/__main__.py
git rm -q -r tests
mkdir -p tests/fixtures
cp 3rdparty/modimports/src/modimports/*.py src/cleanporter/
cp 3rdparty/modimports/src/modimports/py.typed src/cleanporter/
cp -r 3rdparty/modimports/tests/fixtures/pkg tests/fixtures/pkg
touch tests/__init__.py
# rewrite intra-package imports and the public name
sed -i 's/^from modimports/from cleanporter/; s/^import modimports/import cleanporter/' src/cleanporter/*.py
sed -i 's/modimports: enforce/cleanporter: enforce/' src/cleanporter/__init__.py
```

- [ ] **Step 3: Rename the status members and remap the codes**

Replace the `Status` enum and `Finding.code` in `src/cleanporter/model.py`:

```python
class Status(enum.Enum):
    #: ``NAME`` is an object imported by name -> fixable violation.
    VIOLATION = "violation"
    #: Could not classify (parent not importable, ambiguous, ...) -> never fixed.
    UNRESOLVED = "unresolved"
    #: Structurally a violation but deliberately not rewritten.
    SKIPPED = "skipped"


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    column: int
    parent: str
    name: str
    status: Status
    detail: str = ""

    @property
    def code(self) -> str:
        return {
            Status.VIOLATION: "CP001",
            Status.UNRESOLVED: "CP002",
            Status.SKIPPED: "CP003",
        }[self.status]

    def format(self) -> str:
        loc = f"{self.path}:{self.line}:{self.column}"
        if self.status is Status.VIOLATION:
            token = self.parent.rsplit(".", 1)[-1]
            msg = (
                f"imports object '{self.name}' from module '{self.parent}'; "
                f"import the module and use '{token}.{self.name}'"
            )
        elif self.status is Status.UNRESOLVED:
            msg = f"could not determine whether '{self.parent}.{self.name}' is a module: {self.detail}"
        else:
            subject = "file" if self.name == "?" else f"'{self.name}' from '{self.parent}'"
            msg = f"{subject} not rewritten: {self.detail}"
        return f"{loc}: {self.code} {msg}"
```

Then fix the two call sites that used the old names:

```bash
sed -i 's/Status\.UNKNOWN/Status.UNRESOLVED/g; s/Status\.UNFIXABLE/Status.SKIPPED/g' \
    src/cleanporter/*.py
```

- [ ] **Step 4: Point packaging at the new package**

In `pyproject.toml` set `requires-python = ">=3.10"`, `dependencies = ["libcst>=1.1", "typer>=0.12"]`, `[project.scripts] cleanporter = "cleanporter.cli:main"`, and `[tool.hatch.build.targets.wheel] packages = ["src/cleanporter"]`. Keep `name = "cleanporter"`. Add:

```toml
[tool.ruff]
line-length = 100
target-version = "py310"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM"]
ignore = ["E501"]

[tool.ruff.lint.per-file-ignores]
"src/cleanporter/cli.py" = ["B008"]

[tool.mypy]
python_version = "3.10"
strict = true
```

- [ ] **Step 5: Port the modimports tests under the new package name**

```bash
cp 3rdparty/modimports/tests/test_analyze_fix.py tests/test_analyze.py
cp 3rdparty/modimports/tests/test_probe.py tests/test_probe.py
sed -i 's/from modimports/from cleanporter/g' tests/test_analyze.py tests/test_probe.py
sed -i 's/Status\.UNKNOWN/Status.UNRESOLVED/g; s/Status\.UNFIXABLE/Status.SKIPPED/g' tests/test_analyze.py
sed -i 's/"unfixable", "unknown"/"skipped", "unresolved"/' tests/test_analyze.py
```

- [ ] **Step 6: Write the traversal regression tests**

These lock in the three shapes that crashed or were silently mishandled in the old cleanporter. Create `tests/test_traversal.py`:

```python
"""Traversal shapes that the old hand-rolled statement walker got wrong."""

from __future__ import annotations

from pathlib import Path

import libcst as cst

from cleanporter.analyze import FileRecord, analyze_record, package_of
from cleanporter.config import Config
from cleanporter.firstparty import ModuleMap
from cleanporter.model import Status
from cleanporter.resolver import Resolver
from cleanporter.rewrite import fix_record

FIXTURES = Path(__file__).parent / "fixtures"


def _prepare(source: str):
    path = FIXTURES / "pkg" / "a.py"
    mm = ModuleMap.from_paths([FIXTURES / "pkg", path])
    resolver = Resolver(mm)
    rec = FileRecord(path, source, cst.parse_module(source), package_of(path, mm))
    from cleanporter.analyze import iter_units

    resolver.warm([(u.parent, u.name) for u in iter_units(rec.tree, rec.base_pkg)
                   if u.parent and not u.star])
    return rec, resolver


def _analyze(source: str):
    rec, resolver = _prepare(source)
    return analyze_record(rec, resolver, Config())


def _fix(source: str) -> str:
    rec, resolver = _prepare(source)
    new_source, _ = fix_record(rec, resolver, Config())
    return new_source


def test_elif_body_does_not_crash_and_is_reported():
    src = (
        "import sys\n"
        "if sys.argv:\n"
        "    pass\n"
        "elif len(sys.argv) > 1:\n"
        "    from pkg.sub.mod import Thing\n"
    )
    findings = _analyze(src)
    assert [f.status for f in findings] == [Status.VIOLATION]
    assert findings[0].line == 5


def test_one_liner_suite_is_reported_but_not_rewritten():
    src = "if True: from pkg.sub.mod import Thing\n"
    assert [f.status for f in _analyze(src)] == [Status.VIOLATION]
    assert _fix(src) == src


def test_semicolon_joined_line_is_reported_but_not_rewritten():
    src = 'from pkg.sub.mod import Thing; mod = "oops"\n'
    assert [f.status for f in _analyze(src)] == [Status.VIOLATION]
    assert _fix(src) == src


def test_async_and_nested_scopes_are_reported():
    src = (
        "async def a():\n"
        "    from pkg.sub.mod import Thing\n"
        "    return Thing\n"
        "\n"
        "class C:\n"
        "    from pkg.sub.mod import Thing as T2\n"
    )
    assert [f.status for f in _analyze(src)] == [Status.VIOLATION, Status.VIOLATION]
```

- [ ] **Step 7: Run the whole suite**

Run: `uv sync --dev && uv run pytest -q`
Expected: PASS — 15 ported tests plus 4 traversal tests = 19 passing.

If `test_one_liner_suite_is_reported_but_not_rewritten` or the semicolon test fails, the cause is `rewrite._import_lines`, which must keep its `len(line.body) == 1` and `SimpleStatementLine` conditions. Those two conditions are load-bearing safety properties, not incidental — do not relax them.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "refactor: adopt modimports architecture as the cleanporter package

Takes modimports as the code base per the merge spec. Renames the package,
maps Status onto cleanporter's CP001/CP002/CP003 codes, and adds regression
tests for the traversal shapes the previous hand-rolled walker crashed on."
```

---

### Task 2: One tree traversal per file

**Files:**
- Modify: `src/cleanporter/analyze.py`
- Test: `tests/test_perf.py`

**Interfaces:**
- Consumes: `analyze.FileRecord`, `analyze.iter_units`, `analyze.analyze_record` from Task 1.
- Produces: `FileRecord.units -> list[ImportUnit]` and `FileRecord.positions -> Mapping[cst.CSTNode, CodeRange]`, both lazily computed once per record. `iter_units(tree, base_pkg)` keeps its signature and stays the uncached primitive. `analyze_record` and `collect_pairs` read `rec.units` / `rec.positions`.

Profiling `analyze.build` + `analyze_record` over 297 files showed 891 full-tree visits — `collect_pairs` walks each tree, `analyze_record` walks it again, and `analyze_record` resolves `PositionProvider` on a fresh `MetadataWrapper` every call. `cli.fix` then calls `analyze_record` a second time, making it four.

- [ ] **Step 1: Write the failing test**

Create `tests/test_perf.py`:

```python
"""The analysis path must not re-walk a file's tree."""

from __future__ import annotations

from pathlib import Path

import libcst as cst

from cleanporter.analyze import FileRecord, analyze_record, collect_pairs, package_of
from cleanporter.config import Config
from cleanporter.firstparty import ModuleMap
from cleanporter.resolver import Resolver

FIXTURES = Path(__file__).parent / "fixtures"

SOURCE = (
    "from pkg.sub.mod import Thing\n"
    "from pkg.sub import mod\n"
    "x = Thing()\n"
)


def _record() -> FileRecord:
    path = FIXTURES / "pkg" / "a.py"
    mm = ModuleMap.from_paths([FIXTURES / "pkg", path])
    return FileRecord(path, SOURCE, cst.parse_module(SOURCE), package_of(path, mm))


def test_units_are_computed_once_and_cached():
    rec = _record()
    assert rec.units is rec.units
    assert [u.name for u in rec.units] == ["Thing", "mod"]


def test_positions_are_computed_once_and_cached():
    rec = _record()
    assert rec.positions is rec.positions


def test_repeated_analysis_does_not_rewalk_the_tree(monkeypatch):
    import cleanporter.analyze as analyze_mod

    rec = _record()
    mm = ModuleMap.from_paths([FIXTURES / "pkg", rec.path])
    resolver = Resolver(mm)
    resolver.warm(collect_pairs([rec]))

    calls = {"n": 0}
    real = analyze_mod.iter_units

    def counting(tree, base_pkg):
        calls["n"] += 1
        return real(tree, base_pkg)

    monkeypatch.setattr(analyze_mod, "iter_units", counting)

    analyze_record(rec, resolver, Config())
    analyze_record(rec, resolver, Config())
    assert calls["n"] == 0, "analyze_record must read the cached rec.units"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_perf.py -v`
Expected: FAIL — `AttributeError: 'FileRecord' object has no attribute 'units'`.

- [ ] **Step 3: Cache the units and the position map on the record**

Replace `FileRecord` in `src/cleanporter/analyze.py`:

```python
@dataclass
class FileRecord:
    path: Path
    source: str
    tree: cst.Module
    base_pkg: str
    _units: list[ImportUnit] | None = field(default=None, repr=False, compare=False)
    _positions: object | None = field(default=None, repr=False, compare=False)

    @property
    def units(self) -> list[ImportUnit]:
        """Every ``from`` import in the file. Computed once."""
        if self._units is None:
            self._units = list(iter_units(self.tree, self.base_pkg))
        return self._units

    @property
    def positions(self):  # type: ignore[no-untyped-def]
        """``PositionProvider`` mapping for this tree. Resolved once."""
        if self._positions is None:
            self._positions = MetadataWrapper(
                self.tree, unsafe_skip_copy=True
            ).resolve(PositionProvider)
        return self._positions
```

Add `field` to the `dataclasses` import. Then make the two consumers read the cache:

```python
def collect_pairs(records: list[FileRecord]) -> list[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for rec in records:
        for unit in rec.units:
            if unit.parent and not unit.star:
                pairs.add((unit.parent, unit.name))
    return sorted(pairs)


def analyze_record(rec: FileRecord, resolver: Resolver, config: Config) -> list[Finding]:
    positions = rec.positions
    findings: list[Finding] = []
    for unit in rec.units:
        pos = positions[unit.node].start
        line, col = pos.line, pos.column
        ...  # body unchanged from here
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -q`
Expected: PASS — 22 tests.

- [ ] **Step 5: Confirm the speedup on a real tree**

```bash
rm -rf /tmp/bench && mkdir -p /tmp/bench
cp -r "$(uv run python -c 'import libcst,pathlib;print(pathlib.Path(libcst.__file__).parent)')" /tmp/bench/
printf '[project]\nname="b"\nversion="0"\n' > /tmp/bench/pyproject.toml
time uv run python -m cleanporter check /tmp/bench
```

Expected: same violation count as before the change, measurably faster. Record both numbers in the commit message.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "perf: compute each file's units and positions once

analyze_record re-walked every tree and resolved PositionProvider on a fresh
MetadataWrapper per call, so a run made three to four full visits per file.
Cache both on FileRecord."
```

---

### Task 3: `[tool.cleanporter]` configuration

**Files:**
- Modify: `src/cleanporter/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `config.ConfigError(ValueError)`; `config.find_pyproject(start: Path) -> Path | None`; `config.load_config(start: Path) -> Config`; and an extended `Config` frozen dataclass with fields `root: Path`, `exclude: tuple[str, ...]`, `scope: str`, `source_roots: tuple[str, ...]`, `treat_unresolved_as_error: bool`, `exempt_modules: frozenset[str]`, `exempt_names: frozenset[str]`, `python: str | None`. All fields have defaults, so `Config()` still constructs. `Config.is_exempt(parent, name)` is unchanged. The old `extra_roots` field is renamed to `source_roots`.

Note `tomllib` is 3.11+; the floor is 3.10, so import it with a `tomli` fallback.

- [ ] **Step 1: Write the failing test**

Create `tests/test_config.py`:

```python
"""[tool.cleanporter] loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from cleanporter.config import Config, ConfigError, find_pyproject, load_config


def _project(tmp_path: Path, table: str = "") -> Path:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0"\n' + table, encoding="utf-8"
    )
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    return tmp_path


def test_defaults_when_no_table(tmp_path):
    cfg = load_config(_project(tmp_path))
    assert cfg.root == tmp_path
    assert cfg.exclude == ()
    assert cfg.scope == "all"
    assert cfg.treat_unresolved_as_error is False
    assert "typing" in cfg.exempt_modules


def test_defaults_when_no_pyproject_at_all(tmp_path):
    cfg = load_config(tmp_path)
    assert cfg.root == tmp_path
    assert cfg.scope == "all"


def test_search_walks_upward_from_a_file(tmp_path):
    _project(tmp_path)
    deep = tmp_path / "pkg" / "deep" / "mod.py"
    deep.parent.mkdir(parents=True)
    deep.write_text("", encoding="utf-8")
    assert find_pyproject(deep) == tmp_path / "pyproject.toml"
    assert load_config(deep).root == tmp_path


def test_reads_every_key(tmp_path):
    cfg = load_config(_project(tmp_path, """
[tool.cleanporter]
exclude = ["tests/", "src/generated_*.py"]
scope = "first-party"
treat_unresolved_as_error = true
source_roots = ["src"]
exempt_modules = ["attrs"]
exempt_names = ["annotations"]
python = "/usr/bin/python3"
"""))
    assert cfg.exclude == ("tests/", "src/generated_*.py")
    assert cfg.scope == "first-party"
    assert cfg.treat_unresolved_as_error is True
    assert cfg.source_roots == ("src",)
    assert cfg.python == "/usr/bin/python3"
    assert cfg.exempt_names == frozenset({"annotations"})


def test_exempt_modules_extends_rather_than_replaces_defaults(tmp_path):
    cfg = load_config(_project(tmp_path, '[tool.cleanporter]\nexempt_modules = ["attrs"]\n'))
    assert "attrs" in cfg.exempt_modules
    assert "typing" in cfg.exempt_modules
    assert cfg.is_exempt("attrs.validators", "instance_of") is True
    assert cfg.is_exempt("collections", "OrderedDict") is False


@pytest.mark.parametrize(
    "table, message",
    [
        ('[tool.cleanporter]\nexclude = "tests/"\n', "must be a list of strings"),
        ('[tool.cleanporter]\nscope = "mine"\n', "must be one of"),
        ('[tool.cleanporter]\ntreat_unresolved_as_error = "yes"\n', "must be a boolean"),
        ('[tool.cleanporter]\nnonsense = 1\n', "unknown"),
    ],
)
def test_malformed_config_raises(tmp_path, table, message):
    with pytest.raises(ConfigError, match=message):
        load_config(_project(tmp_path, table))


def test_plain_config_still_constructs_with_no_arguments():
    assert Config().scope == "all"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL — `ImportError: cannot import name 'ConfigError'`.

- [ ] **Step 3: Write the implementation**

Replace `src/cleanporter/config.py` entirely:

```python
"""Configuration: exemptions, enforcement scope, and file selection.

The Google style guide explicitly exempts ``typing``; the default allowlist
extends that to the other places where importing names is idiomatic and
blessed (``typing_extensions``, ``collections.abc``, ``__future__``).
Everything is configurable under ``[tool.cleanporter]`` in the nearest
``pyproject.toml``.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - 3.10 only
    import tomli as tomllib

# Modules whose members may be imported directly by name.
DEFAULT_EXEMPT_MODULES: frozenset[str] = frozenset(
    {"typing", "typing_extensions", "collections.abc", "__future__"}
)

_VALID_SCOPES = ("all", "first-party")

_LIST_KEYS = ("exclude", "source_roots", "exempt_modules", "exempt_names")
_BOOL_KEYS = ("treat_unresolved_as_error",)
_KNOWN_KEYS = frozenset(_LIST_KEYS + _BOOL_KEYS + ("scope", "python"))


class ConfigError(ValueError):
    """Raised when [tool.cleanporter] is malformed."""


@dataclass(frozen=True)
class Config:
    #: Directory of the pyproject.toml this config came from (or cwd).
    root: Path = field(default_factory=Path.cwd)
    #: Glob patterns matched against project-relative POSIX paths.
    exclude: tuple[str, ...] = ()
    #: ``all`` or ``first-party``.
    scope: str = "all"
    #: Explicit first-party import roots, relative to ``root``.
    source_roots: tuple[str, ...] = ()
    #: Count CP002 findings toward the failure exit code.
    treat_unresolved_as_error: bool = False
    #: ``from MODULE import X`` is allowed when MODULE is in this set.
    exempt_modules: frozenset[str] = DEFAULT_EXEMPT_MODULES
    #: Individual bound names that are always allowed.
    exempt_names: frozenset[str] = frozenset()
    #: Interpreter used for the stdlib/third-party probe (None -> current).
    python: str | None = None

    def is_exempt(self, parent: str, name: str) -> bool:
        if name in self.exempt_names:
            return True
        # Exempt if the from-module or any ancestor is exempted.
        parts = parent.split(".")
        return any(".".join(parts[:i]) in self.exempt_modules for i in range(len(parts), 0, -1))


def _str_list(table: dict[str, object], key: str) -> tuple[str, ...]:
    value = table[key]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"tool.cleanporter.{key} must be a list of strings")
    return tuple(value)


def _parse_table(table: dict[str, object], root: Path) -> Config:
    unknown = set(table) - _KNOWN_KEYS
    if unknown:
        raise ConfigError(f"unknown tool.cleanporter keys: {sorted(unknown)}")

    kwargs: dict[str, object] = {"root": root}
    if "exclude" in table:
        kwargs["exclude"] = _str_list(table, "exclude")
    if "source_roots" in table:
        kwargs["source_roots"] = _str_list(table, "source_roots")
    if "exempt_modules" in table:
        kwargs["exempt_modules"] = DEFAULT_EXEMPT_MODULES | frozenset(
            _str_list(table, "exempt_modules")
        )
    if "exempt_names" in table:
        kwargs["exempt_names"] = frozenset(_str_list(table, "exempt_names"))
    if "scope" in table:
        value = table["scope"]
        if not isinstance(value, str) or value not in _VALID_SCOPES:
            raise ConfigError(
                f"tool.cleanporter.scope must be one of {_VALID_SCOPES}, got {value!r}"
            )
        kwargs["scope"] = value
    if "python" in table:
        value = table["python"]
        if not isinstance(value, str):
            raise ConfigError("tool.cleanporter.python must be a string")
        kwargs["python"] = value
    for key in _BOOL_KEYS:
        if key in table:
            value = table[key]
            if not isinstance(value, bool):
                raise ConfigError(f"tool.cleanporter.{key} must be a boolean")
            kwargs[key] = value
    return Config(**kwargs)  # type: ignore[arg-type]


def find_pyproject(start: Path) -> Path | None:
    """Walk upward from *start* looking for a pyproject.toml."""
    current = start.resolve()
    if current.is_file():
        current = current.parent
    while True:
        candidate = current / "pyproject.toml"
        if candidate.is_file():
            return candidate
        parent = current.parent
        if parent == current:
            return None
        current = parent


def load_config(start: Path) -> Config:
    """Load configuration walking upward from *start*; defaults if absent."""
    anchor = start.resolve()
    pyproject = find_pyproject(anchor)
    if pyproject is None:
        return Config(root=anchor if anchor.is_dir() else anchor.parent)
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    table = data.get("tool", {}).get("cleanporter", {})
    if not isinstance(table, dict):
        raise ConfigError("[tool.cleanporter] must be a TOML table")
    return _parse_table(table, pyproject.parent)
```

- [ ] **Step 4: Rename the one existing consumer of `extra_roots`**

`analyze.build` reads `config.extra_roots`. Change that line to `config.source_roots`, and resolve entries against the config root:

```python
    roots = [config.root / r for r in config.source_roots]
```

- [ ] **Step 5: Add the 3.10 fallback dependency**

In `pyproject.toml`: `dependencies = ["libcst>=1.1", "typer>=0.12", "tomli>=2.0; python_version<'3.11'"]`.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest -q`
Expected: PASS — 32 tests.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat: load configuration from [tool.cleanporter] in pyproject.toml

Ports cleanporter's config layer onto the merged base: exclude globs, scope,
source_roots, treat_unresolved_as_error, and validation, alongside the
exemption keys the modimports Config already carried."
```

---

### Task 4: File discovery — exclude globs and skip directories

**Files:**
- Create: `src/cleanporter/discover.py`
- Modify: `src/cleanporter/analyze.py` (`build` calls the new expander; delete `expand`)
- Test: `tests/test_discover.py`

**Interfaces:**
- Consumes: `config.Config` from Task 3.
- Produces: `discover.iter_python_files(paths: list[Path], config: Config) -> tuple[list[Path], list[str]]` returning `(files, warnings)`. `discover.ALWAYS_SKIP_DIRS: frozenset[str]`. `analyze.build` calls it and returns the warnings as its third element alongside parse errors — signature becomes `build(paths, config) -> tuple[list[FileRecord], Resolver, list[Finding], list[str]]`.

Two defects are being fixed at once. modimports has no exclusion at all and happily lints `.venv`. The old cleanporter over-corrected: it dropped any path with a dot-prefixed component, so `cleanporter .venv/lib/python3.14/site-packages/libcst` reported "checked 0 file(s)" and exited 0. The rule that satisfies both: skip dot-directories and known junk directories **while walking**, but never reject a path the user named explicitly.

- [ ] **Step 1: Write the failing test**

Create `tests/test_discover.py`:

```python
"""Path expansion, exclusion, and skip directories."""

from __future__ import annotations

from pathlib import Path

from cleanporter.config import Config
from cleanporter.discover import iter_python_files


def _tree(tmp_path: Path) -> Path:
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "pkg" / "mod.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "pkg" / "skipme.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "pkg" / "__pycache__").mkdir()
    (tmp_path / "src" / "pkg" / "__pycache__" / "mod.py").write_text("", encoding="utf-8")
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "vendored.py").write_text("", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("", encoding="utf-8")
    return tmp_path


def _names(files: list[Path]) -> list[str]:
    return sorted(f.name for f in files)


def test_walk_collects_python_files_only(tmp_path):
    root = _tree(tmp_path)
    files, warnings = iter_python_files([root], Config(root=root))
    assert _names(files) == ["__init__.py", "mod.py", "skipme.py"]
    assert warnings == []


def test_walk_skips_dot_directories_and_pycache(tmp_path):
    root = _tree(tmp_path)
    files, _ = iter_python_files([root], Config(root=root))
    assert not any(".venv" in f.parts for f in files)
    assert not any("__pycache__" in f.parts for f in files)


def test_explicitly_named_dot_directory_is_still_scanned(tmp_path):
    root = _tree(tmp_path)
    files, _ = iter_python_files([root / ".venv"], Config(root=root))
    assert _names(files) == ["vendored.py"]


def test_explicitly_named_file_is_scanned_even_if_excluded(tmp_path):
    root = _tree(tmp_path)
    cfg = Config(root=root, exclude=("**/skipme.py",))
    files, _ = iter_python_files([root / "src" / "pkg" / "skipme.py"], cfg)
    assert _names(files) == ["skipme.py"]


def test_exclude_glob_applies_while_walking(tmp_path):
    root = _tree(tmp_path)
    cfg = Config(root=root, exclude=("**/skipme.py",))
    files, _ = iter_python_files([root], cfg)
    assert _names(files) == ["__init__.py", "mod.py"]


def test_literal_exclude_matches_a_directory_and_its_contents(tmp_path):
    root = _tree(tmp_path)
    cfg = Config(root=root, exclude=("src/pkg",))
    files, _ = iter_python_files([root], cfg)
    assert files == []


def test_results_are_deduplicated_and_sorted(tmp_path):
    root = _tree(tmp_path)
    target = root / "src" / "pkg" / "mod.py"
    files, _ = iter_python_files([target, root / "src", target], Config(root=root))
    assert len(files) == len({f.resolve() for f in files})
    assert files == sorted(files)


def test_missing_path_is_a_warning_not_a_crash(tmp_path):
    root = _tree(tmp_path)
    files, warnings = iter_python_files([root / "nope"], Config(root=root))
    assert files == []
    assert len(warnings) == 1 and "does not exist" in warnings[0]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_discover.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cleanporter.discover'`.

- [ ] **Step 3: Write the implementation**

Create `src/cleanporter/discover.py`:

```python
"""Expand command-line paths into the set of files to analyse.

Directories named on the command line are walked; dot-directories and known
build/cache directories are skipped during the walk. A path the user named
*explicitly* is never rejected -- pointing the tool at ``.venv/...`` or at an
excluded file is taken as deliberate.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

from .config import Config

ALWAYS_SKIP_DIRS = frozenset(
    {
        "__pycache__",
        "node_modules",
        "build",
        "dist",
        "site-packages",
    }
)


def _is_skipped_dir(name: str) -> bool:
    return name.startswith(".") or name in ALWAYS_SKIP_DIRS


def _excluded(path: Path, config: Config) -> bool:
    try:
        rel = path.resolve().relative_to(config.root.resolve()).as_posix()
    except ValueError:
        rel = path.as_posix()
    for pattern in config.exclude:
        if fnmatch.fnmatch(rel, pattern):
            return True
        if any(ch in pattern for ch in "*?["):
            continue
        literal = pattern.rstrip("/")
        if rel == literal or rel.startswith(literal + "/"):
            return True
    return False


def _walk(directory: Path, config: Config) -> list[Path]:
    found: list[Path] = []
    for child in sorted(directory.iterdir()):
        if child.is_dir():
            if _is_skipped_dir(child.name) or _excluded(child, config):
                continue
            found.extend(_walk(child, config))
        elif child.suffix == ".py" and not _excluded(child, config):
            found.append(child)
    return found


def iter_python_files(paths: list[Path], config: Config) -> tuple[list[Path], list[str]]:
    """Expand *paths* into a de-duplicated sorted file list plus warnings."""
    warnings: list[str] = []
    seen: set[Path] = set()
    out: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if not path.exists():
            warnings.append(f"path does not exist: {path}")
            continue
        # An explicitly named file bypasses every filter.
        candidates = [path] if path.is_file() else _walk(path, config)
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            out.append(candidate)
    return sorted(out), warnings
```

- [ ] **Step 4: Route `build` through it and delete the old expander**

In `src/cleanporter/analyze.py`, delete the `expand` function and change `build`:

```python
def build(
    paths: list[Path], config: Config
) -> tuple[list[FileRecord], Resolver, list[Finding], list[str]]:
    """Expand paths, parse files, build the resolver and warm its cache."""
    files, warnings = iter_python_files(paths, config)
    roots = [config.root / r for r in config.source_roots]
    module_map = ModuleMap.from_paths(files + roots)
    resolver = Resolver(module_map, python=config.python)

    records: list[FileRecord] = []
    errors: list[Finding] = []
    for f in files:
        source = f.read_text(encoding="utf-8")
        try:
            tree = cst.parse_module(source)
        except cst.ParserSyntaxError as exc:
            errors.append(
                Finding(f, exc.raw_line, exc.raw_column, "?", "?",
                        Status.UNRESOLVED, f"parse error: {exc.message}")
            )
            continue
        records.append(FileRecord(f, source, tree, package_of(f, module_map)))

    resolver.warm(collect_pairs(records))
    return records, resolver, errors, warnings
```

Add `from .discover import iter_python_files` at the top. Update the two unpack sites in `cli.py` to take four values.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest -q`
Expected: PASS — 40 tests.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: exclude globs and skip directories for path expansion

modimports had no exclusion and linted .venv; the old cleanporter rejected any
dot-prefixed path component, so explicitly naming a file under .venv silently
checked nothing. Skip while walking, never reject an explicit path."
```

---

### Task 5: argparse CLI with 0/1/2 exit codes; drop Typer

**Files:**
- Modify: `src/cleanporter/cli.py` (full rewrite), `src/cleanporter/__main__.py`
- Modify: `pyproject.toml` (remove `typer`, remove the `B008` per-file ignore)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `analyze.build`, `analyze.analyze_record`, `rewrite.fix_record`, `config.load_config`, `config.ConfigError`, `model.Status`.
- Produces: `cli.main(argv: list[str] | None = None) -> int` and `cli.build_arg_parser() -> argparse.ArgumentParser`. `main` returns the exit code rather than raising `SystemExit`, so tests can call it directly.

One command with `--fix`, replacing modimports' `check`/`fix` subcommands.

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli.py`:

```python
"""End-to-end CLI behaviour and exit codes."""

from __future__ import annotations

from pathlib import Path

import pytest

from cleanporter.cli import main


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    (tmp_path / "src" / "demo").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0"\n', encoding="utf-8"
    )
    (tmp_path / "src" / "demo" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "demo" / "helpers.py").write_text(
        "THING = 42\n", encoding="utf-8"
    )
    (tmp_path / "src" / "demo" / "consumer.py").write_text(
        "from demo.helpers import THING\ntotal = THING\n", encoding="utf-8"
    )
    return tmp_path


def test_check_reports_and_exits_1(project, capsys):
    rc = main([str(project / "src")])
    out = capsys.readouterr().out
    assert "consumer.py" in out and "CP001" in out
    assert rc == 1


def test_clean_tree_exits_0(project, capsys):
    (project / "src" / "demo" / "consumer.py").write_text(
        "from demo import helpers\ntotal = helpers.THING\n", encoding="utf-8"
    )
    assert main([str(project / "src")]) == 0


def test_fix_rewrites_and_exits_0(project, capsys):
    rc = main(["--fix", str(project / "src")])
    assert rc == 0
    assert (project / "src" / "demo" / "consumer.py").read_text(encoding="utf-8") == (
        "from demo import helpers\ntotal = helpers.THING\n"
    )
    assert "fixed" in capsys.readouterr().out


def test_diff_previews_without_writing(project, capsys):
    before = (project / "src" / "demo" / "consumer.py").read_text(encoding="utf-8")
    rc = main(["--diff", str(project / "src")])
    out = capsys.readouterr().out
    assert "-from demo.helpers import THING" in out
    assert (project / "src" / "demo" / "consumer.py").read_text(encoding="utf-8") == before
    assert rc == 1


def test_typing_imports_are_exempt(project, capsys):
    (project / "src" / "demo" / "consumer.py").write_text(
        "from typing import Any\nfrom collections.abc import Mapping\n"
        "x: Any = None\ny: Mapping = {}\n",
        encoding="utf-8",
    )
    assert main([str(project / "src")]) == 0


def test_exempt_flag_extends_the_allowlist(project, capsys):
    (project / "src" / "demo" / "consumer.py").write_text(
        "from demo.helpers import THING\n", encoding="utf-8"
    )
    assert main(["--exempt", "demo.helpers", str(project / "src")]) == 0


def test_exclude_config_is_respected(project, capsys):
    (project / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0"\n'
        '[tool.cleanporter]\nexclude = ["**/consumer.py"]\n',
        encoding="utf-8",
    )
    assert main([str(project / "src")]) == 0
    assert "consumer.py" not in capsys.readouterr().out


def test_syntax_error_exits_2(project, capsys):
    (project / "src" / "demo" / "broken.py").write_text("def (:\n", encoding="utf-8")
    assert main([str(project / "src")]) == 2


def test_bad_config_exits_2(project, capsys):
    (project / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0"\n'
        '[tool.cleanporter]\nscope = "nonsense"\n',
        encoding="utf-8",
    )
    assert main([str(project / "src")]) == 2
    assert "configuration error" in capsys.readouterr().err


def test_missing_path_warns_and_exits_0(project, capsys):
    rc = main([str(project / "nope")])
    captured = capsys.readouterr()
    assert "does not exist" in captured.out + captured.err
    assert rc == 0


def test_strict_promotes_unresolved_to_failure(project, capsys):
    (project / "src" / "demo" / "consumer.py").write_text(
        "from definitely_missing_pkg_xyz import thing\n", encoding="utf-8"
    )
    assert main([str(project / "src")]) == 0
    assert main(["--strict", str(project / "src")]) == 1
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL — `TypeError`, because the Typer `main()` takes no `argv`.

- [ ] **Step 3: Write the implementation**

Replace `src/cleanporter/cli.py` entirely:

```python
"""Command-line interface: check, and optionally fix, from-imports."""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

from . import __version__
from .analyze import analyze_record, build
from .config import ConfigError, Config, load_config
from .model import Finding, Status
from .rewrite import fix_record

_EXIT_OK = 0
_EXIT_VIOLATIONS = 1
_EXIT_ERROR = 2


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cleanporter",
        description=(
            "Check that 'from ... import ...' statements import modules only "
            "(Google Python Style Guide section 2.2) and optionally rewrite "
            "violations."
        ),
        epilog=(
            "examples:\n"
            "  cleanporter src/\n"
            "  cleanporter --diff src/\n"
            "  cleanporter --fix src/\n\n"
            "exit codes: 0 ok, 1 violations remain, 2 operational error.\n"
            "configure under [tool.cleanporter] in pyproject.toml."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("paths", nargs="*", default=["."],
                        help="files or directories to process")
    parser.add_argument("--fix", action="store_true",
                        help="rewrite violations in place where provably safe")
    parser.add_argument("--diff", action="store_true",
                        help="show the rewrite as a unified diff without writing")
    parser.add_argument("--python", default=None,
                        help="interpreter used to classify stdlib/third-party names")
    parser.add_argument("--exempt", action="append", default=[], metavar="MODULE",
                        help="additional module whose members may be imported by name")
    parser.add_argument("--root", action="append", default=[], metavar="PATH",
                        help="additional first-party import root")
    parser.add_argument("--strict", action="store_true",
                        help="also fail on imports that could not be classified")
    parser.add_argument("--version", action="version",
                        version=f"cleanporter {__version__}")
    return parser


def _apply_overrides(config: Config, args: argparse.Namespace) -> Config:
    from dataclasses import replace

    return replace(
        config,
        exempt_modules=config.exempt_modules | frozenset(args.exempt),
        source_roots=config.source_roots + tuple(args.root),
        python=args.python or config.python,
        treat_unresolved_as_error=config.treat_unresolved_as_error or args.strict,
    )


def run(args: argparse.Namespace) -> int:
    anchor = Path(args.paths[0]).resolve()
    try:
        config = _apply_overrides(load_config(anchor), args)
    except ConfigError as exc:
        print(f"cleanporter: configuration error: {exc}", file=sys.stderr)
        return _EXIT_ERROR

    paths = [Path(p) for p in args.paths]
    records, resolver, parse_errors, warnings = build(paths, config)
    for warning in warnings:
        print(f"cleanporter: warning: {warning}")

    findings: list[Finding] = list(parse_errors)
    changed = 0
    for rec in records:
        if args.fix or args.diff:
            outcome = fix_record(rec, resolver, config)
            if outcome.status == "fixed":
                changed += 1
                # Diff first: rec.source is still the original here.
                sys.stdout.writelines(
                    difflib.unified_diff(
                        rec.source.splitlines(keepends=True),
                        outcome.source.splitlines(keepends=True),
                        fromfile=f"a/{rec.path}",
                        tofile=f"b/{rec.path}",
                    )
                )
                if args.fix:
                    rec.path.write_text(outcome.source, encoding="utf-8")
                    print(f"fixed: {rec.path}")
                    # Report against what is now on disk.
                    rec = _reparse(rec, outcome.source, config)
            findings.extend(outcome.blockers)
        findings.extend(analyze_record(rec, resolver, config))

    findings.sort(key=lambda f: (str(f.path), f.line, f.column, f.code))
    for finding in findings:
        print(finding.format())

    violations = sum(f.status is Status.VIOLATION for f in findings)
    skipped = sum(f.status is Status.SKIPPED for f in findings)
    unresolved = sum(f.status is Status.UNRESOLVED and f.parent != "?" for f in findings)
    print()
    print(
        f"checked {len(records)} file(s)"
        + (f", fixed {changed}" if args.fix else "")
        + f": {violations} violation(s), {skipped} not rewritten, "
        f"{unresolved} unresolved"
    )

    if parse_errors:
        return _EXIT_ERROR
    hard = violations + skipped + (unresolved if config.treat_unresolved_as_error else 0)
    return _EXIT_VIOLATIONS if hard else _EXIT_OK


def _reparse(rec, source, config):
    import libcst as cst

    from .analyze import FileRecord

    return FileRecord(rec.path, source, cst.parse_module(source), rec.base_pkg)


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if not args.paths:
        args.paths = ["."]
    try:
        return run(args)
    except KeyboardInterrupt:  # pragma: no cover
        return _EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
```

Set `src/cleanporter/__main__.py` to:

```python
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
```

Add `__version__ = "0.2.0"` to `src/cleanporter/__init__.py` if it is not already there.

- [ ] **Step 4: Drop the Typer dependency**

In `pyproject.toml` set `dependencies = ["libcst>=1.1", "tomli>=2.0; python_version<'3.11'"]` and delete the `[tool.ruff.lint.per-file-ignores]` entry for `cli.py`. Then `uv sync --dev`.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest -q && uv run ruff check src tests`
Expected: PASS — 51 tests, ruff clean.

`test_fix_rewrites_and_exits_0` depends on `fix_record` still returning a 2-tuple at this point; it will be updated to `FixOutcome` in Task 8. Until then, adapt the `outcome` block in `run` to the tuple form and change it in Task 8. Simpler: implement `FixOutcome` here as a two-line shim so the CLI is written once —

```python
# rewrite.py, temporary until Task 8 fills it in
@dataclass
class FixOutcome:
    status: str
    source: str
    blockers: list[Finding] = field(default_factory=list)
    fixed: int = 0


def fix_record(rec, resolver, config) -> FixOutcome:
    wrapper = cst.MetadataWrapper(rec.tree, unsafe_skip_copy=True)
    fixer = _Fixer(rec, resolver, config)
    new_source = wrapper.visit(fixer).code
    status = "fixed" if new_source != rec.source and fixer._plan.fixed else "clean"
    return FixOutcome(status, new_source, [], fixer._plan.fixed)
```

Update the `_fix` helpers in `tests/test_analyze.py` and `tests/test_traversal.py` to `return fix_record(rec, resolver, Config()).source`.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: argparse CLI with 0/1/2 exit codes, drop Typer

One command with --fix/--diff replacing check/fix subcommands. libcst is now
the only runtime dependency on 3.11+."
```

---

### Task 6: First-party C-extension submodules

**Files:**
- Modify: `src/cleanporter/firstparty.py`
- Test: `tests/test_firstparty.py`

**Interfaces:**
- Consumes: `firstparty.ModuleMap` from Task 1.
- Produces: `firstparty.EXTENSION_SUFFIXES: frozenset[str]`. `ModuleMap.classify` keeps its current `bool | None` signature in this task; Task 7 changes it.

`ModuleMap._scan` only records `.py` files, so a first-party extension module is classified as an object. Verified consequence: `from amb import accel` where `accel.cpython-314-x86_64-linux-gnu.so` exists is reported as a violation and rewritten to `import amb` plus `amb.accel`, which raises `AttributeError` at runtime because `import amb` does not import the submodule.

- [ ] **Step 1: Write the failing test**

Create `tests/test_firstparty.py`:

```python
"""Filesystem classification of first-party packages."""

from __future__ import annotations

from pathlib import Path

from cleanporter.firstparty import ModuleMap


def _pkg(tmp_path: Path) -> Path:
    root = tmp_path / "src"
    (root / "amb").mkdir(parents=True)
    (root / "amb" / "__init__.py").write_text("", encoding="utf-8")
    (root / "amb" / "mod.py").write_text("Q = 1\n", encoding="utf-8")
    return root


def test_py_submodule_is_a_module(tmp_path):
    root = _pkg(tmp_path)
    mm = ModuleMap([root])
    assert mm.classify("amb", "mod") is True


def test_plain_object_is_not_a_module(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "__init__.py").write_text("Thing = object()\n", encoding="utf-8")
    mm = ModuleMap([root])
    assert mm.classify("amb", "Thing") is False


def test_extension_submodule_is_a_module(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "accel.cpython-314-x86_64-linux-gnu.so").touch()
    mm = ModuleMap([root])
    assert mm.classify("amb", "accel") is True


def test_windows_extension_submodule_is_a_module(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "fast.cp310-win_amd64.pyd").touch()
    mm = ModuleMap([root])
    assert mm.classify("amb", "fast") is True


def test_directory_holding_only_an_extension_is_a_package(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "native").mkdir()
    (root / "amb" / "native" / "core.abi3.so").touch()
    mm = ModuleMap([root])
    assert mm.classify("amb", "native") is True
    assert mm.classify("amb.native", "core") is True


def test_non_first_party_defers_to_the_probe(tmp_path):
    mm = ModuleMap([_pkg(tmp_path)])
    assert mm.classify("collections", "OrderedDict") is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_firstparty.py -v`
Expected: FAIL — three failures asserting `True`, got `False`.

- [ ] **Step 3: Write the implementation**

In `src/cleanporter/firstparty.py`, add the constant and a stem helper, then use them in `_is_pkg_dir` and `_scan`:

```python
#: Suffixes CPython will import as an extension module.
EXTENSION_SUFFIXES = frozenset({".so", ".pyd"})


def _module_stem(path: Path) -> str:
    """``accel.cpython-314-x86_64-linux-gnu.so`` -> ``accel``."""
    return path.name.split(".")[0]


def _is_importable_file(path: Path) -> bool:
    return path.suffix == ".py" or path.suffix in EXTENSION_SUFFIXES


def _is_pkg_dir(d: Path) -> bool:
    if (d / "__init__.py").is_file():
        return True
    # PEP 420 namespace package: a directory that contributes submodules.
    return d.is_dir() and any(
        _is_importable_file(c) or (c.is_dir() and c.name != "__pycache__")
        for c in d.iterdir()
    )
```

and in `ModuleMap._scan`:

```python
    def _scan(self, root: Path, directory: Path) -> None:
        for child in sorted(directory.iterdir()):
            if child.name == "__pycache__" or child.name.startswith("."):
                continue
            if child.is_dir() and _is_pkg_dir(child):
                self._packages.add(self._dotted(root, child))
                self._scan(root, child)
            elif _is_importable_file(child):
                stem = _module_stem(child)
                if stem and stem != "__init__":
                    self._modules.add(self._dotted(root, child.with_name(stem)))
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest -q`
Expected: PASS — 57 tests.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "fix: classify first-party C-extension submodules as modules

A .so/.pyd submodule was reported as an object and rewritten into an
attribute access on the parent package, which raises AttributeError because
importing a package does not import its extension submodules."
```

---

### Task 7: Report submodule / `__init__`-binding ambiguity instead of guessing

**Files:**
- Create: `src/cleanporter/_bindings.py`
- Modify: `src/cleanporter/model.py`, `src/cleanporter/firstparty.py`, `src/cleanporter/resolver.py`, `src/cleanporter/analyze.py`
- Test: `tests/test_firstparty.py` (extend), `tests/test_resolver.py` (create)

**Interfaces:**
- Consumes: `firstparty.ModuleMap`, `resolver.Resolver` from earlier tasks.
- Produces: `model.Kind` enum with members `MODULE`, `OBJECT`, `AMBIGUOUS`. `_bindings.top_level_bindings(path: str) -> frozenset[str]`, cached. `ModuleMap.classify(parent, name) -> Kind | None` (**breaking change** from `bool | None`; `None` still means "not first-party"). `Resolver.is_module` keeps `bool | None`. New `Resolver.reason(parent: str, name: str) -> str` returns the human explanation used as a CP002 `detail`.

When `pkg/mod.py` exists *and* `pkg/__init__.py` binds the name `mod`, the binding wins at import time, so `from pkg import mod` may well be an object. modimports answers "module" and misses the violation. Never guess: report CP002.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_firstparty.py`:

```python
from cleanporter.model import Kind


def test_submodule_shadowed_by_an_init_binding_is_ambiguous(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "__init__.py").write_text(
        'mod = "shadowing string, not the submodule"\n', encoding="utf-8"
    )
    mm = ModuleMap([root])
    assert mm.classify("amb", "mod") is Kind.AMBIGUOUS


def test_init_importing_its_own_submodule_is_not_ambiguous(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "__init__.py").write_text("from . import mod\n", encoding="utf-8")
    mm = ModuleMap([root])
    assert mm.classify("amb", "mod") is Kind.MODULE
```

Create `tests/test_resolver.py`:

```python
"""Layered resolution and the reasons attached to unresolved verdicts."""

from __future__ import annotations

from pathlib import Path

from cleanporter.firstparty import ModuleMap
from cleanporter.resolver import Resolver


def _pkg(tmp_path: Path) -> Path:
    root = tmp_path / "src"
    (root / "amb").mkdir(parents=True)
    (root / "amb" / "__init__.py").write_text("", encoding="utf-8")
    (root / "amb" / "mod.py").write_text("Q = 1\n", encoding="utf-8")
    return root


def test_first_party_module_and_object(tmp_path):
    r = Resolver(ModuleMap([_pkg(tmp_path)]))
    assert r.is_module("amb", "mod") is True
    assert r.is_module("amb", "Nope") is False


def test_stdlib_falls_through_to_the_probe(tmp_path):
    r = Resolver(ModuleMap([_pkg(tmp_path)]))
    assert r.is_module("os", "path") is True
    assert r.is_module("collections", "OrderedDict") is False


def test_ambiguous_is_unresolved_with_an_explanatory_reason(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "__init__.py").write_text('mod = "shadow"\n', encoding="utf-8")
    r = Resolver(ModuleMap([root]))
    assert r.is_module("amb", "mod") is None
    assert "both a submodule" in r.reason("amb", "mod")


def test_unimportable_parent_is_unresolved_with_its_own_reason(tmp_path):
    r = Resolver(ModuleMap([_pkg(tmp_path)]))
    assert r.is_module("definitely_missing_pkg_xyz", "thing") is None
    assert "not importable" in r.reason("definitely_missing_pkg_xyz", "thing")


def test_warm_batches_and_matches_individual_lookups(tmp_path):
    r = Resolver(ModuleMap([_pkg(tmp_path)]))
    pairs = [("amb", "mod"), ("collections", "OrderedDict"), ("os", "path")]
    r.warm(pairs)
    assert [r.is_module(p, n) for p, n in pairs] == [True, False, True]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_firstparty.py tests/test_resolver.py -v`
Expected: FAIL — `ImportError: cannot import name 'Kind'`.

- [ ] **Step 3: Add the `Kind` enum**

In `src/cleanporter/model.py`:

```python
class Kind(enum.Enum):
    """What ``PARENT.NAME`` resolves to, as far as the filesystem can tell."""

    MODULE = "module"
    OBJECT = "object"
    #: Both a submodule on disk and a top-level binding in the parent's
    #: ``__init__``. The binding wins at import time, so this cannot be
    #: decided statically -- report it, never guess.
    AMBIGUOUS = "ambiguous"
```

- [ ] **Step 4: Add the binding extractor**

Create `src/cleanporter/_bindings.py`:

```python
"""Top-level name bindings of a module, read with ``ast``.

Used to detect the case where a package ``__init__`` binds a name that also
exists as a submodule on disk. Parsing only -- nothing is imported.
"""

from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path

_TRY_TYPES: tuple[type[ast.AST], ...] = (ast.Try,) + (
    (ast.TryStar,) if hasattr(ast, "TryStar") else ()
)


def _collect(body: list[ast.stmt], names: set[str], submodule_imports: set[str]) -> None:
    for stmt in body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(stmt.name)
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                names.update(n.id for n in ast.walk(target) if isinstance(n, ast.Name))
        elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
            if isinstance(stmt.target, ast.Name):
                names.add(stmt.target.id)
        elif isinstance(stmt, ast.Import):
            for alias in stmt.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(stmt, ast.ImportFrom):
            for alias in stmt.names:
                if alias.name == "*":
                    continue
                bound = alias.asname or alias.name
                names.add(bound)
                # ``from . import mod`` binds the submodule itself, so it is
                # not a shadowing binding.
                if stmt.level and stmt.module is None:
                    submodule_imports.add(bound)
        elif isinstance(stmt, ast.If):
            _collect(stmt.body, names, submodule_imports)
            _collect(stmt.orelse, names, submodule_imports)
        elif isinstance(stmt, _TRY_TYPES):
            _collect(stmt.body, names, submodule_imports)
            _collect(stmt.orelse, names, submodule_imports)
            _collect(stmt.finalbody, names, submodule_imports)
            for handler in stmt.handlers:
                _collect(handler.body, names, submodule_imports)
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            _collect(stmt.body, names, submodule_imports)


@lru_cache(maxsize=1024)
def top_level_bindings(path: str) -> frozenset[str]:
    """Names bound at the top level of *path*, excluding self-submodule imports."""
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
        return frozenset()
    names: set[str] = set()
    submodule_imports: set[str] = set()
    _collect(tree.body, names, submodule_imports)
    return frozenset(names - submodule_imports)
```

- [ ] **Step 5: Return `Kind` from `ModuleMap.classify`**

In `src/cleanporter/firstparty.py`, record each package's `__init__.py` during the scan and consult it:

```python
    def __init__(self, roots: list[Path]) -> None:
        self.roots = [r.resolve() for r in roots]
        self._modules: set[str] = set()
        self._packages: set[str] = set()
        self._inits: dict[str, Path] = {}  # dotted package -> its __init__.py
        for root in self.roots:
            self._scan(root, root)
```

Replace `_scan` in full (it already gained extension handling in Task 6):

```python
    def _scan(self, root: Path, directory: Path) -> None:
        for child in sorted(directory.iterdir()):
            if child.name == "__pycache__" or child.name.startswith("."):
                continue
            if child.is_dir() and _is_pkg_dir(child):
                dotted = self._dotted(root, child)
                self._packages.add(dotted)
                init = child / "__init__.py"
                if init.is_file():
                    self._inits[dotted] = init
                self._scan(root, child)
            elif _is_importable_file(child):
                stem = _module_stem(child)
                if stem and stem != "__init__":
                    self._modules.add(self._dotted(root, child.with_name(stem)))
```

and replace `classify`:

```python
    def classify(self, parent: str, name: str) -> Kind | None:
        """First-party answer, or ``None`` if ``parent`` is not first-party."""
        if not self.is_first_party(parent):
            return None
        full = f"{parent}.{name}"
        on_disk = full in self._packages or full in self._modules
        init = self._inits.get(parent)
        shadowed = init is not None and name in top_level_bindings(str(init))
        if on_disk and shadowed:
            return Kind.AMBIGUOUS
        if on_disk:
            return Kind.MODULE
        return Kind.OBJECT
```

Add `from .model import Kind` and `from ._bindings import top_level_bindings`.

- [ ] **Step 6: Migrate Task 6's assertions to the new return type**

`ModuleMap.classify` no longer returns a bool, so the four tests written in
Task 6 must move to the enum. In `tests/test_firstparty.py`:

```bash
sed -i 's/mm\.classify(\(.*\)) is True/mm.classify(\1) is Kind.MODULE/; \
        s/mm\.classify(\(.*\)) is False/mm.classify(\1) is Kind.OBJECT/' \
    tests/test_firstparty.py
```

Leave `test_non_first_party_defers_to_the_probe` asserting `is None` — that is
still the "not first-party" signal.

- [ ] **Step 7: Teach the resolver about `Kind` and reasons**

In `src/cleanporter/resolver.py`:

```python
_AMBIGUOUS = "'{name}' is both a submodule of '{parent}' and bound in its __init__"
_NOT_IMPORTABLE = "'{parent}' is not importable in the target interpreter"


class Resolver:
    def __init__(self, module_map: ModuleMap, python: str | None = None) -> None:
        self._map = module_map
        self._python = python or sys.executable
        self._in_process = Path(self._python).resolve() == Path(sys.executable).resolve()
        self._cache: dict[tuple[str, str], bool | None] = {}
        self._notes: dict[tuple[str, str], str] = {}
        self._probe_path = str(Path(_probe.__file__).resolve())

    def _from_kind(self, key: tuple[str, str], kind: Kind) -> bool | None:
        if kind is Kind.MODULE:
            self._cache[key] = True
        elif kind is Kind.OBJECT:
            self._cache[key] = False
        else:
            self._cache[key] = None
            self._notes[key] = _AMBIGUOUS.format(parent=key[0], name=key[1])
        return self._cache[key]

    def is_module(self, parent: str, name: str) -> bool | None:
        key = (parent, name)
        if key in self._cache:
            return self._cache[key]
        kind = self._map.classify(parent, name)
        if kind is not None:
            return self._from_kind(key, kind)
        result = self._probe([key]).get(key)
        self._cache[key] = result
        return result

    def reason(self, parent: str, name: str) -> str:
        key = (parent, name)
        return self._notes.get(key, _NOT_IMPORTABLE.format(parent=parent))

    def warm(self, pairs: list[tuple[str, str]]) -> None:
        pending: list[tuple[str, str]] = []
        for key in pairs:
            if key in self._cache:
                continue
            kind = self._map.classify(*key)
            if kind is not None:
                self._from_kind(key, kind)
            else:
                pending.append(key)
        if pending:
            self._cache.update(self._probe(pending))
```

Add `from .model import Kind`. `_probe` is unchanged.

- [ ] **Step 8: Use the reason in the finding**

In `analyze.analyze_record`, replace the hard-coded `UNRESOLVED` detail:

```python
        if verdict is None:
            findings.append(
                Finding(rec.path, line, col, unit.parent, unit.name,
                        Status.UNRESOLVED, resolver.reason(unit.parent, unit.name))
            )
            continue
```

- [ ] **Step 9: Run the tests**

Run: `uv run pytest -q`
Expected: PASS — 64 tests.

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "fix: report submodule/__init__-binding ambiguity instead of guessing

When pkg/mod.py exists and pkg/__init__.py also binds 'mod', the binding wins
at import time. Answering 'module' silently misses a real violation; report
CP002 with an explanation and never rewrite it."
```

---

### Task 8: All-or-nothing rewrite gate and the string-literal guard

**Files:**
- Create: `src/cleanporter/guards.py`
- Modify: `src/cleanporter/rewrite.py`
- Test: `tests/test_guards.py`, `tests/test_rewrite.py`

**Interfaces:**
- Consumes: `model.Finding`, `model.Status`, `analyze.FileRecord`, `resolver.Resolver`, `config.Config`.
- Produces: `guards.Hit = tuple[int, str]`; `guards.find_string_mentions(tree, names, line_of, skip_ids=frozenset()) -> list[Hit]`. `rewrite.FixOutcome(status: str, source: str, blockers: list[Finding], fixed: int)` where `status` is `"fixed" | "clean" | "skipped" | "error"` and `source` always holds a string (equal to the input unless `status == "fixed"`). `rewrite.fix_record(rec, resolver, config) -> FixOutcome` — this replaces the temporary shim from Task 5. `_Fixer` gains public attributes `plan` and `blockers` (renamed from `_plan`, and new).

This is the keystone task. Everything from here to Task 12 adds one guard to the same gate.

The gate itself is three lines, because libcst hands `leave_Module` the pristine original tree: returning `original_node` discards every edit the transformer made to the children. Verified.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_guards.py`:

```python
"""Whole-file safety predicates, in isolation."""

from __future__ import annotations

import libcst as cst

from cleanporter import guards


def _line_of(tree: cst.Module):
    from libcst.metadata import MetadataWrapper, PositionProvider

    positions = MetadataWrapper(tree, unsafe_skip_copy=True).resolve(PositionProvider)
    return lambda node: positions[node].start.line


def _hits(source: str, names: set[str], **kwargs):
    tree = cst.parse_module(source)
    return guards.find_string_mentions(tree, names, _line_of(tree), **kwargs)


def test_dunder_all_mention_is_a_hit():
    hits = _hits('from a import THING\n__all__ = ["THING"]\n', {"THING"})
    assert len(hits) == 1
    assert hits[0][0] == 2
    assert "string literal" in hits[0][1]


def test_getattr_string_mention_is_a_hit():
    assert _hits('x = getattr(m, "Widget")\n', {"Widget"})


def test_substring_is_not_a_hit():
    assert _hits('s = "THINGAMAJIG and SOMETHING"\n', {"THING"}) == []


def test_unrelated_string_is_not_a_hit():
    assert _hits('s = "hello"\n', {"THING"}) == []


def test_no_names_means_no_hits():
    assert _hits('__all__ = ["THING"]\n', set()) == []


def test_skip_ids_exempts_a_specific_string_node():
    tree = cst.parse_module('__all__ = ["THING"]\n')
    strings = [n for n in _walk(tree) if isinstance(n, cst.SimpleString)]
    assert guards.find_string_mentions(
        tree, {"THING"}, _line_of(tree), skip_ids=frozenset({id(strings[0])})
    ) == []


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)
```

Create `tests/test_rewrite.py`:

```python
"""Fixer behaviour: rewrites must be exact, or not happen at all."""

from __future__ import annotations

from pathlib import Path

import libcst as cst

from cleanporter.analyze import FileRecord, collect_pairs, package_of
from cleanporter.config import Config
from cleanporter.firstparty import ModuleMap
from cleanporter.model import Status
from cleanporter.resolver import Resolver
from cleanporter.rewrite import FixOutcome, fix_record

FIXTURES = Path(__file__).parent / "fixtures"


def outcome(source: str) -> FixOutcome:
    path = FIXTURES / "pkg" / "a.py"
    mm = ModuleMap.from_paths([FIXTURES / "pkg", path])
    resolver = Resolver(mm)
    rec = FileRecord(path, source, cst.parse_module(source), package_of(path, mm))
    resolver.warm(collect_pairs([rec]))
    return fix_record(rec, resolver, Config())


def test_basic_rewrite_reports_fixed():
    result = outcome("from pkg.sub.mod import Thing\nx = Thing()\n")
    assert result.status == "fixed"
    assert result.fixed == 1
    assert result.source == "from pkg.sub import mod\nx = mod.Thing()\n"
    assert result.blockers == []


def test_compliant_file_reports_clean():
    src = "from pkg.sub import mod\nx = mod.Thing()\n"
    result = outcome(src)
    assert result.status == "clean"
    assert result.source == src


def test_dunder_all_blocks_the_whole_file():
    src = 'from pkg.sub.mod import Thing\n__all__ = ["Thing"]\nx = Thing()\n'
    result = outcome(src)
    assert result.status == "skipped"
    assert result.source == src, "a blocked file must be byte-identical"
    assert [f.status for f in result.blockers] == [Status.SKIPPED]
    assert "string literal" in result.blockers[0].detail


def test_a_blocker_suppresses_otherwise_safe_rewrites_in_the_same_file():
    # 'go' is perfectly safe to rewrite, but the file is all-or-nothing.
    src = (
        "from pkg.sub.mod import Thing, go\n"
        '__all__ = ["Thing"]\n'
        "x = Thing()\n"
        "y = go()\n"
    )
    result = outcome(src)
    assert result.status == "skipped"
    assert result.source == src


def test_blocker_finding_formats_as_cp003():
    src = 'from pkg.sub.mod import Thing\n__all__ = ["Thing"]\n'
    (blocker,) = outcome(src).blockers
    assert blocker.code == "CP003"
    assert "file not rewritten" in blocker.format()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_guards.py tests/test_rewrite.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cleanporter.guards'`.

- [ ] **Step 3: Write the guards module**

Create `src/cleanporter/guards.py`:

```python
"""Whole-file safety predicates.

Each function returns ``(line, reason)`` hits. A single hit blocks the entire
file from being rewritten: libcst's scope analysis is not flow-sensitive and
these constructs make a mechanical rename unprovable, so the conservative
answer is to leave the file exactly as the author wrote it and explain why.
"""

from __future__ import annotations

import re
from typing import Callable, Collection, FrozenSet

import libcst as cst

#: ``(line, human-readable reason)``.
Hit = tuple[int, str]
LineOf = Callable[[cst.CSTNode], int]


def _patterns(names: Collection[str]) -> list[tuple[str, re.Pattern[str]]]:
    return [(n, re.compile(rf"\b{re.escape(n)}\b")) for n in sorted(names)]


def find_string_mentions(
    tree: cst.Module,
    names: Collection[str],
    line_of: LineOf,
    skip_ids: FrozenSet[int] = frozenset(),
) -> list[Hit]:
    """Names mentioned inside string literals.

    ``__all__ = ["Widget"]`` and ``getattr(m, "Widget")`` keep working only if
    the name survives; a rename would silently break them. ``skip_ids`` exempts
    string nodes the caller is rewriting itself (lazy annotations, Task 16).
    """
    hits: list[Hit] = []
    if not names:
        return hits
    patterns = _patterns(names)

    class V(cst.CSTVisitor):
        def visit_SimpleString(self, node: cst.SimpleString) -> None:
            if id(node) in skip_ids:
                return
            for name, pattern in patterns:
                if pattern.search(node.raw_value):
                    hits.append((line_of(node), f"name '{name}' appears in a string literal"))
                    break

    tree.visit(V())
    return hits
```

- [ ] **Step 4: Add the gate to the fixer**

In `src/cleanporter/rewrite.py`:

Change the imports and the class header:

```python
from libcst.metadata import GlobalScope, PositionProvider, ScopeProvider

from . import _imports, guards
from .model import Finding, Status


class _Fixer(cst.CSTTransformer):
    METADATA_DEPENDENCIES = (ScopeProvider, PositionProvider)

    def __init__(self, rec: FileRecord, resolver: Resolver, config: Config) -> None:
        super().__init__()
        self._rec = rec
        self._resolver = resolver
        self._config = config
        self.plan = _Plan()
        self.blockers: list[Hit] = []
        self._module_binding: dict[str, str] = {}
        self._existing: dict[str, str] = {}
        self._used_names: set[str] = set()
        self._tc_ids: set[int] = set()
        #: Local names this run would rewrite -- the input to every guard.
        self._fixed_locals: set[str] = set()
```

Rename every remaining `self._plan` to `self.plan` in the file.

In `_plan_line`, record the locals being rewritten. Immediately after `if not fix: return`:

```python
        self._fixed_locals.update(asname or name for name, asname in fix)
```

At the end of `visit_Module`, after the `_plan_line` loop, run the guards:

```python
        self._run_guards(node)

    def _line_of(self, node: cst.CSTNode) -> int:
        position = self.get_metadata(PositionProvider, node, None)
        return position.start.line if position is not None else 0

    def _run_guards(self, node: cst.Module) -> None:
        if not self._fixed_locals:
            return
        self.blockers.extend(
            guards.find_string_mentions(node, self._fixed_locals, self._line_of)
        )
```

Add the gate:

```python
    def leave_Module(self, original: cst.Module, updated: cst.Module) -> cst.Module:
        # All-or-nothing. libcst hands us the pristine original tree, so
        # returning it discards every edit made to the children.
        return original if self.blockers else updated
```

Replace the temporary `FixOutcome` and `fix_record` from Task 5 with the real ones:

```python
@dataclass
class FixOutcome:
    #: ``"fixed"`` | ``"clean"`` | ``"skipped"`` | ``"error"``.
    status: str
    #: Resulting source; equal to the input unless ``status == "fixed"``.
    source: str
    blockers: list[Finding] = field(default_factory=list)
    fixed: int = 0


def fix_record(rec: FileRecord, resolver: Resolver, config: Config) -> FixOutcome:
    """Rewrite one file, or leave it exactly as it was and say why."""
    wrapper = cst.MetadataWrapper(rec.tree, unsafe_skip_copy=True)
    fixer = _Fixer(rec, resolver, config)
    new_source = wrapper.visit(fixer).code

    if fixer.blockers:
        return FixOutcome(
            "skipped",
            rec.source,
            [
                Finding(rec.path, line, 0, "?", "?", Status.SKIPPED, reason)
                for line, reason in sorted(set(fixer.blockers))
            ],
        )
    if not fixer.plan.fixed or new_source == rec.source:
        return FixOutcome("clean", rec.source)
    return FixOutcome("fixed", new_source, [], fixer.plan.fixed)
```

Add `from .guards import Hit` alongside the `guards` import.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest -q`
Expected: PASS — 76 tests.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: all-or-nothing rewrite gate with the string-literal guard

A file is either fully rewritten or left byte-identical, with CP003 findings
explaining why. First guard: a rewritten local mentioned in a string literal
(__all__, getattr), which a rename would silently break."
```

---

### Task 9: `global` / `nonlocal` guard

**Files:**
- Modify: `src/cleanporter/guards.py`, `src/cleanporter/rewrite.py`
- Test: `tests/test_guards.py`, `tests/test_rewrite.py` (extend both)

**Interfaces:**
- Consumes: `guards.Hit`, `guards.LineOf`, the gate from Task 8.
- Produces: `guards.find_scope_declarations(tree, names, line_of) -> list[Hit]`.

`global THING` inside a function makes the module-level binding writable from elsewhere. Rewriting the import to `helpers.THING` leaves the `global THING` statement referring to a name that no longer exists, and the assignment through it silently stops feeding the reads we rewrote.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_guards.py`:

```python
def _decl_hits(source: str, names: set[str]):
    tree = cst.parse_module(source)
    return guards.find_scope_declarations(tree, names, _line_of(tree))


def test_global_declaration_is_a_hit():
    hits = _decl_hits("def f():\n    global THING\n    THING = 3\n", {"THING"})
    assert len(hits) == 1
    assert hits[0][0] == 2
    assert "global" in hits[0][1]


def test_nonlocal_declaration_is_a_hit():
    src = "def outer():\n    Widget = 1\n    def inner():\n        nonlocal Widget\n"
    hits = _decl_hits(src, {"Widget"})
    assert len(hits) == 1 and "nonlocal" in hits[0][1]


def test_declaration_of_an_unrelated_name_is_not_a_hit():
    assert _decl_hits("def f():\n    global OTHER\n", {"THING"}) == []


def test_multiple_names_in_one_declaration_are_reported_together():
    hits = _decl_hits("def f():\n    global A, B\n", {"A", "B"})
    assert len(hits) == 1 and "A/B" in hits[0][1]
```

Append to `tests/test_rewrite.py`:

```python
def test_global_declaration_blocks_the_file():
    src = (
        "from pkg.sub.mod import Thing\n"
        "def f():\n"
        "    global Thing\n"
        "    Thing = 3\n"
    )
    result = outcome(src)
    assert result.status == "skipped"
    assert result.source == src
    assert "global" in result.blockers[0].detail
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_guards.py tests/test_rewrite.py -v`
Expected: FAIL — `AttributeError: module 'cleanporter.guards' has no attribute 'find_scope_declarations'`.

- [ ] **Step 3: Write the implementation**

Append to `src/cleanporter/guards.py`:

```python
def find_scope_declarations(
    tree: cst.Module, names: Collection[str], line_of: LineOf
) -> list[Hit]:
    """``global`` / ``nonlocal`` declarations naming a rewritten local.

    Such a declaration keeps a module-level name writable from another scope.
    Qualifying the reads without also rewriting the writes would silently
    decouple them, so the file is left alone.
    """
    hits: list[Hit] = []
    if not names:
        return hits
    wanted = set(names)

    class V(cst.CSTVisitor):
        def visit_Global(self, node: cst.Global) -> None:
            self._record(node, "global")

        def visit_Nonlocal(self, node: cst.Nonlocal) -> None:
            self._record(node, "nonlocal")

        def _record(self, node: cst.CSTNode, keyword: str) -> None:
            clashing = [i.name.value for i in node.names if i.name.value in wanted]
            if clashing:
                hits.append(
                    (line_of(node), f"'{'/'.join(sorted(clashing))}' declared {keyword}")
                )

    tree.visit(V())
    return hits
```

Wire it into `_Fixer._run_guards`:

```python
    def _run_guards(self, node: cst.Module) -> None:
        if not self._fixed_locals:
            return
        self.blockers.extend(
            guards.find_string_mentions(node, self._fixed_locals, self._line_of)
        )
        self.blockers.extend(
            guards.find_scope_declarations(node, self._fixed_locals, self._line_of)
        )
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest -q`
Expected: PASS — 81 tests.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: block rewriting when a target is declared global/nonlocal"
```

---

### Task 10: Module-level rebinding guard

**Files:**
- Modify: `src/cleanporter/rewrite.py`
- Test: `tests/test_rewrite.py` (extend)

**Interfaces:**
- Consumes: the gate from Task 8, `libcst.metadata.ScopeProvider`.
- Produces: no new public names. `_Fixer._plan_line` appends to `self.blockers` when a rewritten local has a sibling assignment in the same scope.

This guard lives in the fixer rather than `guards.py` because it needs scope metadata, which the standalone predicates deliberately do not take.

libcst's scope analysis is not flow-sensitive. When a module-level name is both imported and later assigned, every access lists *both* as referents, so there is no way to tell which accesses belong to the import. Qualifying all of them is wrong.

Note the closely related hazard — a local shadowing the *new* module token — needs no work: `_binding_for` already consults `GlobalScope.assignments`, which includes plain assignments, and picks `mod_2`. Task 10's test suite pins that behaviour so a future refactor cannot lose it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rewrite.py`:

```python
def test_module_level_rebinding_blocks_the_file():
    src = (
        "from pkg.sub.mod import Thing\n"
        "first = Thing\n"
        "Thing = 5\n"
        "second = Thing\n"
    )
    result = outcome(src)
    assert result.status == "skipped"
    assert result.source == src
    assert "rebound" in result.blockers[0].detail


def test_function_local_shadowing_is_safe_and_still_rewritten():
    src = (
        "from pkg.sub.mod import Thing\n"
        "outer = Thing()\n"
        "def f():\n"
        "    Thing = 'shadow'\n"
        "    return Thing\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    assert result.source == (
        "from pkg.sub import mod\n"
        "outer = mod.Thing()\n"
        "def f():\n"
        "    Thing = 'shadow'\n"
        "    return Thing\n"
    )


def test_collision_with_the_new_module_token_is_aliased_not_broken():
    src = "from pkg.sub.mod import Thing\nmod = 'a local string'\nx = Thing()\n"
    result = outcome(src)
    assert result.status == "fixed"
    assert "import mod as mod_2" in result.source or "mod as mod_2" in result.source
    assert "mod_2.Thing()" in result.source
    assert "mod = 'a local string'" in result.source


def test_import_never_referenced_is_still_removed():
    result = outcome("from pkg.sub.mod import Thing\nx = 1\n")
    assert result.status == "fixed"
    assert "import Thing" not in result.source
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_rewrite.py -v`
Expected: FAIL — `test_module_level_rebinding_blocks_the_file` gets `status == "fixed"` and a corrupted rewrite.

- [ ] **Step 3: Write the implementation**

In `_Fixer._plan_line`, inside the `for name, asname in fix:` loop that qualifies references, detect sibling assignments before recording any rename:

```python
        for name, asname in fix:
            bound = asname or name
            ours = [a for a in scope[bound] if getattr(a, "node", None) is imp]
            others = [
                a
                for a in scope[bound]
                if getattr(a, "node", None) is not imp
                and type(a).__name__ != "BuiltinAssignment"
            ]
            if ours and others:
                # libcst's scopes are not flow-sensitive, so accesses of a
                # rebound name list both the import and the assignment as
                # referents. There is no safe subset to rewrite.
                self.blockers.append(
                    (self._line_of(imp), f"local '{bound}' is rebound in the same scope")
                )
                continue
            for assignment in ours:
                for ref in assignment.references:
                    self.plan.name_repl[id(ref.node)] = cst.Attribute(
                        value=cst.Name(bind), attr=cst.Name(name)
                    )
            self.plan.fixed += 1
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest -q`
Expected: PASS — 85 tests.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: block rewriting a local that is rebound in the same scope

libcst scope analysis is not flow-sensitive: accesses of a rebound name list
both the import and the assignment as referents, so no subset of them can be
qualified safely."
```

---

### Task 11: Re-parse verification

**Files:**
- Modify: `src/cleanporter/rewrite.py`
- Test: `tests/test_rewrite.py` (extend)

**Interfaces:**
- Consumes: `rewrite.fix_record`, `rewrite.FixOutcome` from Task 8.
- Produces: `FixOutcome.status == "error"` when the rewritten source does not parse. `source` holds the *original* text in that case.

A last line of defence. If a bug ever produces output that will not compile, the tool must hand back the original rather than write it to disk.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_rewrite.py`:

```python
def test_unparseable_rewrite_is_reverted_and_reported(monkeypatch):
    import cleanporter.rewrite as rewrite_mod

    def boom(source):
        raise SyntaxError("simulated bad output")

    monkeypatch.setattr(rewrite_mod.ast, "parse", boom)

    src = "from pkg.sub.mod import Thing\nx = Thing()\n"
    result = outcome(src)
    assert result.status == "error"
    assert result.source == src, "the original must be handed back untouched"
    assert result.blockers and "did not parse" in result.blockers[0].detail


def test_valid_rewrite_passes_verification():
    result = outcome("from pkg.sub.mod import Thing\nx = Thing()\n")
    assert result.status == "fixed"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_rewrite.py::test_unparseable_rewrite_is_reverted_and_reported -v`
Expected: FAIL — `assert 'fixed' == 'error'`.

- [ ] **Step 3: Write the implementation**

At the top of `src/cleanporter/rewrite.py` add `import ast`. Then in `fix_record`, between the blocker check and the success return:

```python
    if not fixer.plan.fixed or new_source == rec.source:
        return FixOutcome("clean", rec.source)

    try:
        ast.parse(new_source)
    except SyntaxError as exc:
        # Never hand back source we cannot compile. Keep the original.
        return FixOutcome(
            "error",
            rec.source,
            [
                Finding(
                    rec.path, exc.lineno or 0, 0, "?", "?", Status.SKIPPED,
                    "internal error: the rewrite did not parse; reverted",
                )
            ],
        )

    return FixOutcome("fixed", new_source, [], fixer.plan.fixed)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest -q`
Expected: PASS — 87 tests.

- [ ] **Step 5: Handle `"error"` in the CLI**

In `cli.run`, the `outcome.status == "fixed"` branch already skips other statuses, and `findings.extend(outcome.blockers)` already collects the error finding. Confirm that an `"error"` outcome contributes a CP003 and therefore exit code 1, not a silent success. Add to `tests/test_cli.py`:

```python
def test_internal_rewrite_error_does_not_write_a_broken_file(project, monkeypatch):
    from cleanporter.model import Finding, Status
    from cleanporter.rewrite import FixOutcome

    target = project / "src" / "demo" / "consumer.py"
    before = target.read_text(encoding="utf-8")

    def fake(rec, resolver, config):
        return FixOutcome(
            "error", rec.source,
            [Finding(rec.path, 1, 0, "?", "?", Status.SKIPPED, "internal error")],
        )

    # cli imports fix_record by name, so patch it there.
    monkeypatch.setattr("cleanporter.cli.fix_record", fake)
    assert main(["--fix", str(project / "src")]) == 1
    assert target.read_text(encoding="utf-8") == before
```

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: verify the rewritten source parses before returning it"
```

---

### Task 12: Preserve trailing comments on rewritten import lines

**Files:**
- Modify: `src/cleanporter/rewrite.py`
- Test: `tests/test_rewrite.py` (extend)

**Interfaces:**
- Consumes: `_Fixer._plan_line`, the gate from Task 8.
- Produces: no new public names. `_plan_line` carries `leading_lines` onto the first replacement statement and `trailing_whitespace` onto the last, and blocks the file when a line that would be *deleted* carries a trailing comment.

Verified defect: `from demo.helpers import THING  # trailing comment` loses the comment, because the replacement statements are built with `cst.parse_statement` and only `leading_lines` is carried across.

The deletion case has no good answer — when the module is already bound the whole line disappears and there is nowhere to put the comment. Rather than silently dropping the author's words, block the file and say so.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rewrite.py`:

```python
def test_trailing_comment_is_preserved():
    src = "from pkg.sub.mod import Thing  # keep me\nx = Thing()\n"
    result = outcome(src)
    assert result.status == "fixed"
    assert result.source == "from pkg.sub import mod  # keep me\nx = mod.Thing()\n"


def test_leading_comments_and_blank_lines_are_preserved():
    src = (
        "# leading comment block\n"
        "# second line\n"
        "\n"
        "from pkg.sub.mod import Thing\n"
        "\n"
        "use = Thing\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    assert result.source == (
        "# leading comment block\n"
        "# second line\n"
        "\n"
        "from pkg.sub import mod\n"
        "\n"
        "use = mod.Thing\n"
    )


def test_trailing_comment_lands_on_the_last_replacement_line():
    src = "from pkg.sub.mod import Thing, go  # both\nx = Thing() + go()\n"
    result = outcome(src)
    assert result.status == "fixed"
    assert result.source.splitlines()[0].endswith("# both")


def test_deleting_a_commented_line_blocks_instead_of_dropping_the_comment():
    src = (
        "from pkg.sub import mod\n"
        "from pkg.sub.mod import Thing  # why this exists\n"
        "x = Thing()\n"
    )
    result = outcome(src)
    assert result.status == "skipped"
    assert result.source == src
    assert "comment" in result.blockers[0].detail


def test_deleting_an_uncommented_line_is_fine():
    src = "from pkg.sub import mod\nfrom pkg.sub.mod import Thing\nx = Thing()\n"
    result = outcome(src)
    assert result.status == "fixed"
    assert result.source == "from pkg.sub import mod\nx = mod.Thing()\n"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_rewrite.py -v`
Expected: FAIL — `test_trailing_comment_is_preserved` produces `from pkg.sub import mod\n` with the comment gone.

- [ ] **Step 3: Write the implementation**

Replace the tail of `_Fixer._plan_line` (the block that currently carries only `leading_lines`):

```python
        has_trailing_comment = line.trailing_whitespace.comment is not None
        if new_lines:
            first = new_lines[0]
            if isinstance(first, cst.SimpleStatementLine):
                new_lines[0] = first.with_changes(leading_lines=line.leading_lines)
            last = new_lines[-1]
            if isinstance(last, cst.SimpleStatementLine):
                new_lines[-1] = last.with_changes(
                    trailing_whitespace=line.trailing_whitespace
                )
        elif has_trailing_comment:
            # The line disappears entirely (the module is already bound), so
            # there is nowhere to put the author's comment. Do not drop it.
            self.blockers.append(
                (
                    self._line_of(line),
                    "removing this import would discard its trailing comment",
                )
            )
            return
        self.plan.line_repl[id(line)] = new_lines
```

Note `new_lines[-1]` is read *after* `new_lines[0]` is reassigned, so a single-statement replacement correctly receives both.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest -q`
Expected: PASS — 92 tests.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "fix: preserve trailing comments on rewritten import lines

Replacement statements were built with parse_statement and carried only
leading_lines, so a trailing comment was silently dropped. Carry
trailing_whitespace too, and block the file when the line is deleted outright
and the comment has nowhere to go."
```

---

### Task 13: Report what `--fix` declined, and count it correctly

**Files:**
- Modify: `src/cleanporter/cli.py`
- Test: `tests/test_cli.py` (extend)

**Interfaces:**
- Consumes: `cli.run` from Task 5, `rewrite.FixOutcome` from Task 8.
- Produces: no new public names. Parse errors stop being mixed into the `findings` list and are printed and counted separately.

modimports' `fix` command listed only `UNFIXABLE` and `UNKNOWN` in "left for manual review", so a `VIOLATION` it declined to rewrite — a function-scope or `TYPE_CHECKING` import — vanished from the output entirely. The Task 5 CLI already re-analyses each record after fixing, so most of these tests should pass on arrival; their value is as regression locks. The counting bug is real and does need a fix: `unresolved` currently filters on `parent != "?"`, which silently excludes unanchorable relative imports from the summary even though they are printed.

- [ ] **Step 1: Write the tests**

Append to `tests/test_cli.py`:

```python
def test_fix_still_reports_violations_it_declined(project, capsys):
    (project / "src" / "demo" / "consumer.py").write_text(
        "from demo.helpers import THING\n"
        '__all__ = ["THING"]\n',
        encoding="utf-8",
    )
    rc = main(["--fix", str(project / "src")])
    out = capsys.readouterr().out
    assert "CP003" in out, "the blocker must be explained"
    assert "CP001" in out, "the unfixed violation must still be reported"
    assert rc == 1


def test_fix_reports_nothing_for_a_fully_fixed_file(project, capsys):
    rc = main(["--fix", str(project / "src")])
    out = capsys.readouterr().out
    assert "CP001" not in out
    assert rc == 0


def test_summary_counts_match_the_printed_lines(project, capsys):
    (project / "src" / "demo" / "consumer.py").write_text(
        "from demo.helpers import THING\n"
        "from definitely_missing_pkg_xyz import other\n",
        encoding="utf-8",
    )
    main([str(project / "src")])
    out = capsys.readouterr().out
    assert out.count("CP001") == 1
    assert out.count("CP002") == 1
    assert "1 violation(s)" in out
    assert "1 unresolved" in out


def test_unanchorable_relative_import_is_counted(project, capsys):
    (project / "src" / "demo" / "consumer.py").write_text(
        "from ..... import nothing\n", encoding="utf-8"
    )
    main([str(project / "src")])
    out = capsys.readouterr().out
    assert "CP002" in out
    assert "0 unresolved" not in out
```

- [ ] **Step 2: Run the tests**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL on `test_summary_counts_match_the_printed_lines` and `test_unanchorable_relative_import_is_counted` (the summary reports `0 unresolved`). The other two should already PASS — that is the intended outcome; keep them.

- [ ] **Step 3: Separate parse errors from findings**

In `cli.run`, stop seeding `findings` with `parse_errors` and print them on their own:

```python
    records, resolver, parse_errors, warnings = build(paths, config)
    for warning in warnings:
        print(f"cleanporter: warning: {warning}")
    for error in sorted(parse_errors, key=lambda f: (str(f.path), f.line)):
        print(error.format())

    findings: list[Finding] = []
```

and fix the counting:

```python
    violations = sum(f.status is Status.VIOLATION for f in findings)
    skipped = sum(f.status is Status.SKIPPED for f in findings)
    unresolved = sum(f.status is Status.UNRESOLVED for f in findings)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest -q`
Expected: PASS — 97 tests.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "fix: count unresolved findings accurately and keep parse errors separate

Also locks in that --fix still reports the violations it declined to rewrite;
modimports' fix command dropped those from its output entirely."
```

---

### Task 14: `scope = "first-party"`

**Files:**
- Modify: `src/cleanporter/resolver.py`, `src/cleanporter/analyze.py`
- Test: `tests/test_analyze.py` (extend)

**Interfaces:**
- Consumes: `firstparty.ModuleMap.is_first_party`, `config.Config.scope`.
- Produces: `resolver.Resolver.is_first_party(dotted: str) -> bool`, delegating to the module map. `analyze_record` drops findings whose parent is not first-party when `config.scope == "first-party"`.

Lets a project adopt the rule on its own code first and enforce stdlib/third-party later.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_analyze.py`:

```python
def test_scope_first_party_ignores_stdlib():
    src = "from functools import partial\nfrom pkg.sub.mod import Thing\n"
    findings = _analyze_with(src, Config(scope="first-party"))
    assert [f.parent for f in findings] == ["pkg.sub.mod"]


def test_scope_all_reports_both():
    src = "from functools import partial\nfrom pkg.sub.mod import Thing\n"
    findings = _analyze_with(src, Config(scope="all"))
    assert sorted(f.parent for f in findings) == ["functools", "pkg.sub.mod"]


def test_scope_first_party_still_reports_unanchorable_relative_imports():
    findings = _analyze_with("from ..... import nothing\n", Config(scope="first-party"))
    assert [f.status for f in findings] == [Status.UNRESOLVED]


def _analyze_with(source: str, config: Config):
    path = FIXTURES / "pkg" / "a.py"
    mm = ModuleMap.from_paths([FIXTURES / "pkg", path])
    resolver = Resolver(mm)
    rec = _record(source, path, mm)
    resolver.warm([(u.parent, u.name) for u in rec.units if u.parent and not u.star])
    return analyze_record(rec, resolver, config)
```

Add `from cleanporter.analyze import collect_pairs` and `from cleanporter.model import Status` to that file's imports if they are not already present.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_analyze.py -v`
Expected: FAIL — `test_scope_first_party_ignores_stdlib` reports both parents.

- [ ] **Step 3: Write the implementation**

Add to `resolver.Resolver`:

```python
    def is_first_party(self, dotted: str) -> bool:
        """True when *dotted* lives under one of the analysis roots."""
        return self._map.is_first_party(dotted)
```

In `analyze.analyze_record`, filter after the exemption check and before classification:

```python
        if config.is_exempt(unit.parent, unit.name):
            continue
        if config.scope == "first-party" and not resolver.is_first_party(unit.parent):
            continue
```

Note this sits *after* the `unit.parent is None` branch, so unanchorable relative imports are still reported under either scope.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest -q`
Expected: PASS — 100 tests.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: scope = 'first-party' limits reporting to the project's own modules"
```

---

### Task 15: Fix imports that are not at module scope

**Files:**
- Modify: `src/cleanporter/rewrite.py`
- Test: `tests/test_rewrite.py` (extend)

**Interfaces:**
- Consumes: `_Fixer` as of Task 12.
- Produces: no new public names. `_Fixer._module_binding` is re-keyed from `str` to `tuple[int, str]` (`(id(scope), parent)`). New private helpers `_Fixer._local_names(scope) -> set[str]`, `_Fixer._names_in_scope(scope) -> set[str]`, and `_Fixer._binding_for(scope, parent) -> str | None` (was `_binding_for(parent)`).

modimports refuses any import that is not in `GlobalScope`; cleanporter fixes these correctly and in place. Removing the restriction is not just deleting a check: the binding bookkeeping is module-global today, so two functions importing the same module would have the import emitted into the first one only, leaving the second with a `NameError`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rewrite.py`:

```python
def test_function_scope_import_is_fixed_in_place():
    src = "def f():\n    from pkg.sub.mod import Thing\n    return Thing()\n"
    result = outcome(src)
    assert result.status == "fixed"
    assert result.source == (
        "def f():\n    from pkg.sub import mod\n    return mod.Thing()\n"
    )


def test_each_function_gets_its_own_import():
    src = (
        "def f():\n"
        "    from pkg.sub.mod import Thing\n"
        "    return Thing()\n"
        "def g():\n"
        "    from pkg.sub.mod import Thing\n"
        "    return Thing()\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    assert result.source.count("from pkg.sub import mod") == 2


def test_function_scope_reuses_a_module_level_binding():
    src = (
        "from pkg.sub import mod\n"
        "def f():\n"
        "    from pkg.sub.mod import Thing\n"
        "    return Thing()\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    assert result.source == (
        "from pkg.sub import mod\ndef f():\n    return mod.Thing()\n"
    )


def test_function_scope_avoids_colliding_with_a_local():
    src = (
        "def f():\n"
        "    mod = 'a local'\n"
        "    from pkg.sub.mod import Thing\n"
        "    return mod, Thing()\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    assert "mod_2" in result.source
    assert "mod = 'a local'" in result.source


def test_class_body_import_is_fixed():
    src = "class C:\n    from pkg.sub.mod import Thing\n    x = Thing\n"
    result = outcome(src)
    assert result.status == "fixed"
    assert "from pkg.sub import mod" in result.source
    assert "x = mod.Thing" in result.source
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_rewrite.py -v`
Expected: FAIL — all five report `status == "clean"` because non-global scopes are skipped.

- [ ] **Step 3: Write the implementation**

In `_Fixer.__init__`, re-key the binding map and add the scope caches:

```python
        self._module_binding: dict[tuple[int, str], str] = {}
        self._existing: dict[str, str] = {}
        self._global_names: set[str] = set()
        self._scope_names: dict[int, set[str]] = {}
```

In `visit_Module`, record the global names once, before planning:

```python
    def visit_Module(self, node: cst.Module) -> None:
        self._tc_ids = _type_checking_import_ids(node)
        for _line, imp in self._import_lines(node):
            scope = self.get_metadata(ScopeProvider, imp, None)
            if isinstance(scope, GlobalScope):
                self._global_names = {a.name for a in scope.assignments}
                break
        self._build_existing(node)
        for line, imp in self._import_lines(node):
            self._plan_line(line, imp)
        self._run_guards(node)
```

Delete `self._used_names` and its updates; the scope helpers replace it. Add:

```python
    def _local_names(self, scope: object) -> set[str]:
        """Names assigned directly in *scope*, ignoring enclosing scopes."""
        return {a.name for a in scope.assignments}  # type: ignore[attr-defined]

    def _names_in_scope(self, scope: object) -> set[str]:
        """Names a new binding in *scope* must not collide with."""
        key = id(scope)
        if key not in self._scope_names:
            names = self._local_names(scope)
            if not isinstance(scope, GlobalScope):
                names = names | self._global_names
            self._scope_names[key] = names
        return self._scope_names[key]
```

Replace `_binding_for` entirely:

```python
    def _binding_for(self, scope: object, parent: str) -> str | None:
        """Token to qualify through, or ``None`` when one already exists.

        Returns the token of a *new* import to emit. ``None`` means *parent*
        is already bound in a way this scope can see, so no import is needed.
        """
        key = (id(scope), parent)
        if key in self._module_binding:
            return None
        existing = self._existing.get(parent)
        if existing is not None:
            # A module-level import is visible from nested scopes unless the
            # scope assigns that name itself.
            shadowed = not isinstance(scope, GlobalScope) and existing in self._local_names(scope)
            if not shadowed:
                self._module_binding[key] = existing
                return None
        token = parent.rsplit(".", 1)[-1]
        taken = self._names_in_scope(scope)
        bind = token
        counter = 2
        while bind in taken:
            bind = f"{token}_{counter}"
            counter += 1
        taken.add(bind)
        self._module_binding[key] = bind
        return bind
```

In `_plan_line`, drop the `GlobalScope` restriction and thread the scope through:

```python
        scope = self.get_metadata(ScopeProvider, imp, None)
        if scope is None:
            return
```

and:

```python
        bind = self._binding_for(scope, parent)
        if bind is not None:
            new_lines.append(self._module_import_stmt(parent, bind))
        bind = self._module_binding[(id(scope), parent)]
```

`_build_existing` keeps its `GlobalScope` check — only module-level imports populate `_existing`, which is exactly the visibility rule being modelled.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest -q`
Expected: PASS — 105 tests. `test_async_and_nested_scopes_are_reported` from Task 1 still passes; those imports are now rewritten as well as reported.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: rewrite imports that are not at module scope

Bindings are now tracked per scope, so two functions importing the same module
each get their own import instead of the second silently losing its binding."
```

---

### Task 16: `TYPE_CHECKING` imports under `from __future__ import annotations`

**Files:**
- Modify: `src/cleanporter/rewrite.py`
- Test: `tests/test_rewrite.py` (extend)

**Interfaces:**
- Consumes: `_Fixer` as of Task 15, `guards.find_string_mentions`'s `skip_ids` parameter from Task 8.
- Produces: `_Plan.string_repl: dict[int, str]` mapping `id(SimpleString)` to its replacement value. `rewrite._annotation_strings(tree: cst.Module) -> dict[int, cst.SimpleString]`. `_Fixer` gains `_future_annotations: bool` and `_string_targets: dict[str, str]` (local name -> `"token.Name"`).

A `TYPE_CHECKING`-gated import is only safe to rewrite when annotations are strings at runtime. Without `from __future__ import annotations` an eagerly evaluated annotation would raise `NameError` after the rename, so block. With it, rewrite the import *and* the lazy string annotations that mention the name.

This task also fixes a latent bug carried over from cleanporter, where the string-annotation rename was keyed by the *imported symbol* rather than the *local name*, so `from m import Thing as T` never matched `'T'` in an annotation. Keying by local name is what `test_alias_in_lazy_annotation_is_renamed_by_local_name` pins.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rewrite.py`:

```python
_TC_HEAD = "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n"


def test_type_checking_without_future_annotations_blocks():
    src = _TC_HEAD + "    from pkg.sub.mod import Thing\ndef g(x: Thing) -> None: ...\n"
    result = outcome(src)
    assert result.status == "skipped"
    assert result.source == src
    assert "TYPE_CHECKING" in result.blockers[0].detail


def test_type_checking_with_future_annotations_is_fixed():
    src = (
        "from __future__ import annotations\n"
        + _TC_HEAD
        + "    from pkg.sub.mod import Thing\ndef g(x: Thing) -> None: ...\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    assert "    from pkg.sub import mod" in result.source
    assert "def g(x: mod.Thing) -> None" in result.source


def test_lazy_string_annotation_is_renamed():
    src = (
        "from __future__ import annotations\n"
        + _TC_HEAD
        + "    from pkg.sub.mod import Thing\ndef g(x: 'Thing') -> None: ...\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    assert "'mod.Thing'" in result.source


def test_alias_in_lazy_annotation_is_renamed_by_local_name():
    src = (
        "from __future__ import annotations\n"
        + _TC_HEAD
        + "    from pkg.sub.mod import Thing as T\ndef g(x: 'T') -> None: ...\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    assert "'mod.Thing'" in result.source
    assert "'T'" not in result.source


def test_string_outside_an_annotation_still_blocks_under_future_annotations():
    src = (
        "from __future__ import annotations\n"
        "from pkg.sub.mod import Thing\n"
        '__all__ = ["Thing"]\n'
    )
    result = outcome(src)
    assert result.status == "skipped"
    assert "string literal" in result.blockers[0].detail


def test_annotated_assignment_string_is_renamed():
    src = (
        "from __future__ import annotations\n"
        + _TC_HEAD
        + "    from pkg.sub.mod import Thing\nvalue: 'Thing' = None\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    assert "value: 'mod.Thing' = None" in result.source
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_rewrite.py -v`
Expected: FAIL — every one reports `status == "clean"`, since `_tc_ids` currently makes `_plan_line` return early and silently.

- [ ] **Step 3: Write the implementation**

Add the annotation-string collector to `src/cleanporter/rewrite.py`:

```python
def _annotation_strings(tree: cst.Module) -> dict[int, cst.SimpleString]:
    """String literals sitting in a genuine annotation slot.

    Under ``from __future__ import annotations`` these are never evaluated at
    runtime, so a textual rename inside them is safe.
    """
    found: dict[int, cst.SimpleString] = {}

    def absorb(annotation: cst.Annotation | None) -> None:
        if annotation is None:
            return
        stack: list[cst.CSTNode] = [annotation.annotation]
        while stack:
            node = stack.pop()
            if isinstance(node, cst.SimpleString):
                found[id(node)] = node
            stack.extend(node.children)

    class V(cst.CSTVisitor):
        def visit_FunctionDef(self, node: cst.FunctionDef) -> None:
            params = node.params
            for param in (
                list(params.params)
                + list(params.posonly_params)
                + list(params.kwonly_params)
            ):
                absorb(param.annotation)
            absorb(node.returns)

        def visit_AnnAssign(self, node: cst.AnnAssign) -> None:
            absorb(node.annotation)

    tree.visit(V())
    return found
```

Add `string_repl: dict[int, str] = field(default_factory=dict)` to `_Plan`.

Detect the future import in `_Fixer.visit_Module`, before planning:

```python
        self._future_annotations = any(
            _imports.resolve_parent(imp, self._rec.base_pkg) == "__future__"
            and any(n == "annotations" for n, _a, _x in _imports.imported_names(imp))
            for _line, imp in self._import_lines(node)
        )
```

Initialise `self._future_annotations = False` and `self._string_targets: dict[str, str] = {}` in `__init__`.

Delete `id(imp) in self._tc_ids` from the early-return condition at the top of `_plan_line`, leaving only `if _imports.is_star(imp): return`. Then insert the blocker **immediately after `if not fix: return`**, so that a `TYPE_CHECKING` import with nothing to fix never blocks the file:

```python
        if not fix:
            return
        self._fixed_locals.update(asname or name for name, asname in fix)
        if id(imp) in self._tc_ids and not self._future_annotations:
            self.blockers.append(
                (
                    self._line_of(imp),
                    "TYPE_CHECKING-gated import; rewriting it without "
                    "`from __future__ import annotations` risks NameError",
                )
            )
            return
```

`_build_existing` keeps its own `id(imp) in self._tc_ids` skip: a module bound only under `TYPE_CHECKING` is not available at runtime and must never be reused as a qualifier.

Record the rename targets in the `for name, asname in fix:` loop, next to `self.plan.fixed += 1`:

```python
            self._string_targets[asname or name] = f"{bind}.{name}"
```

Plan the string rewrites in `_run_guards`, before the string guard runs:

```python
    def _run_guards(self, node: cst.Module) -> None:
        if not self._fixed_locals:
            return
        skip_ids = self._plan_annotation_strings(node)
        self.blockers.extend(
            guards.find_string_mentions(
                node, self._fixed_locals, self._line_of, skip_ids=skip_ids
            )
        )
        self.blockers.extend(
            guards.find_scope_declarations(node, self._fixed_locals, self._line_of)
        )

    def _plan_annotation_strings(self, node: cst.Module) -> frozenset[int]:
        """Rename locals inside lazy string annotations; return their ids."""
        if not (self._future_annotations and self._string_targets):
            return frozenset()
        patterns = [
            (re.compile(rf"\b{re.escape(local)}\b"), replacement)
            for local, replacement in sorted(self._string_targets.items())
        ]
        for ident, string_node in _annotation_strings(node).items():
            value = string_node.value
            for pattern, replacement in patterns:
                value = pattern.sub(replacement, value)
            if value != string_node.value:
                self.plan.string_repl[ident] = value
        return frozenset(self.plan.string_repl)
```

Add `import re` at the top. Apply the rewrites:

```python
    def leave_SimpleString(self, original: cst.SimpleString, updated: cst.SimpleString):
        replacement = self.plan.string_repl.get(id(original))
        return updated if replacement is None else updated.with_changes(value=replacement)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest -q`
Expected: PASS — 111 tests.

- [ ] **Step 5: Run the full quality gate**

Run: `uv run ruff check src tests && uv run mypy --strict src/cleanporter`
Expected: ruff clean. mypy should report no more than the handful of libcst-metadata `Any` complaints; fix anything else it finds.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: rewrite TYPE_CHECKING imports when future annotations are active

Blocks otherwise, since an eagerly evaluated annotation would raise NameError.
Lazy string annotations are renamed by local name, so 'Thing as T' correctly
rewrites 'T' rather than looking for 'Thing'."
```

---

### Task 17: Documentation, and retire `3rdparty/modimports`

**Files:**
- Modify: `README.md`
- Delete: `3rdparty/`
- Test: `tests/test_readme.py`

**Interfaces:**
- Consumes: the finished CLI from Task 13.
- Produces: no code. A doctest-style test keeps the README's documented flags and codes honest.

- [ ] **Step 1: Write the failing test**

Create `tests/test_readme.py`:

```python
"""The README must not drift from the actual CLI surface."""

from __future__ import annotations

import re
from pathlib import Path

from cleanporter.cli import build_arg_parser
from cleanporter.config import _KNOWN_KEYS
from cleanporter.model import Status

README = Path(__file__).resolve().parents[1] / "README.md"


def test_every_cli_flag_is_documented():
    text = README.read_text(encoding="utf-8")
    flags = {
        option
        for action in build_arg_parser()._actions
        for option in action.option_strings
        if option.startswith("--") and option not in {"--help"}
    }
    missing = sorted(f for f in flags if f not in text)
    assert missing == [], f"undocumented flags: {missing}"


def test_every_config_key_is_documented():
    text = README.read_text(encoding="utf-8")
    missing = sorted(k for k in _KNOWN_KEYS if k not in text)
    assert missing == [], f"undocumented config keys: {missing}"


def test_every_finding_code_is_documented():
    text = README.read_text(encoding="utf-8")
    codes = {
        "CP001": Status.VIOLATION,
        "CP002": Status.UNRESOLVED,
        "CP003": Status.SKIPPED,
    }
    missing = sorted(c for c in codes if c not in text)
    assert missing == [], f"undocumented finding codes: {missing}"


def test_no_stale_references_to_the_old_tools():
    text = README.read_text(encoding="utf-8")
    assert "modimports" not in text
    assert "3rdparty" not in text
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_readme.py -v`
Expected: FAIL — the current README documents `--fix` only and none of the new flags.

- [ ] **Step 3: Rewrite the README**

Merge the two READMEs. Take cleanporter's structure — intro, install, quickstart, exit codes, finding codes, configuration table, how classification works, fixer safety model, known limitations, development — and fold in the material only modimports had:

- The "Why a runtime resolver" section, updated: layer 1 filesystem (first-party, no side effects, now including `.so`/`.pyd` and ambiguity detection), layer 2 the out-of-process probe against `--python`, layer 3 undetermined means reported and never rewritten.
- The exemptions paragraph: `typing`, `typing_extensions`, `collections.abc`, `__future__` are exempt by the style guide itself; extend with `--exempt` or `exempt_modules`.
- The before/after example block.
- The comparison to `flake8-import-restrictions` IMR241, the pylint plugin, and Ruff issue #5841.

Document every flag from `build_arg_parser` (`--fix`, `--diff`, `--python`, `--exempt`, `--root`, `--strict`, `--version`) and every key in `_KNOWN_KEYS` (`exclude`, `scope`, `source_roots`, `treat_unresolved_as_error`, `exempt_modules`, `exempt_names`, `python`).

Under known limitations, keep both tools' honest caveats: relative imports are rewritten to their absolute form; imports are not re-sorted (use isort/Ruff); wildcard imports are reported but never rewritten; one-liner suites and semicolon-joined statements are reported but never rewritten; type comments are not inspected.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest -q`
Expected: PASS — 115 tests.

- [ ] **Step 5: Delete the vendored tree**

```bash
git rm -r --cached 3rdparty >/dev/null 2>&1 || true
rm -rf 3rdparty
echo "3rdparty/" >> .gitignore  # only if you want to keep a local copy around
uv run pytest -q
```

Expected: PASS — nothing imports from `3rdparty`.

- [ ] **Step 6: Final verification**

```bash
uv run pytest -q
uv run ruff check src tests
uv run mypy --strict src/cleanporter
uv run cleanporter --fix /tmp/bake_check/src   # a scratch copy, not the repo
uv run cleanporter src tests
```

Expected: tests pass, ruff clean, mypy within the agreed baseline, and `cleanporter` runs cleanly over its own source.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "docs: merge both READMEs and retire the vendored modimports tree

Adds README drift tests so the documented flags, config keys, and finding
codes stay in sync with the code."
```

---

## Verification Matrix

Every behaviour the spec requires, and the test that proves it. Run this list against the finished tree before calling the merge done.

**Reconciled 2026-08-27 (Task 17), tree at 166/170 tests:** every row below was
checked against `uv run pytest --collect-only -q` node IDs, not just `grep`
text matches. All 19 rows still name real tests, verbatim, with no renames
needed. No coverage gap was found.

| Spec requirement | Test |
| --- | --- |
| 1 exemptions | `test_cli.py::test_typing_imports_are_exempt`, `test_config.py::test_exempt_modules_extends_rather_than_replaces_defaults` |
| 2 out-of-process probe | `test_resolver.py::test_stdlib_falls_through_to_the_probe`, `test_probe.py` |
| 3 no side effects for first-party | `test_firstparty.py` (whole file — no imports occur) |
| 4 binding reuse / aliasing | `test_rewrite.py::test_collision_with_the_new_module_token_is_aliased_not_broken`, `::test_function_scope_reuses_a_module_level_binding` |
| 5 clean multi-line replacement | `test_rewrite.py::test_trailing_comment_lands_on_the_last_replacement_line` |
| 6 visitor traversal | `test_traversal.py` (whole file) |
| 7 all-or-nothing | `test_rewrite.py::test_a_blocker_suppresses_otherwise_safe_rewrites_in_the_same_file` |
| 8 guards | `test_guards.py` (whole file), `test_rewrite.py::test_dunder_all_blocks_the_whole_file`, `::test_global_declaration_blocks_the_file`, `::test_module_level_rebinding_blocks_the_file` |
| 9 re-parse verification | `test_rewrite.py::test_unparseable_rewrite_is_reverted_and_reported` |
| 10 C-extension submodules | `test_firstparty.py::test_extension_submodule_is_a_module` |
| 11 ambiguity reported | `test_firstparty.py::test_submodule_shadowed_by_an_init_binding_is_ambiguous` |
| 12 config and exclusion | `test_config.py`, `test_discover.py` |
| 13 first-party scope | `test_analyze.py::test_scope_first_party_ignores_stdlib` |
| 14 CLI and exit codes | `test_cli.py` (whole file) |
| 15 non-module-scope fixing | `test_rewrite.py::test_function_scope_import_is_fixed_in_place` |
| 16 TYPE_CHECKING | `test_rewrite.py::test_type_checking_with_future_annotations_is_fixed` |
| 17 trailing comments | `test_rewrite.py::test_trailing_comment_is_preserved` |
| 18 declined violations reported | `test_cli.py::test_fix_still_reports_violations_it_declined` |
| 19 one traversal per file | `test_perf.py` (whole file) |

## Deferred

Not in this plan; open as issues after the merge lands.

- **Probe result caching on disk.** Task 2 removes the traversal overhead; if a large run is still slow, the remaining cost is importing third-party parents. Cache `(interpreter, parent, name) -> verdict` keyed by the parent's mtime.
- **Keeping relative imports relative.** Both tools rewrite `from .sub.mod import C` to the absolute `from pkg.sub import mod`. Preserving the relative form is a contained change to `_module_import_stmt`, but it needs its own tests for the level arithmetic.
- **`six.moves` in the default exemption set.** The style guide lists it; it is dead weight for a 3.10+ codebase. Add it if a user asks.
- **Type comments (`# type: ...`).** Not inspected by either tool.
