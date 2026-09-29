"""Analysis driver: turn source files into :class:`Finding` objects.

`FileRecord` carries a parsed file and lazily caches what is expensive to
derive from it -- its `FileFacts`, the import units, where each import
starts, libcst's position metadata when something needs it, whatever
`cleanporter.skip` takes out of the file and its inline suppressions
(`cleanporter.suppress`) -- so a record survives being
analysed more than once (`engine.run` re-parses into a fresh record after a
fix and analyses it again). Building the records of a run, and the resolver
they are analysed against, is `project.build`'s job.

Visitor dispatch over a libcst tree is most of what a check costs, so a check
walks each tree exactly once (`collect_facts`) and derives every other answer
from what that walk kept. The module-level helpers (`iter_units`,
`module_bindings`, `attribute_pairs`, ...) answer one question each for a
caller holding only a tree; each is a fresh walk, so nothing on the run's path
calls them.

A file a `skip` rule takes whole is still discovered, read and parsed here. It
contributes its imports as *evidence* -- who re-exports what, which directories
are packages -- and only its findings are suppressed. That is the whole
difference between `skip` and ``exclude``: dropping a `conftest.py` at
discovery would stop it counting as an importer of the fixtures it pulls in,
which can unblock an unsafe rewrite somewhere else entirely.
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib
import warnings
from collections.abc import Iterator, Mapping

import libcst as cst
from libcst import metadata

from cleanporter import aliases, config, firstparty, model
from cleanporter import resolver as resolver_lib
from cleanporter import skip as skip_lib
from cleanporter import suppress as suppress_lib

from . import _imports, guards


@dataclasses.dataclass
class ImportUnit:
    node: cst.ImportFrom
    parent: str | None  # resolved absolute module, or None if unresolved
    name: str
    asname: str | None
    alias: cst.ImportAlias | None
    star: bool


@dataclasses.dataclass
class FileRecord:
    path: pathlib.Path
    source: str
    tree: cst.Module
    base_pkg: str
    #: This file's own dotted module name (``""`` when it is not under a
    #: known import root). Needed to ask whether *this* module's imports are
    #: load-bearing re-exports for other files in the run.
    qualname: str = ""
    #: Project root, against which `skip` file patterns are matched.
    root: pathlib.Path = dataclasses.field(default_factory=pathlib.Path.cwd)
    #: Configured skip rules. Empty is the common case and costs nothing.
    skip_rules: tuple[skip_lib.Rule, ...] = ()
    #: The codec the file was decoded with (``utf-8-sig`` keeps a BOM), so
    #: `--fix` writes it back the way it was. See `cleanporter._source`.
    encoding: str = "utf-8"
    #: The file's bytes as read, or ``None`` for a record not built from disk.
    #: Diffs are computed against these, and the fixer declines a file whose
    #: decoded text would not encode back to them.
    raw: bytes | None = dataclasses.field(default=None, repr=False, compare=False)
    #: Whether the file is anchored in an import root the user *declared*
    #: and that passes `firstparty.ModuleMap.root_for_absolute_spelling`,
    #: which is what lets a top-level package's ``from . import C`` be
    #: spelled ``import pkg`` (`_imports.module_import_spelling`).
    declared_root: bool = False
    #: The *inferred* root the file is anchored in, when declaring it would
    #: do the same; named in that import's `CP003` (`_imports.unspellable_reason`).
    root_hint: str = ""
    #: The tree's `FileFacts`, when the caller already walked it for them
    #: (`project.build` must, before it can know ``base_pkg``); otherwise
    #: collected on first use.
    _facts: FileFacts | None = dataclasses.field(default=None, repr=False, compare=False)
    _units: list[ImportUnit] | None = dataclasses.field(default=None, repr=False, compare=False)
    _positions: Mapping[cst.CSTNode, metadata.CodeRange] | None = dataclasses.field(
        default=None, repr=False, compare=False
    )
    _import_starts: Mapping[cst.ImportFrom, tuple[int, int]] | None = dataclasses.field(
        default=None, repr=False, compare=False
    )
    _plain_import_starts: Mapping[cst.Import, tuple[int, int]] | None = dataclasses.field(
        default=None, repr=False, compare=False
    )
    _skipped: skip_lib.Skipped | None = dataclasses.field(default=None, repr=False, compare=False)
    _suppressions: suppress_lib.Suppressions | None = dataclasses.field(
        default=None, repr=False, compare=False
    )

    @property
    def facts(self) -> FileFacts:
        """Everything the analysis reads off the tree, from one walk. Computed once."""
        if self._facts is None:
            self._facts = collect_facts(self.tree)
        return self._facts

    @property
    def units(self) -> list[ImportUnit]:
        """Every ``from`` import in the file. Computed once."""
        if self._units is None:
            self._units = list(self.facts.units(self.base_pkg))
        return self._units

    @property
    def import_starts(self) -> Mapping[cst.ImportFrom, tuple[int, int]]:
        r"""``(line, column)`` where each ``from`` import starts, as libcst counts them.

        Exactly ``positions[node].start`` for every node in `facts`, and
        taken from there whenever `positions` is resolved anyway -- already,
        or because `skipped` will need it (a record with `skip_rules`).
        Otherwise read off an ``ast`` parse (`_import_from_starts`), because
        resolving ``PositionProvider`` is a code generation pass over the whole
        tree with position tracking -- as costly as the walk itself
        -- to learn the position of a handful of statements. A file that
        path cannot vouch for -- ``\r`` line endings, a ``\f`` or a backslash
        continuation that libcst does not reproduce, a grammar the running
        Python rejects -- falls back to the metadata, so the answer is
        libcst's either way.
        """
        if self._import_starts is None:
            self._import_starts = self._starts(self.facts.import_froms, ast.ImportFrom, "from")
        return self._import_starts

    @property
    def plain_import_starts(self) -> Mapping[cst.Import, tuple[int, int]]:
        """`import_starts` for every plain ``import`` statement. Computed once.

        Only the alias check (`CP006`) needs these, so a run with no alias
        rule never computes them.
        """
        if self._plain_import_starts is None:
            self._plain_import_starts = self._starts(self.facts.plain_imports, ast.Import, "import")
        return self._plain_import_starts

    def _starts[N: (cst.Import, cst.ImportFrom)](
        self, nodes: tuple[N, ...], kind: type[ast.stmt], keyword: str
    ) -> Mapping[N, tuple[int, int]]:
        """Where each of *nodes* starts: see `import_starts`."""
        starts = (
            _statement_starts(self.source, self.tree, nodes, kind, keyword)
            if self._positions is None and not self.skip_rules
            else None
        )
        if starts is None:
            positions = self.positions
            starts = {
                node: (positions[node].start.line, positions[node].start.column) for node in nodes
            }
        return starts

    @property
    def skipped(self) -> skip_lib.Skipped:
        """What `skip_rules` take out of this file. Computed once.

        Deliberately lazy *and* short-circuited: with no rules configured this
        never walks the tree and never forces `positions`, so the whole
        feature is free for anyone not using it.
        """
        if self._skipped is None:
            self._skipped = (
                skip_lib.regions(
                    self.tree,
                    self.positions,
                    self.skip_rules,
                    skip_lib.file_candidates(self.path, self.root),
                    self.qualname,
                )
                if self.skip_rules
                else skip_lib.EMPTY
            )
        return self._skipped

    @property
    def suppressions(self) -> suppress_lib.Suppressions:
        """This file's ``# cleanporter: ignore[...]`` comments. Computed once.

        Free for a file `suppress.may_hold` rules out: nothing is walked
        and `positions` is not forced.
        """
        if self._suppressions is None:
            self._suppressions = (
                suppress_lib.collect(self.tree, self.positions)
                if suppress_lib.may_hold(self.source)
                else suppress_lib.EMPTY
            )
        return self._suppressions

    @property
    def positions(self) -> Mapping[cst.CSTNode, metadata.CodeRange]:
        """``PositionProvider`` mapping for this tree. Resolved once."""
        if self._positions is None:
            self._positions = metadata.MetadataWrapper(self.tree, unsafe_skip_copy=True).resolve(
                metadata.PositionProvider
            )
        return self._positions


def _import_from_starts(
    source: str, tree: cst.Module, nodes: tuple[cst.ImportFrom, ...]
) -> dict[cst.ImportFrom, tuple[int, int]] | None:
    """Where each of *nodes*, every ``ImportFrom`` of *tree*, starts: `_statement_starts`."""
    return _statement_starts(source, tree, nodes, ast.ImportFrom, "from")


def _statement_starts[N: (cst.Import, cst.ImportFrom)](
    source: str, tree: cst.Module, nodes: tuple[N, ...], kind: type[ast.stmt], keyword: str
) -> dict[N, tuple[int, int]] | None:
    r"""Where each of *nodes* starts, read off an ``ast`` parse; ``None`` if unsure.

    Written for ``ImportFrom`` (*kind* `ast.ImportFrom`, *keyword* ``from``),
    and equally true of a plain ``Import`` (`ast.Import`, ``import``): the
    reasoning below is about the statement's first keyword, whichever it is.

    *nodes* are every ``ImportFrom`` of *tree*, the libcst tree parsed from
    *source*, in source order. libcst's ``PositionProvider`` puts such a
    node's start at its ``from`` keyword *in the code the tree generates*,
    counting a line per newline and a column in characters. ``ast`` puts its
    ``ImportFrom`` at the same keyword *in source*, with a line per newline
    the tokenizer sees and a column in UTF-8 bytes. So the two agree whenever:

    * the generated code is *source*. libcst means to round-trip exactly, but
      1.9 does not always: a form feed (``\f``) or a backslash continuation
      in a statement's leading whitespace is dropped, so every position after
      it on that line -- or the line itself -- moves. The tree's own
      ``code`` is compared with *source* rather than trusting a list of
      known shapes; it is a code generation pass without position tracking,
      a fraction of what resolving the metadata costs;
    * the newlines are the same ones. Both count ``\n``, ``\r\n`` and a lone
      ``\r``, but libcst counts per generated token, and whether a ``\r\n``
      can straddle two tokens is not something to prove here: any ``\r`` in
      the file and this declines, leaving a plain ``\n`` count on both sides;
    * the column is converted from bytes to characters, on the line split
      the same way (on ``\n``); and
    * the statements are the same ones. Both are source order and must be as
      many, and each converted position must land on the text ``from``.

    Anything else -- a file ``ast`` cannot parse (a newer grammar libcst
    accepts, a NUL byte), a count or keyword mismatch, text that will not
    encode -- is ``None``, and the caller resolves the metadata instead. The
    fast path either gives libcst's answer or no answer.
    """
    if not nodes:
        return {}
    if "\r" in source or tree.code != source:
        return None
    try:
        with warnings.catch_warnings():
            # An invalid escape sequence is a SyntaxWarning at parse time; it
            # is the file's business, not this report's.
            warnings.simplefilter("ignore")
            parsed = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    found = sorted(
        (node.lineno, node.col_offset) for node in ast.walk(parsed) if isinstance(node, kind)
    )
    if len(found) != len(nodes):
        return None
    lines = source.split("\n")
    starts: dict[N, tuple[int, int]] = {}
    for node, (lineno, offset) in zip(nodes, found, strict=True):
        text = lines[lineno - 1]
        try:
            column = len(text.encode("utf-8")[:offset].decode("utf-8"))
        except UnicodeError:
            return None
        if not text.startswith(keyword, column):
            return None
        starts[node] = (lineno, column)
    return starts


def package_of(
    path: pathlib.Path, module_map: firstparty.ModuleMap, relative_level: int = 0
) -> str:
    """Package containing ``path`` (``""`` for a top-level module).

    ``relative_level`` is the deepest relative import in the file; it tells
    `ModuleMap.qualname_for` how deep this file must sit, which is the only
    evidence that separates a real import root from a PEP 420 namespace
    directory that merely looks like one.
    """
    qn = module_map.qualname_for(path, relative_level)
    if qn is None:
        return ""
    if path.name == "__init__.py":
        return qn
    return qn.rsplit(".", 1)[0] if "." in qn else ""


@dataclasses.dataclass(frozen=True)
class FileFacts:
    """Everything the analysis reads off a file's tree, collected in one walk.

    Walking a libCST tree is most of what a check costs -- visitor dispatch
    runs per node -- and the analysis used to walk each tree once per
    question: imports, import heads, relative depth, module bindings,
    attribute reads, star imports. `collect_facts` answers all of them in a
    single pass and keeps only what they are computed from.

    Nothing here depends on the file's package, which is not known yet when
    the tree is first walked: it takes every file's absolute imports to
    settle the import roots (`firstparty.ModuleMap.demote_roots`) before any
    relative import can be anchored. So the facts are *raw* -- the import
    statements themselves, and each attribute read as the dotted text it
    reads through -- and the methods taking ``base_pkg`` resolve them
    against the package afterwards without touching the tree again.
    """

    #: Every ``import`` and ``from ... import`` statement, in source order.
    #: Order matters: a later binding of a name replaces an earlier one.
    imports: tuple[cst.Import | cst.ImportFrom, ...]
    #: ``(prefix, attribute)`` for every ``prefix.attribute`` whose prefix is
    #: a plain dotted name (``a.b.c`` gives ``("a.b", "c")`` and ``("a",
    #: "b")``). Reads through a call or a subscript are not dotted names and
    #: are not recorded.
    attribute_reads: frozenset[tuple[str, str]]
    #: Every string literal whose whole text is a dotted or entry-point path
    #: (`guards.dotted_reference`), with that text and its components, in
    #: source order: a plain string, an f-string with no placeholders, or an
    #: implicit concatenation of those (`_literal_text`). The raw material
    #: for the cross-file string evidence (`project._named`).
    path_strings: tuple[tuple[cst.BaseExpression, str, tuple[str, ...]], ...] = ()
    #: Every ``from ... import`` statement inside a ``def`` or a ``class``
    #: body, however deep. The rest are *module level* -- including those
    #: under a module-level ``if``, ``try`` or ``with``, which bind module
    #: attributes all the same. A package ``__init__``'s module-level imports
    #: are its public surface (`Decider.public_surface`).
    nested_import_froms: frozenset[cst.ImportFrom] = frozenset()
    #: Every plain ``import`` statement inside a ``def`` or a ``class`` body:
    #: `nested_import_froms` for the other kind of statement.
    nested_plain_imports: frozenset[cst.Import] = frozenset()

    @property
    def import_froms(self) -> tuple[cst.ImportFrom, ...]:
        """Every ``from ... import`` statement, in source order."""
        return tuple(node for node in self.imports if isinstance(node, cst.ImportFrom))

    @property
    def plain_imports(self) -> tuple[cst.Import, ...]:
        """Every plain ``import`` statement, in source order."""
        return tuple(node for node in self.imports if isinstance(node, cst.Import))

    def absolute_import_heads(self) -> set[str]:
        """See the module-level `absolute_import_heads`."""
        heads: set[str] = set()
        for node in self.imports:
            if isinstance(node, cst.ImportFrom):
                if _imports.relative_level(node) == 0:
                    heads.add(_imports.dotted(node.module).split(".")[0])
            else:
                heads.update(_imports.dotted(alias.name).split(".")[0] for alias in node.names)
        heads.discard("")
        return heads

    def max_relative_level(self) -> int:
        """See the module-level `max_relative_level`."""
        return max((_imports.relative_level(node) for node in self.import_froms), default=0)

    def units(self, base_pkg: str) -> Iterator[ImportUnit]:
        """See `iter_units`."""
        for node in self.import_froms:
            parent = _imports.resolve_parent(node, base_pkg)
            if _imports.is_star(node):
                yield ImportUnit(node, parent, "*", None, None, star=True)
                continue
            for name, asname, alias in _imports.imported_names(node):
                yield ImportUnit(node, parent, name, asname, alias, star=False)

    def module_bindings(self, base_pkg: str) -> dict[str, str]:
        """See the module-level `module_bindings`."""
        bound: dict[str, str] = {}
        for node in self.imports:
            if isinstance(node, cst.Import):
                for alias in node.names:
                    dotted = _imports.dotted(alias.name)
                    if alias.asname is not None and isinstance(alias.asname.name, cst.Name):
                        bound[alias.asname.name.value] = dotted
                    else:
                        # ``import a.b`` binds only ``a``, which names ``a``.
                        head = dotted.split(".")[0]
                        bound[head] = head
                continue
            parent = _imports.resolve_parent(node, base_pkg)
            if parent is None:
                continue
            for name, asname, _alias in _imports.imported_names(node):
                bound[asname or name] = f"{parent}.{name}"
        return bound

    def attribute_pairs(self, base_pkg: str) -> set[tuple[str, str]]:
        """See the module-level `attribute_pairs`."""
        bound = self.module_bindings(base_pkg)
        found: set[tuple[str, str]] = set()
        for prefix, attr in self.attribute_reads:
            head, _dot, rest = prefix.partition(".")
            target = bound.get(head)
            if target is None:
                continue
            module = f"{target}.{rest}" if rest else target
            found.add((module, attr))
        return found

    def star_imported_modules(self, base_pkg: str) -> set[str]:
        """See the module-level `star_imported_modules`."""
        found: set[str] = set()
        for node in self.import_froms:
            if _imports.is_star(node):
                parent = _imports.resolve_parent(node, base_pkg)
                if parent is not None:
                    found.add(parent)
        return found


def _literal_text(node: cst.SimpleString | cst.FormattedString) -> str | None:
    """The runtime text of *node* when it is written out literally, else ``None``.

    A path has no escapes, so text holding a backslash is not one, and
    without one the raw text *is* the value -- no evaluation needed. An
    f-string counts only with no placeholders (``f"pkg.mod.helper"``); a
    brace means a placeholder or an escaped brace, neither of which a path
    has. Bytes are not paths anything imports by.
    """
    if "b" in node.prefix.lower():
        return None
    if isinstance(node, cst.SimpleString):
        text = node.raw_value
    else:
        pieces: list[str] = []
        for part in node.parts:
            if not isinstance(part, cst.FormattedStringText):
                return None
            pieces.append(part.value)
        text = "".join(pieces)
    return None if "\\" in text or "{" in text or "}" in text else text


class _FactCollector(cst.CSTVisitor):
    """The one walk `collect_facts` makes. Every hook returns ``None``: descend."""

    def __init__(self) -> None:
        super().__init__()
        self.imports: list[cst.Import | cst.ImportFrom] = []
        self.attribute_reads: set[tuple[str, str]] = set()
        self.path_strings: list[tuple[cst.BaseExpression, str, tuple[str, ...]]] = []
        #: Ids of strings that are parts of a concatenation, read with it.
        self._string_parts: set[int] = set()
        #: Every ``from`` import inside a ``def`` or ``class`` body.
        self.nested_import_froms: set[cst.ImportFrom] = set()
        #: Every plain ``import`` inside a ``def`` or ``class`` body.
        self.nested_plain_imports: set[cst.Import] = set()
        #: How many ``def``/``class`` bodies the walk is inside.
        self._depth = 0

    # libcst dispatches to these exact signatures; only the nesting matters.
    def visit_FunctionDef(self, node: cst.FunctionDef) -> None:  # noqa: ARG002
        self._depth += 1

    def leave_FunctionDef(self, original_node: cst.FunctionDef) -> None:  # noqa: ARG002
        self._depth -= 1

    def visit_ClassDef(self, node: cst.ClassDef) -> None:  # noqa: ARG002
        self._depth += 1

    def leave_ClassDef(self, original_node: cst.ClassDef) -> None:  # noqa: ARG002
        self._depth -= 1

    def visit_SimpleString(self, node: cst.SimpleString) -> None:
        if id(node) not in self._string_parts:
            self._path_string(node, _literal_text(node))

    def visit_FormattedString(self, node: cst.FormattedString) -> None:
        if id(node) not in self._string_parts:
            self._path_string(node, _literal_text(node))

    def visit_ConcatenatedString(self, node: cst.ConcatenatedString) -> None:
        # ``"pkg.mod." "helper"`` is one string at runtime, so it is read
        # whole, once: its parts -- nested concatenations included -- are
        # marked so their own visits do not read a fragment as a path.
        # Descent is not cut short: an f-string's expressions still hold
        # attribute reads.
        if id(node) in self._string_parts:
            return
        texts: list[str | None] = []
        pending: list[cst.BaseExpression] = [node.left, node.right]
        while pending:
            part = pending.pop(0)
            self._string_parts.add(id(part))
            if isinstance(part, cst.ConcatenatedString):
                pending[:0] = [part.left, part.right]
            elif isinstance(part, (cst.SimpleString, cst.FormattedString)):
                texts.append(_literal_text(part))
            else:  # pragma: no cover - libcst admits nothing else here
                texts.append(None)
        if all(t is not None for t in texts):
            self._path_string(node, "".join(t for t in texts if t is not None))

    def _path_string(self, node: cst.BaseExpression, text: str | None) -> None:
        if text is None:
            return
        parts = guards.dotted_reference(text)
        if parts is not None:
            self.path_strings.append((node, text, parts))

    def visit_Import(self, node: cst.Import) -> None:
        self.imports.append(node)
        if self._depth:
            self.nested_plain_imports.add(node)

    def visit_ImportFrom(self, node: cst.ImportFrom) -> None:
        self.imports.append(node)
        if self._depth:
            self.nested_import_froms.add(node)

    def visit_Attribute(self, node: cst.Attribute) -> None:
        # Descends into import statements too, exactly as the separate walk
        # this replaces did: ``import a.b`` is itself an ``a.b`` read.
        try:
            prefix = _imports.dotted(node.value)
        except TypeError:
            return  # a call, a subscript, ... -- not a dotted module path
        self.attribute_reads.add((prefix, node.attr.value))


def collect_facts(tree: cst.Module) -> FileFacts:
    """Walk *tree* once and return every `FileFacts` the analysis needs."""
    collector = _FactCollector()
    tree.visit(collector)
    return FileFacts(
        tuple(collector.imports),
        frozenset(collector.attribute_reads),
        tuple(collector.path_strings),
        frozenset(collector.nested_import_froms),
        frozenset(collector.nested_plain_imports),
    )


def iter_units(tree: cst.Module, base_pkg: str) -> Iterator[ImportUnit]:
    """Yield an :class:`ImportUnit` per name in every ``from`` import."""
    return collect_facts(tree).units(base_pkg)


def absolute_import_heads(tree: cst.Module) -> set[str]:
    """Top-level names *tree* imports absolutely (``import a.b`` -> ``a``).

    Evidence for `ModuleMap.demote_roots`: whatever a file imports by an
    absolute name lives under an import root, so it is not a root itself.
    """
    return collect_facts(tree).absolute_import_heads()


def max_relative_level(tree: cst.Module) -> int:
    """Deepest ``from ... import`` dot count in *tree* (0 if none are relative)."""
    return collect_facts(tree).max_relative_level()


def collect_pairs(records: list[FileRecord]) -> list[tuple[str, str]]:
    """``(module, name)`` pairs to classify: every name imported *from* a module."""
    pairs: set[tuple[str, str]] = set()
    for rec in records:
        for unit in rec.units:
            if unit.parent and not unit.star:
                pairs.add((unit.parent, unit.name))
    return sorted(pairs)


def replacement_pairs(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """``(package, token)`` for every ``package.token`` some import is *from*.

    These are the pairs `resolver.Resolver.replacement_unreachable` asks
    about -- the import the fixer would write, rather than the one the file
    already has -- and they are classified in the same batch so a run still
    costs one probe round-trip rather than one per import.

    Deliberately not folded into `collect_pairs`: its result is also the seed
    for the *use* evidence `resolver.Resolver.is_load_bearing` weighs, and
    ``package.token`` being imported *through* is not a use of that name.
    """
    out: set[tuple[str, str]] = set()
    for parent, _name in pairs:
        package, _, token = parent.rpartition(".")
        if package:
            out.add((package, token))
    return sorted(out)


def module_bindings(tree: cst.Module, base_pkg: str) -> dict[str, str]:
    """Local name -> dotted module it is bound to, for every import in *tree*.

    ``import a.b`` binds ``a``; ``import a.b as ab`` binds ``ab`` to ``a.b``;
    ``from p import m`` binds ``m`` to ``p.m``. Whether ``p.m`` is really a
    module is not checked here -- recording it regardless only ever makes
    `attribute_pairs` see *more* uses, and this evidence is used to decline
    rewrites, so over-collecting is the safe direction.
    """
    return collect_facts(tree).module_bindings(base_pkg)


def attribute_pairs(tree: cst.Module, base_pkg: str) -> set[tuple[str, str]]:
    """``(module, attribute)`` pairs *tree* reads through a module binding.

    ``import pkg.tool`` followed by ``pkg.tool.dump`` is a use of
    ``pkg.tool.dump`` every bit as much as ``from pkg.tool import dump`` is,
    and it is the shape this tool rewrites everything *into* -- so a fixer
    that only looked at ``from`` imports for evidence was blind to its own
    output, and a second ``--fix`` run would happily delete the attribute the
    first run had just protected.

    Bindings are the whole file's, wherever they sit relative to the read
    (`module_bindings`).
    """
    return collect_facts(tree).attribute_pairs(base_pkg)


def star_imported_modules(tree: cst.Module, base_pkg: str) -> set[str]:
    """Modules *tree* does ``from M import *`` on.

    A star import takes every public name, so any of *M*'s re-exports could be
    the one it needs. There is no way to narrow it, so all of them count.
    """
    return collect_facts(tree).star_imported_modules(base_pkg)


#: Why a wildcard import is reported and never rewritten.
_WILDCARD = "wildcard import cannot be rewritten to a module import"
#: Why a relative import that climbs out of every known package is `CP002`.
_UNANCHORED = "relative import could not be anchored to a package"
_REEXPORT = (
    "explicit re-export ('as' aliasing the name to itself); rewriting it would remove a public name"
)
_UNREAD = (
    "the imported name is never read in this file, so rewriting the import "
    "would only remove the binding -- and something outside this file "
    "(a pytest fixture, an entry point) may be reading it"
)


@dataclasses.dataclass(frozen=True)
class Decision:
    """What cleanporter does with one imported name, in every mode.

    ``status`` is the finding to report, or ``None`` for a name nothing is
    reported about -- exempt, outside the configured `scope`, a module, or part
    of a package ``__init__``'s public surface (`Decider.public_surface`). The
    fixer rewrites only names checking reports as `CP001` (never-read names
    become `CP003` under ``--fix``); a guard hit still declines the whole
    file. So a name ``check`` reports as anything else -- or does not report
    at all -- is never touched by ``--fix``.
    """

    status: model.Status | None
    detail: str = ""
    #: Kept only because nothing in the file reads it. The fixer collects
    #: these to hand back to `analyze_record`; see `rewrite.FixOutcome.unread`.
    unread: bool = False
    #: For a `CP004` an inline comment made: the code it replaced and the
    #: comments naming it. `analyze_record` reports the rest as `CP005`.
    hit: suppress_lib.Hit | None = None

    @property
    def rewrite(self) -> bool:
        """True when this is a `CP001` the fixer should rewrite."""
        return self.status is model.Status.VIOLATION


#: Nothing to report and nothing to rewrite.
COMPLIANT = Decision(None)
#: A `CP001`: reported by ``check``, rewritten by ``--fix``.
VIOLATION = Decision(model.Status.VIOLATION)


def _overridden(rule: skip_lib.Rule | None, decision: Decision) -> Decision:
    """*decision*, or the `CP004` that a matching *rule* replaces it with."""
    if rule is None:
        return decision
    return Decision(model.Status.SKIPPED_BY_CONFIG, rule.describe())


def in_scope(parent: str, resolver: resolver_lib.Resolver, config: config.Config) -> bool:
    """Whether the configured ``scope`` covers imports from *parent*.

    The one statement of the rule. Under ``scope = "first-party"`` an import
    from outside the analysis roots is not reported, so not rewritten
    (`Decider.decide`), and not classified either: `project.build` leaves it
    out of the probe batch and the fixer does not look it up for a binding to
    reuse (`rewrite._Fixer._build_existing`). Only the top-level component is
    tested (`resolver.Resolver.is_first_party`).
    """
    return config.scope != "first-party" or resolver.is_first_party(parent)


class Decider:
    """The one per-import decision ladder, shared by ``check`` and ``--fix``.

    `analyze_record` turns each `Decision` into a finding and
    `rewrite._Fixer` turns it into a keep-or-rewrite choice. They used to be
    two hand-synchronised copies of this chain, and the copies drifted: the
    fixer never learned about ``scope = "first-party"``, so ``check`` passed
    over ``from os.path import join`` while ``--fix`` rewrote it. One function
    answering both questions is what keeps "the fixer only rewrites what check
    calls `CP001`" true by construction rather than by review.

    It lives here rather than in a module of its own because it is phrased in
    `ImportUnit` and `FileRecord`, which this module owns, and `rewrite`
    already depends on this module; a separate one would have to import them
    back from here.
    """

    def __init__(
        self, rec: FileRecord, resolver: resolver_lib.Resolver, config: config.Config
    ) -> None:
        self._rec = rec
        self._resolver = resolver
        self._config = config

    def in_scope(self, parent: str) -> bool:
        """Whether imports from *parent* are this run's business at all (`in_scope`)."""
        return in_scope(parent, self._resolver, self._config)

    def decide(
        self, unit: ImportUnit, line: int, never_read: frozenset[str] = frozenset()
    ) -> Decision:
        """`_ladder`'s decision, unless an inline comment suppresses its code.

        A suppressed finding becomes a `CP004`, so the fixer keeps the name
        exactly as it keeps one a skip rule covers. The code matched is the one
        plain ``check`` reports: a never-read name is the fixer's `CP003` but
        check's `CP001`, so ``ignore[CP001]`` covers it in both modes and the
        two cannot disagree about what is suppressed. See `cleanporter.suppress`.
        """
        decision = self._ladder(unit, line, never_read)
        status = model.Status.VIOLATION if decision.unread else decision.status
        code = suppress_lib.CODES.get(status) if status is not None else None
        if code is None:
            return decision
        hit = self._rec.suppressions.match(unit.node, unit.alias, code)
        if hit is None:
            return decision
        where = ", ".join(str(s.line) for s in hit.by)
        return Decision(
            model.Status.SKIPPED_BY_CONFIG,
            f"{code} suppressed by the inline comment on line {where}",
            hit=hit,
        )

    def _ladder(
        self, unit: ImportUnit, line: int, never_read: frozenset[str] = frozenset()
    ) -> Decision:
        """Decide *unit*, an import found on *line* of the record's file.

        *never_read* holds the names this import binds that nothing in the file
        reads -- see `rewrite._Fixer._unread_names`. Only the fixer can work
        that out, so a plain ``check`` passes nothing.

        The order of the chain is the reported order. A skip rule *replaces*
        a finding; it does not add one. So it is resolved up front but applied
        only where a finding is actually produced, never before the filters
        that would report nothing at all -- `CP004` for a compliant ``from pkg
        import module``, or for an exempt ``typing`` import, would pad the one
        count the docs offer as the way to see how much a rule swallowed; a
        package ``__init__``'s public surface (`public_surface`) is one of
        those filters, for the same reason. The
        line-wide reasons (a skip covering the line, a replacement with no
        relative spelling, an unreachable replacement) precede the per-name
        ones, and never-read comes *last*,
        because reaching it means every other reason to keep the name was
        already false.
        """
        skipped = self._rec.skipped
        rule = skipped.covers(line) or (
            None if unit.star else skipped.pin(unit.asname or unit.name)
        )
        if unit.star:
            return _overridden(rule, Decision(model.Status.SKIPPED, _WILDCARD))
        parent = unit.parent
        if parent is None:
            return _overridden(rule, Decision(model.Status.UNRESOLVED, _UNANCHORED))
        if self._config.is_exempt(parent, unit.name) or not self.in_scope(parent):
            return COMPLIANT
        verdict = self._resolver.is_module(parent, unit.name)
        if verdict is True:
            return COMPLIANT  # importing a module -> compliant
        if verdict is False and self.public_surface(unit.node):
            # Compliant, not a `CP003`: the re-export is the sanctioned
            # exception to the rule, not a violation declined, so it has no
            # reason to give and nothing to count against the exit code. So
            # it is decided here rather than in `_declined`, which only says
            # why a *violation* is kept, and it outranks every decline there
            # (re-export, load-bearing, named by a string, never read): each
            # of those is true of some of these imports, and none is the
            # reason they stay. Before the skip rule, as a compliant module
            # is, so a rule over it adds no `CP004`. Only for a proven object:
            # a module is compliant already, and an unresolved name stays the
            # `CP002` it is -- whether it is even an import of an object is
            # what the resolver could not say.
            return COMPLIANT
        if rule is not None:
            return Decision(model.Status.SKIPPED_BY_CONFIG, rule.describe())
        if verdict is None:
            return Decision(model.Status.UNRESOLVED, self._resolver.reason(parent, unit.name))
        return self._declined(unit, parent, never_read) or VIOLATION

    def public_surface(self, node: cst.ImportFrom) -> bool:
        """Whether *node* is part of a package's public surface: never reported or rewritten.

        True for a module-level ``from P import S`` in a package's
        ``__init__.py`` -- module level meaning outside every ``def`` and
        ``class``, so one under a module-level ``if``, ``try`` or ``with``
        counts. Whatever such an import binds is an attribute of the package,
        and a package's attributes are what its users import: ``from
        ._version_info import VersionInfo`` in attrs' ``attr/__init__.py`` is
        what makes ``attr.VersionInfo`` exist. Rewriting it deletes public API
        that nothing in the run need read, name in ``__all__`` or mention in a
        string, so no evidence-based guard can see it. Style guide §2.2 is
        commonly relaxed for exactly these re-exports, so the rule is
        structural, not evidential: the import is the sanctioned exception,
        not a declined violation.

        A ``__main__.py`` is not a package's surface, a namespace package has
        no ``__init__.py`` to hold one, and a stub (``__init__.pyi``) is not
        analysed; an import inside a function in ``__init__.py`` binds a local
        (in a class body, a class attribute) and is decided like any other --
        one a ``global`` statement makes a package attribute again is declined
        by the fixer's ``global``/``nonlocal`` guard.
        """
        return (
            self._rec.path.name == "__init__.py" and node not in self._rec.facts.nested_import_froms
        )

    def own_init(self, parent: str) -> bool:
        """Whether the record's file is *parent*'s own ``__init__.py``."""
        return self._rec.path.name == "__init__.py" and self._rec.qualname == parent

    def _declined(
        self, unit: ImportUnit, parent: str, never_read: frozenset[str]
    ) -> Decision | None:
        """The `CP003` for a proven object the fixer must still leave alone.

        The first two reasons are line-wide: they are about the module import
        that would replace the line, which every name on it shares. Either
        there is no spelling for it that does not lean on the inferred import
        root (`_imports.module_import_spelling`), or the one there is cannot
        be shown to bind the module.
        """
        spelling = _imports.module_import_spelling(
            unit.node, parent, declared_root=self._rec.declared_root
        )
        if spelling is None:
            return Decision(
                model.Status.SKIPPED,
                _imports.unspellable_reason(
                    unit.node, parent, root_hint=self._rec.root_hint, own_init=self.own_init(parent)
                ),
            )
        unreachable = self._resolver.replacement_unreachable(
            parent, _imports.render_import(spelling)
        )
        if unreachable is not None:
            return Decision(model.Status.SKIPPED, unreachable)
        if _imports.is_explicit_reexport(unit.name, unit.asname):
            return Decision(model.Status.SKIPPED, _REEXPORT)
        bound = unit.asname or unit.name
        qualname = self._rec.qualname
        if qualname and self._resolver.is_load_bearing(qualname, bound):
            return Decision(
                model.Status.SKIPPED,
                f"another file imports '{bound}' from '{qualname}'; "
                "rewriting this import would remove that attribute",
            )
        named = (
            self._resolver.named_by(qualname, bound, outside=self._rec.path) if qualname else None
        )
        if named is not None:
            return Decision(
                model.Status.SKIPPED,
                f"'{qualname}.{bound}' is named by the string '{named.text}' at "
                f"{named.where()}; rewriting this import would remove that attribute",
            )
        if bound in never_read:
            return Decision(model.Status.SKIPPED, _UNREAD, unread=True)
        return None


