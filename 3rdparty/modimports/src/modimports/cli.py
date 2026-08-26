"""Command-line interface: ``modimports check`` and ``modimports fix``."""

from __future__ import annotations

import difflib
import sys
from pathlib import Path

import typer

from .analyze import analyze_record, build
from .config import DEFAULT_EXEMPT_MODULES, Config
from .model import Status
from .rewrite import fix_record

app = typer.Typer(
    add_completion=False,
    help="Enforce Google Python Style Guide 2.2: import modules, not their members.",
    no_args_is_help=True,
)


def _make_config(python: str | None, exempt: list[str], root: list[str]) -> Config:
    return Config(
        exempt_modules=DEFAULT_EXEMPT_MODULES | frozenset(exempt),
        python=python,
        extra_roots=tuple(root),
    )


@app.command()
def check(
    paths: list[Path] = typer.Argument(..., help="Files or directories to scan."),
    python: str = typer.Option(None, "--python", help="Interpreter for the module probe (default: current)."),
    exempt: list[str] = typer.Option([], "--exempt", help="Extra module whose members may be imported."),
    root: list[str] = typer.Option([], "--root", help="Extra first-party import root."),
    strict: bool = typer.Option(False, "--strict", help="Also fail on undetermined imports."),
) -> None:
    """Report imports of objects that should be module imports. Non-zero on violations."""
    config = _make_config(python, exempt, root)
    records, resolver, errors = build(paths, config)

    findings = list(errors)
    for rec in records:
        findings.extend(analyze_record(rec, resolver, config))
    findings.sort(key=lambda f: (str(f.path), f.line, f.column))

    for f in findings:
        typer.echo(f.format())

    n_viol = sum(f.status is Status.VIOLATION for f in findings)
    n_unfix = sum(f.status is Status.UNFIXABLE for f in findings)
    n_unknown = sum(f.status is Status.UNKNOWN for f in findings)
    typer.echo(
        f"\n{n_viol} violation(s), {n_unfix} unfixable, {n_unknown} undetermined "
        f"across {len(records)} file(s).",
        err=True,
    )
    fail = n_viol or n_unfix or (strict and n_unknown)
    raise typer.Exit(1 if fail else 0)


@app.command()
def fix(
    paths: list[Path] = typer.Argument(..., help="Files or directories to fix."),
    write: bool = typer.Option(False, "--write", "-w", help="Apply changes in place (default: show diff)."),
    python: str = typer.Option(None, "--python", help="Interpreter for the module probe (default: current)."),
    exempt: list[str] = typer.Option([], "--exempt", help="Extra module whose members may be imported."),
    root: list[str] = typer.Option([], "--root", help="Extra first-party import root."),
) -> None:
    """Rewrite object imports to module imports and qualify their uses."""
    config = _make_config(python, exempt, root)
    records, resolver, errors = build(paths, config)

    total_fixed = 0
    changed_files = 0
    for rec in records:
        new_source, fixed = fix_record(rec, resolver, config)
        if fixed == 0 or new_source == rec.source:
            continue
        total_fixed += fixed
        changed_files += 1
        if write:
            rec.path.write_text(new_source, encoding="utf-8")
            typer.echo(f"fixed {rec.path} ({fixed} import(s))", err=True)
        else:
            diff = difflib.unified_diff(
                rec.source.splitlines(keepends=True),
                new_source.splitlines(keepends=True),
                fromfile=str(rec.path),
                tofile=str(rec.path),
            )
            sys.stdout.writelines(diff)

    # surface anything that could not be fixed automatically
    remaining = []
    for rec in records:
        remaining.extend(
            f for f in analyze_record(rec, resolver, config)
            if f.status in (Status.UNFIXABLE, Status.UNKNOWN)
        )

    verb = "fixed" if write else "would fix"
    typer.echo(
        f"\n{verb} {total_fixed} import(s) in {changed_files} file(s); "
        f"{len(remaining)} left for manual review.",
        err=True,
    )
    for f in remaining:
        typer.echo(f.format(), err=True)
    if not write:
        typer.echo("\n(run again with --write to apply; then run isort/ruff to tidy imports)", err=True)


def main() -> None:  # entry point
    app()


if __name__ == "__main__":
    main()
