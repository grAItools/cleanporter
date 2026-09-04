#!/usr/bin/env python
"""Run ``cleanporter --fix`` over real third-party code and check it still works.

The unit suite proves the fixer does what it is told on inputs someone thought
of. This proves it does not break code nobody thought of. Every safety bug
found in the fixer so far was found here and not there:

* rewriting ``from .exceptions import UsageError as UsageError`` deleted a
  public name and broke ``_pytest`` at import,
* ``import unittest`` inside ``if TYPE_CHECKING:`` was reused as a runtime
  binding, producing ``NameError`` in 191 of libCST's test modules,
* rewriting both halves of a re-export chain left ``libcst.tool.dump``
  pointing at nothing.

None of those is visible in a diff, and none of them fails a parse. They only
show up when the rewritten code is *imported and run*.

## What it checks

Everything is a *differential* check: the same probe runs against the pristine
copy and the rewritten one, and only a **new** failure counts. That is what
makes it usable on third-party code that has its own pre-existing failures --
an optional dependency that is not installed, a test that needs a network, a
platform-specific module. We do not need the corpus to be green, only
unchanged.

1. **Every module imports.** Not just the top-level package: each submodule is
   imported in turn, which is what catches an attribute that a rewrite
   deleted from under another module.
2. **No new undefined names**, via ``ruff --select F821``. Cheap, and it
   catches a reference the fixer failed to qualify.
3. **Bundled test suites still pass**, for the packages that ship one. This is
   the strongest signal by a wide margin, because it actually executes the
   rewritten code.

## Usage

    uv run corpus/run.py                 # install, fix, check
    uv run corpus/run.py --keep          # leave the trees for inspection
    uv run corpus/run.py --skip-install  # reuse an existing --work directory

Exits 0 when the rewritten corpus behaves exactly like the original, 1 on a
regression, 2 if the harness itself could not run.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import pathlib
import shutil
import subprocess
import sys
import textwrap

HERE = pathlib.Path(__file__).parent
MANIFEST = HERE / "packages.txt"
REPO = HERE.parent

#: Packages that ship a runnable test suite inside the wheel, and the paths to
#: skip in it. These are the checks with real teeth; the ignores are for
#: directories that need tooling we deliberately do not install (pyre) or that
#: are non-deterministic (hypothesis-driven fuzzing).
BUNDLED_SUITES: dict[str, tuple[str, ...]] = {
    "libcst": ("libcst/tests/pyre", "libcst/tests/test_fuzz.py"),
}

EXIT_OK, EXIT_REGRESSION, EXIT_ERROR = 0, 1, 2

#: Marks a bundled suite that produced no pass/fail tally at all.
_NO_RESULT = "NO RESULT"


def _packages() -> list[str]:
    lines = MANIFEST.read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]


def _top_level_dirs(tree: pathlib.Path) -> list[pathlib.Path]:
    """Importable top-level packages in *tree* (skipping dist-info and the like)."""
    return sorted(
        d
        for d in tree.iterdir()
        if d.is_dir() and not d.name.endswith((".dist-info", ".data")) and d.name != "__pycache__"
    )


#: Imports every module under one top-level package, reporting failures rather
#: than raising. Run as a subprocess per package so that a module which kills
#: the interpreter outright takes down only its own package's probe.
_IMPORT_PROBE = textwrap.dedent(
    """
    import importlib, json, pkgutil, sys, warnings
    warnings.simplefilter("ignore")
    tree, package = sys.argv[1], sys.argv[2]
    sys.path.insert(0, tree)
    failures = {}
    try:
        root = importlib.import_module(package)
    except BaseException as exc:                      # noqa: BLE001
        print(json.dumps({package: f"{type(exc).__name__}: {exc}"}))
        raise SystemExit(0)
    for info in pkgutil.walk_packages(getattr(root, "__path__", []), package + "."):
        try:
            importlib.import_module(info.name)
        except BaseException as exc:                   # noqa: BLE001
            failures[info.name] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(failures))
    """
)


def _import_failures(tree: pathlib.Path) -> dict[str, str]:
    """Module -> error, for every module in *tree* that will not import."""
    failures: dict[str, str] = {}
    for package in _top_level_dirs(tree):
        proc = subprocess.run(
            [sys.executable, "-c", _IMPORT_PROBE, str(tree), package.name],
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )
        if proc.returncode != 0:
            failures[package.name] = f"probe crashed (exit {proc.returncode})"
            continue
        try:
            failures.update(json.loads(proc.stdout or "{}"))
        except ValueError:
            failures[package.name] = "probe produced unreadable output"
    return failures


def _undefined_names(tree: pathlib.Path) -> collections.Counter[str]:
    """How many ``F821`` findings of each kind *tree* has, keyed without positions.

    Compacting an import block moves every line below it, so a finding the
    rewrite never touched comes back at a different line -- and comparing
    ruff's ``concise`` output lines, which begin ``path:line:col:``, reads
    that as a *new* undefined name. `prompt_toolkit`'s pre-existing
    ``Undefined name `result``` moved from line 953 to 921 and was reported
    as a regression on every run, right under a tally saying ``14 before, 14
    after``.

    The key is therefore the file, the rule and the message -- *what* is
    wrong, never *where*. The JSON format carries those three as fields, and
    unlike ``concise`` it has no trailing ``Found N errors.`` summary, which
    is a line whose text changes whenever any count does.

    Counting rather than collecting is what keeps a *second* occurrence of
    something a file was already flagged for visible: the key repeats, so its
    tally rises above the baseline's and the difference is still reported.
    """
    proc = subprocess.run(
        ["ruff", "check", "--isolated", "--select", "F821", "--output-format", "json", "."],
        cwd=tree,
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        findings = json.loads(proc.stdout or "[]")
    except ValueError:
        findings = None
    if not isinstance(findings, list):
        # Ruff did not run, or wrote something unreadable. An empty tally
        # would compare equal to the other tree's and report "unchanged",
        # turning a check that ran nothing into a pass. The tree path goes in
        # the key so the two sides can never cancel out.
        return collections.Counter({f"{_NO_RESULT}: ruff exit {proc.returncode} in {tree}": 1})
    return collections.Counter(
        f"{_relative(tree, f['filename'])}: {f['code']} {f['message']}" for f in findings
    )


def _relative(tree: pathlib.Path, filename: str) -> str:
    """*filename* as ruff reported it, relative to the tree that produced it.

    The absolute path differs between the pristine and the rewritten copy for
    every single file, so comparing it would make every finding look new.
    """
    try:
        return str(pathlib.Path(filename).relative_to(tree))
    except ValueError:
        return filename


def _suite_result(tree: pathlib.Path, package: str, ignores: tuple[str, ...]) -> str:
    """The pass/fail tally line of *package*'s bundled suite, as a string."""
    command = [
        sys.executable,
        "-m",
        "pytest",
        str(tree / package),
        "-q",
        "--no-header",
        "-p",
        "no:cacheprovider",
        "--continue-on-collection-errors",
    ]
    command += [f"--ignore={tree / path}" for path in ignores]
    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=3600,
        check=False,
        # Inherit the real environment and override only the import path. A
        # hand-built env drops HOME, TMPDIR, LANG and the rest, which makes a
        # real suite fail to run for reasons that have nothing to do with the
        # rewrite -- and a suite that does not run cannot detect anything.
        env={**os.environ, "PYTHONPATH": str(tree)},
    )
    for line in reversed(proc.stdout.splitlines()):
        if " passed" in line or " error" in line or " failed" in line:
            # Strip the trailing wall-clock time, which differs run to run.
            return line.split(" in ")[0].strip()
    # Never a comparable value: two runs that both failed to produce a tally
    # would compare equal and report "unchanged", turning the strongest check
    # here into one that silently executed nothing.
    return f"{_NO_RESULT} (exit {proc.returncode})\n{proc.stdout[-2000:]}"