def analyze_record(
    rec: FileRecord,
    resolver: resolver_lib.Resolver,
    config: config.Config,
    unread: frozenset[str] = frozenset(),
) -> list[model.Finding]:
    """Findings for one file.

    *unread* is the fixer's answer to "which of this file's imported names are
    never read here" -- see `rewrite._Fixer._unread_names`. Answering it needs
    scope metadata, which only the fix path resolves, so a plain ``check`` run
    passes nothing and reports those imports as the `CP001` they are. Under
    ``--fix`` the fixer has already paid for the answer, and passing it here is
    what turns "cleanporter did not fix this and did not say why" into a
    `CP003` that does.

    Every decision is `Decider.decide`'s, the same call the fixer makes.
    """
    starts = rec.import_starts
    decider = Decider(rec, resolver, config)
    findings: list[model.Finding] = []
    used: set[tuple[suppress_lib.Suppression, str]] = set()
    #: Comments covering a name reported as never read, for `CP005`'s hint.
    on_unread: set[suppress_lib.Suppression] = set()
    for unit in rec.units:
        line, column = starts[unit.node]
        decision = decider.decide(unit, line, unread)
        _mark_used(rec, unit.node, unit.alias, decision, used)
        if decision.unread:
            on_unread.update(rec.suppressions.covering(unit.node, unit.alias))
        if decision.status is None:
            continue
        findings.append(
            model.Finding(
                rec.path,
                line,
                column,
                unit.parent or "?",
                unit.name,
                decision.status,
                decision.detail,
                _module_import(rec, unit) if decision.rewrite else "",
            )
        )
    findings.extend(_alias_findings(rec, resolver, config, used))
    findings.extend(_unused_suppressions(rec, used, on_unread))
    return findings


