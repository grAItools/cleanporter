# Safety and limitations

The fixer's job is to change code without changing behaviour. Everything on
this page follows from one rule: when a rewrite cannot be *proven* safe, the
file is left exactly as its author wrote it and the reason is reported.

## All-or-nothing, per file

A file is rewritten completely or not at all. Before anything is written, the
fixer plans every rename in the file and runs every guard over it; if a single
guard fires anywhere in that file, the whole plan is discarded and the
original source is returned untouched. There is no partial rewrite, and no
state in which some references were qualified and others were not.

Each declined file is explained by one or more `CP003` findings pointing at
the line responsible.

## What the rewrite is built on

Renames come from libCST's scope analysis, not from text matching. That means
a rename applies only to accesses whose referents resolve *uniquely* to the
import binding being rewritten. Concretely:

- a local variable in some function that happens to share the name is left
  alone;
- `as` aliases are followed — `from a.b import C as D` rewrites uses of `D`;
- a dotted access `obj.C` is not a reference to the imported `C` and is not
  touched;
- imports inside function and class bodies are fixed too, each scope getting
  its own binding, tracked independently of any module-level import of the
  same module.

Formatting survives because libCST is a *concrete* syntax tree: it round-trips
the source, so what the fixer does not deliberately change is reproduced
byte-for-byte. The structural changes are the inserted or replaced import
statement and the qualified references. The original import line's leading
blank lines and comments are carried onto the first replacement line, and its
trailing comment onto the last.

Comments are never *altered*, and they are **never silently dropped**: in the
cases where a rewrite could not carry a comment across, the file is declined
instead. Those cases are listed below.

## When a file is skipped

Any one of these blocks the entire file. Each emits a `CP003` finding naming
the line.

### A mention of the local name inside a string literal

`__all__ = ["Widget"]`, `getattr(mod, "Widget")`, a `pytest.mark.parametrize`
id — these keep working only if the spelling of the name survives, and a
rename would silently break them. So a bare name match inside a string
literal blocks the file.

Two exceptions:

- **Genuine prose docstrings are exempt.** A docstring is identified
  structurally — the value of an expression statement that is the first
  statement of a module, class or function body — never guessed at from
  position or content. A string anywhere else (a pseudo attribute-docstring
  after an assignment, a string inside a list, `__all__`) is not a docstring
  and keeps blocking. The reasoning: a docstring naming an imported symbol is
  extremely common, it is *describing* the import rather than depending on its
  exact spelling, and a stale name in prose is a documentation nit rather than
  broken code. Treating it as unfixable would block a rewrite on a large
  fraction of real files.

    A **doctest** is different. `>>> Thing()` is executable and a rename
    genuinely breaks it, so a docstring containing `>>>` anywhere is not
    exempt and blocks like any other string.

- **Lazy string annotations are rewritten, not blocked.** Under
  `from __future__ import annotations` an annotation is never evaluated at
  runtime, so a string sitting in a genuine annotation slot is rewritten
  *along with* the code rather than treated as a mention.

    This is done by parsing the string's contents as an expression and walking
    it structurally, not by substituting text. `Literal[...]` arguments are
    values rather than type references, so that whole slice is skipped;
    `Annotated[T, ...]` mixes a real type with arbitrary metadata, so only the
    first element is descended into. If a candidate string cannot be parsed,
    or the rewritten result cannot be re-wrapped in its original quoting and
    round-tripped back exactly, it is not guessed at — it is left alone, and
    the ordinary string-mention guard then judges it. The same check catches
    content that carries trivia re-rendering would drop, such as a trailing
    comment inside the annotation string.

!!! note "f-strings"

    An f-string is **not** a blocking mention. Its interpolations are code, so
    `f"{Thing}"` is rewritten to `f"{mod.Thing}"` like any other reference. A
    plain string *nested inside* one — as in `f"{getattr(m, 'Thing')}"` — is
    still an ordinary string literal, and still blocks.

### The local name is rebound in the same scope

If the name bound by the import is also assigned somewhere else in the same
scope, libCST's scopes are not flow-sensitive: an access lists both the import
and the assignment as its referents, so there is no safe subset to rewrite.
The file is declined.

### `global` / `nonlocal` declarations naming it

Such a declaration keeps the name writable from another scope. Qualifying the
reads without also rewriting the writes would silently decouple them.

### A `del` of the local name

libCST records `del x` as a *read* of `x`, so it sails past the rebinding
check and would be rewritten to `del mod.x` — which does not unbind a local at
all. It deletes the attribute from the imported module in `sys.modules`,
breaking every *other* importer of that module. `del name` right after an
import is a real `__init__.py` cleanup idiom, so this is not theoretical.