def _probe(tree: pathlib.Path, label: str) -> dict[str, object]:
    print(f"  probing {label} ...", flush=True)
    return {
        "imports": _import_failures(tree),
        "undefined": _undefined_names(tree),
        # Only for packages actually present: a manifest can drop one, and a
        # suite that never ran must not report "unchanged".
        "suites": {
            p: _suite_result(tree, p, ig) for p, ig in BUNDLED_SUITES.items() if (tree / p).is_dir()
        },
    }


def _report(before: dict[str, object], after: dict[str, object]) -> bool:
    """Print the differences. True when the rewrite changed nothing."""
    ok = True

    # Keyed on the module name, which the rewrite does not move -- unlike the
    # `line:col` the F821 comparison below used to key on. A module that
    # already failed and now fails *differently* is therefore invisible here;
    # comparing the error text instead would flag every failure whose message
    # quotes a file path, because the two trees live at different paths.
    before_imports = dict(before["imports"])  # type: ignore[arg-type]
    after_imports = dict(after["imports"])  # type: ignore[arg-type]
    new_imports = {m: e for m, e in after_imports.items() if m not in before_imports}
    print(f"\nimport failures: {len(before_imports)} before, {len(after_imports)} after")
    if new_imports:
        ok = False
        print(f"  {len(new_imports)} NEW import failure(s):")
        for module, error in sorted(new_imports.items())[:25]:
            print(f"    {module}: {error}")

    before_undefined = collections.Counter(before["undefined"])  # type: ignore[arg-type]
    after_undefined = collections.Counter(after["undefined"])  # type: ignore[arg-type]
    # Counter subtraction keeps only the keys that went *up*, so a finding
    # that merely moved cancels out and a second copy of one that was already
    # there does not.
    new_undefined = after_undefined - before_undefined
    print(
        f"undefined names (F821): {sum(before_undefined.values())} before, "
        f"{sum(after_undefined.values())} after"
    )
    if new_undefined:
        ok = False
        print(f"  {sum(new_undefined.values())} NEW undefined name(s):")
        for finding, count in sorted(new_undefined.items())[:25]:
            print(f"    {finding}{f' (x{count})' if count > 1 else ''}")

    suites = dict(after["suites"])  # type: ignore[arg-type]
    print(f"bundled test suites: {len(suites) or 'none in this corpus'}")
    for package, result in suites.items():
        baseline = dict(before["suites"])[package]  # type: ignore[arg-type]
        # A suite that produced no tally ran nothing, so "unchanged" would be
        # a vacuous pass. Fail loudly instead -- either side counts.
        if _NO_RESULT in str(baseline) or _NO_RESULT in str(result):
            ok = False
            verdict = "DID NOT RUN"
        elif baseline == result:
            verdict = "same"
        else:
            ok = False
            verdict = "CHANGED"
        print(f"  {package}: {verdict}\n    before: {baseline}\n    after:  {result}")
    return ok