def _mark_used(
    rec: FileRecord,
    node: cst.Import | cst.ImportFrom,
    alias: cst.ImportAlias | None,
    decision: Decision,
    used: set[tuple[suppress_lib.Suppression, str]],
) -> None:
    """Record the suppression codes *decision* consumed, into *used*.

    A `CP004` a skip rule made replaced the finding before any comment was
    consulted, so what the comments covering the name would have matched is
    unknowable: every code they name counts as used, rather than be reported
    unused over a finding nobody could see.
    """
    if decision.hit is not None:
        used.update((s, decision.hit.code) for s in decision.hit.by)
    elif decision.status is model.Status.SKIPPED_BY_CONFIG:
        for suppression in rec.suppressions.covering(node, alias):
            used.update((suppression, code) for code in suppression.codes)


@dataclasses.dataclass(frozen=True)
class ModuleBinding:
    """One name an import statement binds to a module: what the alias check weighs."""

    node: cst.Import | cst.ImportFrom
    alias: cst.ImportAlias
    #: The absolute dotted module bound.
    module: str
    #: The name it is bound under -- for a `chain`, the dotted chain itself.
    bound: str
    #: ``import a.b.c`` with no ``as``: nothing is bound to ``a.b.c`` but the
    #: attribute chain ``a.b.c`` reached through ``a`` (bound separately).
    chain: bool = False


