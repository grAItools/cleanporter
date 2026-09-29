r"""Which interpreter the probe asks: a named one, cleanporter's own, or the project's.

cleanporter is usually installed on its own -- ``pipx``, ``uv tool`` -- so the
interpreter running it has none of the target project's third-party
packages. Probed there, every third-party import is "not importable" and so
`CP002`: nothing is wrong, and nothing is checked. Asking the user to know
about ``--python`` did not fix that; finding the project's own interpreter
does.

`choose` turns the ``python`` setting (`config.Config.python`, after the
command line's ``--python``) into the interpreter the resolver is given:

* a path or a command is used as written -- a request, so a missing file is
  the probe's failure to report, as it always was;
* ``"self"`` is cleanporter's own interpreter, in process (the behaviour
  before detection existed);
* ``None`` (the key absent) or ``"auto"`` detects. The order is uv's: the
  *project's* environment first, an activated one only when the project has
  none, so a shell that still has some other environment activated does not
  decide which packages the project is checked against. In order:

  1. ``$UV_PROJECT_ENVIRONMENT`` -- where uv keeps the project environment
     when told not to use ``.venv``; a relative value is read against the
     environment root (below);
  2. ``.venv`` in the environment root;
  3. ``$VIRTUAL_ENV`` -- an activated environment.

  The *environment root* is uv's: the project root (the directory of the
  ``pyproject.toml`` in use, or the first path's directory when there is
  none -- `config.Config.root`), unless the project is a member of a uv
  workspace, in which case it is the workspace root and the member's own
  directory is not tried at all, as uv does not. Membership is uv's rule
  too: the *first* ancestor directory holding a ``pyproject.toml`` decides.
  If that file declares ``[tool.uv.workspace]`` with ``members`` globs that
  match the project root and ``exclude`` globs that do not, it is the
  workspace root; anything else -- a plain project above (``examples/demo``
  inside a package), a non-matching workspace, a file that cannot be read --
  means there is none, and the walk goes no further. A project root that
  itself declares a workspace is its own workspace root, whatever any outer
  workspace's ``members`` say.

  The first candidate whose interpreter (``bin/python``, or
  ``Scripts\python.exe`` on Windows) is an executable file wins. A candidate
  that is not one is passed over without a word: detection is a search, not
  a request, so an unset variable and a stale one are the same miss. Nothing
  else is consulted -- no ``uv`` subprocess, no ``PATH`` search, no conda
  (``$CONDA_PREFIX``), and no walk up the tree beyond the one workspace
  lookup, which stops at the first ancestor with a ``pyproject.toml`` -- so the
  choice is cheap and depends only on two environment variables and the
  project's directories. Finding nothing leaves cleanporter's own
  interpreter, exactly as ``"self"`` would.

Detection only decides *which* environment the probe asks; it never adds an
answer the probe did not prove. An interpreter lacking a package reports it
"not importable" (`CP002`), and one that cannot run fails its whole batch
closed with a warning (`resolver`), so the worst a pick without the
project's packages can do is leave imports unresolved, as not detecting did.
Every answer it does give is proven in the environment the note names.

When the detected environment *is* the one cleanporter runs in, the probe
stays in process and there is nothing to say -- unless an active
``$VIRTUAL_ENV`` names a *different* directory, which the note then reports as
passed over, so a shell's activated environment is never ignored silently
(``uv run cleanporter`` sets it to the project's own, and says nothing).
It is cleanporter's own when either its interpreter is
cleanporter's own by path (`is_this_interpreter`), or -- detection only --
its directory is cleanporter's ``sys.prefix`` and that is a virtual
environment. The second covers ``uv run python -m cleanporter``, whose
``sys.executable`` is the venv's ``bin/python3`` rather than the
``bin/python`` detection looks for: two names in one environment's ``bin``,
found through the same ``pyvenv.cfg``, so the same ``sys.path``. It is not
applied to a named interpreter, which keeps the stricter rule. Any other
pick is probed in a subprocess, as if it had been named with ``--python``,
and `Choice.note` says which interpreter was picked and why -- and, when an
activated ``$VIRTUAL_ENV`` was passed over for the project's own, that too --
so a run's verdicts can always be traced to the environment that gave them.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import os
import pathlib
import sys
import tomllib

#: ``python`` value: detect the project's interpreter (what an absent key does).
AUTO = "auto"
#: ``python`` value: cleanporter's own interpreter, probed in process.
SELF = "self"


@dataclasses.dataclass(frozen=True)
class Choice:
    """The interpreter a run probes with, and what to tell the user about it."""

    #: As `resolver.Resolver` takes it: a path or command, or ``None`` for
    #: cleanporter's own interpreter, in process.
    python: str | None
    #: One line naming the interpreter detection picked and why, or -- when
    #: that is cleanporter's own -- the active ``$VIRTUAL_ENV`` it passed
    #: over; ``None`` when there is nothing to say.
    note: str | None = None


def is_this_interpreter(python: str) -> bool:
    """Whether running *python* would give this very environment, provably.

    What an interpreter can import is decided by its *environment*, and a
    virtual environment is found from where its executable is invoked -- the
    ``pyvenv.cfg`` beside it or one level up -- not from the binary it links
    to. Every venv built from one base Python is a symlink to the same file,
    so comparing symlink-*resolved* paths, as this used to, called any other
    venv of the same Python "this interpreter" and probed it in process,
    against cleanporter's own ``sys.path``: a package only the target had was
    "not importable", and one only cleanporter's environment had was
    classified as if the target had it.

    So the comparison is of the path as invoked, made absolute without
    touching a symlink, against ``sys.executable``. Equal strings name the
    same location and so the same environment. The one lexical step that
    could break that is collapsing ``..`` -- ``venv/link/../bin/python``
    reads as ``venv/bin/python`` but, through a symlinked ``link``, runs
    something else -- so a path with a ``..`` component is never called equal.

    A bare command name (no path separator) is never equal either: the
    subprocess looks it up on ``PATH``, whereas making it absolute joins it to
    the cwd, so ``python3`` run from inside a venv's ``bin`` would be answered
    in process while ``PATH`` names some other interpreter entirely.
    Every miss costs a subprocess, never a wrong answer.
    """
    if not any(sep in python for sep in (os.sep, os.altsep) if sep):
        return False
    if not sys.executable or ".." in pathlib.PurePath(python).parts:
        return False
    return str(pathlib.Path(python).absolute()) == sys.executable


def _is_this_environment(venv: pathlib.Path) -> bool:
    """Whether the virtual environment at *venv* is the one cleanporter runs in.

    Only for a *detected* environment (see the module docstring): its
    directory, made absolute with no symlink resolved and no ``..`` collapsed,
    is ``sys.prefix``, and ``sys.prefix`` is a virtual environment's.
    """
    if sys.prefix == sys.base_prefix or ".." in venv.parts:
        return False
    return str(venv.absolute()) == sys.prefix


def choose(python: str | None, root: pathlib.Path) -> Choice:
    """The interpreter for a ``python`` setting of *python* in the project at *root*.

    See the module docstring for the rule.
    """
    if python == SELF:
        return Choice(None)
    if python is not None and python != AUTO:
        return Choice(python)
    for why, venv in _candidates(root.absolute()):
        candidate = _venv_python(venv)
        if not _is_executable_file(candidate):
            continue
        found = str(candidate)
        passed_over = _passed_over(venv)
        if is_this_interpreter(found) or _is_this_environment(venv):
            if passed_over is None:
                return Choice(None)
            return Choice(
                None,
                note=(
                    f"classifying stdlib and third-party imports in process, with the "
                    f"project's own environment {venv} (found from {why}), not the active "
                    f"$VIRTUAL_ENV {passed_over}"
                ),
            )
        also = "" if passed_over is None else f", not the active $VIRTUAL_ENV {passed_over}"
        return Choice(
            found,
            note=(
                f"classifying stdlib and third-party imports with {found}, found from {why}"
                f"{also}; set python = {SELF!r} (--python {SELF}) to use cleanporter's "
                "own interpreter"
            ),
        )
    return Choice(None)


def _passed_over(venv: pathlib.Path) -> str | None:
    """The active ``$VIRTUAL_ENV``, when it is set and is not the directory *venv*."""
    active = os.environ.get("VIRTUAL_ENV")
    if not active:
        return None
    try:
        same = pathlib.Path(active).samefile(venv)
    except OSError:  # one of them is gone: compare where they would be
        same = pathlib.Path(active).resolve() == venv.resolve()
    return None if same else active


def _candidates(root: pathlib.Path) -> list[tuple[str, pathlib.Path]]:
    """The environments detection tries, in order, each with how it was found."""
    found: list[tuple[str, pathlib.Path]] = []
    workspace = _workspace_root(root)
    base, what = (root, "project") if workspace is None else (workspace, "uv workspace")
    uv_env = os.environ.get("UV_PROJECT_ENVIRONMENT")
    if uv_env:
        found.append((f"$UV_PROJECT_ENVIRONMENT in the {what} root {base}", base / uv_env))
    found.append((f"the .venv in the {what} root {base}", base / ".venv"))
    active = os.environ.get("VIRTUAL_ENV")
    if active:
        found.append(("$VIRTUAL_ENV", pathlib.Path(active).absolute()))
    return found


def _workspace_root(root: pathlib.Path) -> pathlib.Path | None:
    """The uv workspace *root* (absolute) is a member of, other than itself; else ``None``.

    uv's rule (``find_workspace`` in uv-workspace): the *first* ancestor
    holding a ``pyproject.toml`` decides, whatever that file says. It is the
    workspace root when it declares ``[tool.uv.workspace]`` whose ``members``
    globs match *root* and whose ``exclude`` globs do not; a plain project, a
    workspace *root* is not a member of, or a file that cannot be read or
    parsed all mean there is none, and the walk goes no further.

    A *root* whose own ``pyproject.toml`` declares ``[tool.uv.workspace]`` is
    a workspace root itself, and uv looks no further up -- even when an outer
    workspace's ``members`` would match it.
    """
    if _uv_workspace_table(root / "pyproject.toml") is not None:
        return None
    for ancestor in root.parents:
        pyproject = ancestor / "pyproject.toml"
        if not pyproject.is_file():
            continue
        table = _uv_workspace_table(pyproject)
        if table is None:
            return None
        relative = root.relative_to(ancestor).parts
        member = any(_glob_matches(g, relative) for g in _globs(table, "members"))
        excluded = any(_glob_matches(g, relative) for g in _globs(table, "exclude"))
        return ancestor if member and not excluded else None
    return None


def _uv_workspace_table(pyproject: pathlib.Path) -> dict[str, object] | None:
    """The ``[tool.uv.workspace]`` table of *pyproject*; ``None`` for none or unreadable."""
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    node: object = data
    for key in ("tool", "uv", "workspace"):
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    if not isinstance(node, dict):
        return None
    return {str(k): v for k, v in node.items()}


def _globs(table: dict[str, object], key: str) -> list[str]:
    """The string entries of *table*'s list *key*; anything else contributes none."""
    value = table.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _glob_matches(pattern: str, parts: tuple[str, ...]) -> bool:
    """Whether the relative path *parts* matches the workspace glob *pattern*.

    Component by component, so ``*`` never crosses a ``/``; a ``**``
    component matches any number of components, as in uv's globs. Components
    are taken literally, as uv takes them: ``"./pkgs/*"`` and ``"pkgs/*/"``
    carry a ``.`` or an empty component no directory name matches, so they
    include nothing.
    """
    return _match_parts(tuple(pattern.split("/")), parts)


def _match_parts(pattern: tuple[str, ...], parts: tuple[str, ...]) -> bool:
    if not pattern:
        return not parts
    head, rest = pattern[0], pattern[1:]
    if head == "**":
        return any(_match_parts(rest, parts[i:]) for i in range(len(parts) + 1))
    return bool(parts) and fnmatch.fnmatchcase(parts[0], head) and _match_parts(rest, parts[1:])


def _venv_python(venv: pathlib.Path) -> pathlib.Path:
    """The interpreter of the virtual environment at *venv*."""
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _is_executable_file(path: pathlib.Path) -> bool:
    """Whether *path* is a file this process may execute; a broken symlink is not."""
    return path.is_file() and os.access(path, os.X_OK)