class _Tee:
    """Write the report to stdout *and* to a file CI can keep as an artifact.

    A regression names the module and the error, and that is precisely the
    part of a 45-minute log that scrolls away.
    """

    def __init__(self, stream: object, path: pathlib.Path) -> None:
        self._stream = stream
        self._file = path.open("w", encoding="utf-8")

    def write(self, text: str) -> int:
        self._file.write(text)
        self._file.flush()
        return self._stream.write(text)  # type: ignore[attr-defined,no-any-return]

    def flush(self) -> None:
        self._file.flush()
        self._stream.flush()  # type: ignore[attr-defined]

    def close(self) -> None:
        self._file.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--work", default=".corpus", help="scratch directory (default: .corpus)")
    parser.add_argument("--keep", action="store_true", help="do not delete the trees afterwards")
    parser.add_argument(
        "--skip-install", action="store_true", help="reuse the packages already in --work"
    )
    args = parser.parse_args(argv)

    work = pathlib.Path(args.work).resolve()
    original, rewritten = work / "original", work / "rewritten"

    if not args.skip_install:
        if work.exists():
            shutil.rmtree(work)
        original.mkdir(parents=True)
        print(f"installing the corpus into {original} ...", flush=True)
        install = subprocess.run(
            ["uv", "pip", "install", "--quiet", "--target", str(original), *_packages()],
            capture_output=True,
            text=True,
            check=False,
        )
        if install.returncode != 0:
            print(f"corpus: could not install the packages:\n{install.stderr}", file=sys.stderr)
            return EXIT_ERROR
    if not original.is_dir():
        print(f"corpus: {original} does not exist; drop --skip-install", file=sys.stderr)
        return EXIT_ERROR

    original_stdout = sys.stdout
    tee = _Tee(original_stdout, work / "report.txt")
    sys.stdout = tee  # type: ignore[assignment]

    files = sum(1 for _ in original.rglob("*.py"))
    print(f"corpus: {files} Python files")

    if rewritten.exists():
        shutil.rmtree(rewritten)
    shutil.copytree(original, rewritten)

    print("running cleanporter --fix over the copy ...", flush=True)
    fix = subprocess.run(
        [sys.executable, "-m", "cleanporter", "--fix", "."],
        cwd=rewritten,
        capture_output=True,
        text=True,
        check=False,
    )
    # 0 = clean, 1 = findings remain (expected: third-party code is full of
    # them). Anything else is the tool failing to run at all.
    if fix.returncode not in (0, 1):
        print(
            f"corpus: cleanporter failed (exit {fix.returncode}):\n{fix.stderr[-4000:]}",
            file=sys.stderr,
        )
        return EXIT_ERROR
    changed = sum(1 for ln in fix.stderr.splitlines() if ln.startswith("fixed: "))
    print(f"corpus: rewrote {changed} file(s)")

    try:
        ok = _report(_probe(original, "original"), _probe(rewritten, "rewritten"))

        if not args.keep:
            shutil.rmtree(rewritten, ignore_errors=True)

        print(
            "\ncorpus: OK, the rewrite changed no observable behaviour"
            if ok
            else "\ncorpus: REGRESSION -- the rewrite changed behaviour, see above"
        )
    finally:
        # Restore first, then close: anything printed after this point (a
        # traceback on the way out) must still reach the real stdout.
        sys.stdout = original_stdout
        tee.close()
    return EXIT_OK if ok else EXIT_REGRESSION


if __name__ == "__main__":
    sys.exit(main())