def bound_modules(
    rec: FileRecord, resolver: resolver_lib.Resolver, config: config.Config
) -> Iterator[ModuleBinding]:
    """Every binding of a *proven* module in *rec* that an alias convention can judge.

    ``import M as N`` binds ``N`` to ``M``, and ``import M`` binds ``M``'s
    top-level package under its own name -- and, when ``M`` is dotted, the
    `ModuleBinding.chain` ``M`` through it. The syntax proves each of those
    names a module. ``from P import L [as N]`` binds a module only when the
    resolver proves ``P.L`` is one (`resolver.Resolver.is_module`); a name
    it calls an object or cannot classify is `CP001`'s or `CP002`'s
    business, never a guessed `CP006`. A relative import is judged by the
    absolute name it resolves to.

    Left out, as `Decider` leaves them out: a package ``__init__``'s
    module-level imports, its public surface (`Decider.public_surface`); a
    module outside the configured ``scope`` (`in_scope`), which is not
    classified at all; ``__future__`` and wildcard imports, which bind no
    module.
    """
    public = rec.path.name == "__init__.py"
    for node in rec.facts.imports:
        if isinstance(node, cst.Import):
            if not public or node in rec.facts.nested_plain_imports:
                yield from _plain_bindings(node, resolver, config)
        elif not public or node in rec.facts.nested_import_froms:
            yield from _from_bindings(node, rec.base_pkg, resolver, config)


