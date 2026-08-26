"""Command-line interface for cleanporter."""

from __future__ import annotations

import argparse
import fnmatch
import sys
from pathlib import Path

from cleanporter import __version__
from cleanporter.checker import CP001, CP002, Finding, check_module
from cleanporter.config import Config, ConfigError, load_config
from cleanporter.fixer import FixOutcome, fix_file
from cleanporter.resolver import Origin, Resolver, discover_source_roots

_EXIT_OK = 0
_EXIT_VIOLATIONS = 1
_EXIT_ERROR = 2

_ALWAYS_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "venv",
    "node_modules",
    "build",
    "dist",
}


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
            "  cleanporter --fix pkg/module.py\n\n"
            "exit codes: 0 ok, 1 violations remain, 2 operational error.\n"
            "configure exclusions etc. under [tool.cleanporter] in pyproject.toml."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("paths", nargs="*", default=["."], help="files or directories to process")
    parser.add_argument("--fix", action="store_true", help="rewrite violating imports where provably safe")
    parser.add_argument("--version", action="version", version=f"cleanporter {__version__}")
    return parser


def iter_python_files(paths: list[str], config: Config) -> tuple[list[Path], list[str]]:
    """Expand inputs into a de-duplicated sorted file list plus warnings."""
    warnings: list[str] = []
    seen: set[Path] = set()
    out: list[Path] = []

    def excluded(p: Path) -> bool:
        try:
            rel = p.relative_to(config.root).as_posix()
        except ValueError:
            rel = p.as_posix()
        abs_posix = p.resolve().as_posix()
        for pattern in config.exclude:
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(abs_posix, pattern):
                return True
            if any(ch in pattern for ch in "*?["):
                continue
            # Literal patterns match the entry itself or any descendant.
            lit = pattern.rstrip("/")
            if rel == lit or rel.startswith(lit + "/"):
                return True
        return False

    for raw in paths:
        root_path = Path(raw)
        if not root_path.exists():
            warnings.append(f"path does not exist: {raw}")
            continue
        candidates = (
            [root_path]
            if root_path.is_file()
            else sorted(
                p
                for p in root_path.rglob("*.py")
                if not any(part in _ALWAYS_SKIP_DIRS or part.startswith(".") for part in p.parts)
            )
        )
        for cand in candidates:
            resolved = cand.resolve()
            if resolved in seen or excluded(cand):
                continue
            seen.add(resolved)
            out.append(cand)
    return out, warnings


def _read_source(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError as exc:
        print(f"cleanporter: cannot read {path}: {exc}", file=sys.stderr)
        return None
    return data.decode("utf-8")


def _write_source(path: Path, source: str) -> bool:
    text = source.replace("\r\n", "\n")
    try:
        path.write_text(text, encoding="utf-8", newline="\n")
    except OSError as exc:
        print(f"cleanporter: cannot write {path}: {exc}", file=sys.stderr)
        return False
    return True


def run(args: argparse.Namespace) -> int:
    anchor = Path(args.paths[0] if args.paths else ".").resolve()
    if anchor.is_file():
        anchor = anchor.parent
    try:
        config = load_config(anchor)
    except ConfigError as exc:
        print(f"cleanporter: configuration error: {exc}", file=sys.stderr)
        return _EXIT_ERROR

    project_roots = discover_source_roots(config.root, config.source_roots)
    extra = [(root, Origin.THIRD_PARTY) for root in Resolver.default_site_packages()]
    resolver = Resolver(project_roots, runtime_fallback=config.runtime_fallback, extra_roots=extra)

    files, warnings = iter_python_files(list(args.paths), config)
    for warning in warnings:
        print(f"cleanporter: warning: {warning}")

    parse_failures: list[Finding] = []
    all_findings: dict[Path, list[Finding]] = {}
    contexts_map: dict[Path, object] = {}

    checked = 0
    for path in files:
        source = _read_source(path)
        if source is None:
            continue
        try:
            import libcst as cst

            module = cst.parse_module(source)
        except Exception as exc:  # noqa: BLE001 - surface any parser failure
            line = getattr(exc, "editor_line", 0) or 0
            msg = f"syntax error: {(getattr(exc, 'message', '') or str(exc)).splitlines()[0]}"
            parse_failures.append(Finding(path, max(line, 1), 0, "CP999", msg))
            continue
        checked += 1
        findings, contexts = check_module(module, path, resolver, config, project_roots)
        all_findings[path] = findings
        contexts_map[path] = (module, source)

    fixed_files: list[tuple[Path, str]] = []
    fix_skips: list[Finding] = []
    remaining: dict[Path, list[Finding]] = {}

    if args.fix:
        for path, findings in all_findings.items():
            if not any(f.code == CP001 for f in findings):
                remaining[path] = findings
                continue
            source = contexts_map[path][1]
            outcome: FixOutcome = fix_file(
                source,
                path,
                resolver=resolver,
                config=config,
                project_roots=project_roots,
            )
            if outcome.status == "fixed" and outcome.new_source is not None:
                if _write_source(path, outcome.new_source):
                    fixed_files.append((path, outcome.diff or ""))
                else:
                    remaining[path] = findings
            elif outcome.status == "skipped":
                blocker_lines = {b.line for b in outcome.skips}
                remaining[path] = [f for f in findings if f.line not in blocker_lines]
                fix_skips.extend(outcome.skips)
            else:  # "error"
                fix_skips.extend(outcome.skips)
                remaining[path] = findings

    emitted_count = _print_report(
        parse_failures=parse_failures,
        remaining=remaining if args.fix else all_findings,
        fixed_files=fixed_files,
        fix_skips=fix_skips,
    )

    reported = remaining if args.fix else all_findings
    violation_count = sum(1 for f in _values(reported) if f.code == CP001)
    warning_count = sum(1 for f in _values(reported) if f.code == CP002)
    del emitted_count
    print()
    print(
        f"checked {checked} file(s)"
        + (f", fixed {len(fixed_files)}" if args.fix else "")
        + f": {violation_count} violation(s), {warning_count} unresolved-target warning(s)"
    )

    if parse_failures:
        return _EXIT_ERROR
    hard = violation_count + (warning_count if config.treat_unresolved_as_error else 0)
    if hard > 0:
        return _EXIT_VIOLATIONS
    return _EXIT_OK


def _values(mapping: dict[Path, list[Finding]]):
    for findings in mapping.values():
        yield from findings


def _print_report(
    *,
    parse_failures: list[Finding],
    remaining: dict[Path, list[Finding]],
    fixed_files: list[tuple[Path, str]],
    fix_skips: list[Finding],
) -> int:
    for finding in parse_failures:
        print(finding.format())
    for path, diff in fixed_files:
        print(f"fixed: {path}")
        sys.stdout.write(diff)
    for finding in fix_skips:
        print(finding.format())
    count = len(parse_failures)
    for path in sorted(remaining):
        for finding in sorted(remaining[path], key=lambda f: (f.line, f.col)):
            print(finding.format())
            count += 1
    return count


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if not args.paths:
        parser.error("at least one path is required")
    try:
        return run(args)
    except KeyboardInterrupt:  # pragma: no cover
        return _EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
