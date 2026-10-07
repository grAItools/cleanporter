# Copyright (c) 2026 grAItools
# SPDX-License-Identifier: BSD-3-Clause
# See LICENSE for the full license text.

"""Which imports sit behind ``if TYPE_CHECKING:`` and so do not exist at run time.

The fixer needs this twice over. An ``ImportFrom`` inside such a block is only
rewritten when ``from __future__ import annotations`` makes every annotation a
string at run time; and *no* import inside one -- ``ImportFrom`` or plain
``Import`` -- may serve as the runtime binding a rewritten reference is
qualified through, because the block never runs.

"Inside such a block" is decided conservatively: any test that so much as
mentions ``TYPE_CHECKING`` (or a local alias of it) counts, since its body is not
guaranteed to run; the one exception is the exact shape ``if not
TYPE_CHECKING:``, whose body always runs and whose ``else`` never does. See
`_type_checking_only` for the cases that forced each half of that.
"""

from __future__ import annotations

import libcst as cst

from . import _imports, _nodes


def import_ids(tree: cst.Module) -> set[int]:
    """Ids of import nodes located inside an ``if TYPE_CHECKING:`` block.

    Both ``ImportFrom`` *and* plain ``Import``. The plain ones matter to
    `rewrite._Fixer._build_existing`, which harvests already-imported modules
    to bind rewritten references through: a ``TYPE_CHECKING`` block is
    `GlobalScope` to libcst exactly like the module body, so ``import
    unittest`` in one looked like a perfectly good runtime binding. Reusing
    it emitted no new import at all and rewrote ``TestCase`` to
    ``unittest.TestCase``, which raises ``NameError`` the moment it runs.
    Found by running ``_pytest``'s own test suite against a rewritten copy.
    """
    ids: set[int] = set()
    aliases = _type_checking_aliases(tree)

    class V(cst.CSTVisitor):
        def visit_If(self, node: cst.If) -> None:
            # For `if TYPE_CHECKING:` the body is the type-checking-only half;
            # for `if not TYPE_CHECKING:` it is the `else`. Anything else is
            # ordinary runtime code.
            branch: cst.CSTNode | None = None
            if _type_checking_only(node.test, aliases):
                branch = node.body
            elif _is_negated_type_checking(node.test, aliases) and node.orelse is not None:
                branch = node.orelse
            if branch is None:
                return
            for imp in _nodes.import_froms(branch):
                ids.add(id(imp))
            for plain in _nodes.plain_imports(branch):
                ids.add(id(plain))

    tree.visit(V())
    return ids


def body_always_runs(node: cst.If, tree: cst.Module) -> bool:
    """Whether *node*'s body runs whenever *node* does: ``if not TYPE_CHECKING:``.

    The one guard `import_ids` treats as ordinary runtime code; any other
    ``if`` may or may not run its body.
    """
    return _is_negated_type_checking(node.test, _type_checking_aliases(tree))


def _type_checking_aliases(tree: cst.Module) -> set[str]:
    """Local names bound to ``typing.TYPE_CHECKING``, including its own.

    ``from typing import TYPE_CHECKING as TC`` then ``if TC:`` is the same
    guard spelled differently, and matching the identifier alone missed it --
    the block's imports were harvested as runtime bindings.
    """
    names = {"TYPE_CHECKING"}

    class V(cst.CSTVisitor):
        def visit_ImportFrom(self, node: cst.ImportFrom) -> None:
            for name, asname, _alias in _imports.imported_names(node):
                if name == "TYPE_CHECKING" and asname:
                    names.add(asname)

    tree.visit(V())
    return names


def _is_type_checking_name(node: cst.BaseExpression, aliases: set[str]) -> bool:
    """One of *aliases*, or ``<anything>.TYPE_CHECKING``."""
    if isinstance(node, cst.Name):
        return node.value in aliases
    return isinstance(node, cst.Attribute) and node.attr.value == "TYPE_CHECKING"


def _is_negated_type_checking(test: cst.BaseExpression, aliases: set[str]) -> bool:
    """Exactly ``not TYPE_CHECKING`` -- the one guard that always runs.

    Only this precise shape. ``not (TYPE_CHECKING or X)`` depends on ``X``,
    and ``not DEBUG`` has nothing to do with type checking at all -- reading
    every ``not`` as this idiom declined perfectly ordinary files with a
    reason that was false about them.
    """
    return (
        isinstance(test, cst.UnaryOperation)
        and isinstance(test.operator, cst.Not)
        and _is_type_checking_name(test.expression, aliases)
    )


def _type_checking_only(test: cst.BaseExpression, aliases: set[str]) -> bool:
    """True when a block guarded by *test* may not run at run time.

    Matching only a bare ``if TYPE_CHECKING:`` was not enough. A test that
    merely *mentions* ``TYPE_CHECKING`` is equally not guaranteed --
    ``if TYPE_CHECKING or not install_lazy_importer():`` and
    ``if sys.version_info >= (3, 11) or TYPE_CHECKING:`` are both real, and
    both left an import that a rewrite then leaned on as though it always
    existed, producing ``NameError``.

    The one shape that *is* guaranteed is ``if not TYPE_CHECKING:``: at run
    time ``TYPE_CHECKING`` is false, so the body always executes and its
    imports are ordinary runtime bindings. Only that exact shape is excluded
    -- ``if not (TYPE_CHECKING or X):`` is a different expression whose value
    depends on ``X``, so it stays conservative.
    """
    if _is_negated_type_checking(test, aliases):
        return False
    mentioned = False

    class V(cst.CSTVisitor):
        def visit_Name(self, node: cst.Name) -> None:
            nonlocal mentioned
            if node.value in aliases:
                mentioned = True

    test.visit(V())
    return mentioned