def _plain_bindings(
    node: cst.Import, resolver: resolver_lib.Resolver, config: config.Config
) -> Iterator[ModuleBinding]:
    for alias in node.names:
        dotted = _imports.dotted(alias.name)
        if not in_scope(dotted, resolver, config):
            continue
        as_node = alias.asname.name if alias.asname is not None else None
        if isinstance(as_node, cst.Name):
            yield ModuleBinding(node, alias, dotted, as_node.value)
            continue
        head = dotted.partition(".")[0]
        yield ModuleBinding(node, alias, head, head)
        if head != dotted:
            yield ModuleBinding(node, alias, dotted, dotted, chain=True)


def _from_bindings(
    node: cst.ImportFrom, base_pkg: str, resolver: resolver_lib.Resolver, config: config.Config
) -> Iterator[ModuleBinding]:
    if _imports.is_star(node):
        return
    parent = _imports.resolve_parent(node, base_pkg)
    if parent is None or parent == "__future__" or not in_scope(parent, resolver, config):
        return
    for name, asname, alias in _imports.imported_names(node):
        if resolver.is_module(parent, name) is True:
            yield ModuleBinding(node, alias, f"{parent}.{name}", asname or name)


def _alias_findings(
    rec: FileRecord,
    resolver: resolver_lib.Resolver,
    config: config.Config,
    used: set[tuple[suppress_lib.Suppression, str]],
) -> list[model.Finding]:
    """A `CP006` per binding that breaks its alias convention (`cleanporter.aliases`).

    Replaced by a `CP004` exactly as `Decider` replaces a `CP001`: when a
    skip rule covers the line or pins the bound name, or an inline comment
    names ``CP006``. Nothing is walked for a run with no alias rule.
    """
    conventions = config.conventions
    if not conventions:
        return []
    path = skip_lib.file_candidates(rec.path, rec.root)[0]
    findings: list[model.Finding] = []
    for binding in bound_modules(rec, resolver, config):
        expectation = conventions.expected(binding.module, rec.qualname, path)
        detail = _alias_mismatch(binding, expectation) if expectation is not None else None
        if detail is None:
            continue
        if isinstance(binding.node, cst.Import):
            line, column = rec.plain_import_starts[binding.node]
        else:
            line, column = rec.import_starts[binding.node]
        decision = _alias_decision(rec, binding, line, detail)
        _mark_used(rec, binding.node, binding.alias, decision, used)
        if decision.status is not None:
            findings.append(
                model.Finding(
                    rec.path,
                    line,
                    column,
                    binding.module,
                    binding.bound,
                    decision.status,
                    decision.detail,
                )
            )
    return findings