### A `match` capture pattern binding the local name

`case Name:` is a capture pattern: it always matches and *binds* `Name`.
Rewriting it to `case mod.Name:` turns it into a value pattern, which matches
only when the subject equals `mod.Name` — a silent change of control flow.
libCST reports the captured name as an access of the import, so nothing else
in the fixer can tell the two apart. The `case [*rest]` and `case {**rest}`
binding forms are covered by the same rule.

`case Thing():` (a class pattern) and `case mod.Thing:` (already a value
pattern) are genuine references and are rewritten normally.

### An import under `if TYPE_CHECKING:` without future annotations

Moving such an import changes what name is bound, and with eagerly evaluated
annotations that raises `NameError` at runtime. So a `TYPE_CHECKING`-gated
import is only rewritten when the file has `from __future__ import
annotations` active — and then both the import and any lazy string annotation
mentioning the name are rewritten together.

### A comment inside the import statement

The kept-names line is regenerated from text, which cannot carry a statement's
interior trivia across. A comment inside a parenthesized multi-line import —
including a per-name `# noqa:` or `# type: ignore` — would therefore vanish.
Rather than discard it, the file is declined.

### Removing the import line would discard its comment

When the module is already bound elsewhere and nothing on the line needs to be
kept, the import line disappears entirely, leaving nowhere to put a leading or
trailing comment attached to it. Discarding an author's comment silently is
worse than declining the fix, so the file is left alone. A *blank* line before
the import is not a comment and does not block.

### The rewrite did not re-parse

A final backstop rather than a guard: after rewriting, the result must parse.
If it does not, the original content is kept and an internal-error `CP003` is
reported. cleanporter never hands back source it cannot compile.

## Known limitations

- **Relative imports become absolute.** `from .sub.mod import C` is rewritten
  to `from pkg.sub import mod` plus `mod.C`. The import that is *kept* (any
  compliant names in a mixed statement) keeps its original relative form; the
  new module import is always absolute.
- **Imports are not re-sorted.** The fixer inserts or replaces a statement in
  place rather than reflowing the import block. Use isort or Ruff separately
  for layout.
- **Wildcard imports are never rewritten.** `from x import *` is reported as
  `CP003` — there is no module import that reproduces it — in every mode,
  including plain check mode.
- **A file is blocked outright, not partially fixed**, whenever a rewritten
  name appears in a non-docstring string literal, inside a doctest, or when
  removing an import would discard its comment. See the section above.
- **Type comments are not inspected.** A `# type: ...` comment naming a
  rewritten symbol is neither rewritten nor treated as a blocker. (A comment
  *inside* an import statement is a separate matter, and does block.)
- **Guards are per file.** A string in *another* file that names the rewritten
  binding by its dotted path — `monkeypatch.setattr("pkg.cli.helper", ...)`,
  an entry point, an `importlib` lookup — cannot be seen, so `--fix` can make
  such a reference stale even though the rewritten file itself is correct.
  This was found by running cleanporter over its own source: one test patched
  `cleanporter.cli.fix_record`, a name the compliant rewrite no longer binds
  there. Re-run your test suite after a `--fix` sweep; `--fix` prints a note
  to stderr saying so whenever it writes a file.
- **One-liner suites and semicolon-joined imports get no `CP003`.** They are
  reported as `CP001` and not rewritten, but no note explains that the fixer
  declined them. The fixer never plans such a line at all, and turning that
  into a blocker would make the *whole file* unfixable — strictly worse, since
  it would also stop the unrelated, provably safe rewrites in it. The
  violation is still reported; only the "declined, because…" note is missing.
- **All diffs go to one stream.** Every changed file's patch is concatenated
  to stdout rather than written as a separate patch file. Headers are relative
  to the current directory, so the stream is `git apply`-able as one patch.
- **`six.moves` is not exempt by default**, even though the style guide
  mentions it. Add it with `--exempt six.moves` or
  `exempt_modules = ["six.moves"]`.
- **Probe results are not persisted.** They are cached in memory for the run
  only, so a very large third-party surface re-pays the (batched) probe cost
  on every invocation.
- **`__init__.py` bindings are cached by path for the run**, without an mtime
  check. This is safe only because nothing in a single run rewrites a scanned
  `__init__.py`'s plain assignments — an assumption about the fixer's current
  narrow scope, not an enforced invariant.
- **Capture-pattern bindings in an `__init__.py` are not collected** when
  looking for names that shadow a submodule. A module-level `match` statement
  in an `__init__.py` binding a name that collides with a real submodule is
  vanishingly rare; the limit is stated rather than silently assumed.