def _alias_mismatch(binding: ModuleBinding, expectation: aliases.Expectation) -> str | None:
    """Why *binding* breaks *expectation*, or ``None`` when it keeps it."""
    if not expectation.usable:
        template = expectation.rule.alias
        return (
            f"{expectation.rule.describe()} renders its template {template!r} as "
            f"'{expectation.binding}' for this module, which no import can bind; "
            "no binding can satisfy it, so fix the rule"
        )
    wanted = (
        f"its own name '{expectation.binding}'"
        if expectation.own_name
        else f"'{expectation.binding}'"
    )
    by = f"expected {wanted} by {expectation.rule.describe()}"
    if binding.chain:
        # ``import a.b.c`` is the own-name spelling of a submodule, and no
        # other: an identifier convention asks for an ``as``.
        if expectation.own_name:
            return None
        head = binding.module.partition(".")[0]
        return f"`import {binding.module}` binds it only as a chain through '{head}'; {by}"
    if binding.bound == expectation.binding:
        return None
    return by


def _alias_decision(rec: FileRecord, binding: ModuleBinding, line: int, detail: str) -> Decision:
    """The `CP006` for *binding*, or the `CP004` a skip rule or a comment makes of it."""
    rule = rec.skipped.covers(line) or rec.skipped.pin(binding.bound.partition(".")[0])
    if rule is not None:
        return Decision(model.Status.SKIPPED_BY_CONFIG, rule.describe())
    code = suppress_lib.CODES[model.Status.ALIAS_MISMATCH]
    hit = rec.suppressions.match(binding.node, binding.alias, code)
    if hit is not None:
        where = ", ".join(str(s.line) for s in hit.by)
        return Decision(
            model.Status.SKIPPED_BY_CONFIG,
            f"{code} suppressed by the inline comment on line {where}",
            hit=hit,
        )
    return Decision(model.Status.ALIAS_MISMATCH, detail)


def _unused_suppressions(
    rec: FileRecord,
    used: set[tuple[suppress_lib.Suppression, str]],
    on_unread: set[suppress_lib.Suppression],
) -> list[model.Finding]:
    """A `CP005` per suppression comment naming a code that matched nothing.

    A comment on a line a skip rule covers is not reported: the rule took
    whatever it could have matched.

    A never-read name is suppressed by ``CP001`` only (`Decider.decide`), so
    an ``ignore[CP003]`` written after seeing the fixer's never-read `CP003`
    is unused -- in both modes, which is what keeps them agreeing. The
    message says so where the fixer knows the name is never read (*on_unread*).
    """
    findings: list[model.Finding] = []
    for suppression in rec.suppressions.comments:
        if rec.skipped.covers(suppression.line) is not None:
            continue
        unused = [code for code in suppression.codes if (suppression, code) not in used]
        if not unused:
            continue
        findings.append(
            model.Finding(
                rec.path,
                suppression.line,
                suppression.column,
                "",
                "",
                model.Status.UNUSED_SUPPRESSION,
                f"no {' or '.join(unused)} finding on the imports this comment covers"
                + (
                    " (a never-read import is suppressed by CP001, the code a plain check "
                    "reports for it, even where --fix reports it as CP003)"
                    if "CP003" in unused and suppression in on_unread
                    else ""
                )
                + "; "
                + (
                    "remove the comment"
                    if len(unused) == len(suppression.codes)
                    else f"remove {', '.join(unused)} from it"
                ),
            )
        )
    return findings


def _module_import(rec: FileRecord, unit: ImportUnit) -> str:
    """The replacement a `CP001`'s message advises, when it is not the plain absolute one.

    Spelled by the function the fixer spells its new statement with
    (`_imports.module_import_spelling`, then `_imports.render_import`), so
    the advice for ``from .readers import read`` is ``from . import
    readers`` -- the statement ``--fix`` would write, give or take the alias
    it may allocate -- and never the absolute name the inferred root
    implies. An absolute import's advice is its own parent, already in the
    message, so it gets none and its text is unchanged.
    """
    if unit.parent is None or _imports.relative_level(unit.node) == 0:
        return ""
    spelling = _imports.module_import_spelling(
        unit.node, unit.parent, declared_root=rec.declared_root
    )
    return _imports.render_import(spelling) if spelling is not None else ""
