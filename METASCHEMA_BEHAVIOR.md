# Metaschema Processor — Behavior Reference

This document describes how the metaschema processor (MSP,
[`source_proc.py`](src/ga4gh/gkm/metaschema/tools/source_proc.py)) turns a
`*-source.yaml` document into the artifacts consumed downstream:

- the in-memory **processed schema** (`processor.for_js`) — a JSON Schema
  2020-12 document with every class under `$defs`;
- **split per-class JSON** files (`source2splitjs.py` → `json/<Class>`);
- **reStructuredText docs** (`y2t.py` → `def/<Class>.rst`).

It is the source of truth for *current* processor behavior. Keep it in sync
when the processing rules change.

> **Status:** This reflects the migration to the `abstract: true` class
> convention. The older `heritableProperties` / `heritableRequired` /
> `extends` model has been removed (see [History](#history)).

---

## 1. Class model

Every entry under `$defs` is one of these kinds:

| Kind | How it is recognized | In the source |
|------|----------------------|---------------|
| **Abstract** | `abstract: true` | `type: object` + `properties` / `required` |
| **Concrete (inherited)** | no `abstract` flag; `inherits:` a parent | `inherits:` + `properties` (no `type`; the processor injects `type: object`) |
| **Concrete (composed)** | no `abstract` flag; top-level `allOf` / `anyOf` / `oneOf` | `allOf: [ {$ref: Base}, {properties: …} ]` — no `inherits`, no `type` (injected). Used by the recipes/profiles, e.g. `Condition`, `VariantPathogenicityStatement` |
| **Primitive** | `type` is not `object`/absent | e.g. `type: string`, `type: array` |

Notes:

- **`type: object` is injected** by the processor for every non-primitive
  class (abstract and concrete), so concrete sources omit it.
- Abstract and concrete classes both store members under **`properties`** /
  **`required`**. `heritableProperties` / `heritableRequired` are no longer
  used.
- **Empty `properties` / `required` are omitted.** When a class's merged
  property set (or required set) is empty — e.g. an `allOf`-composed recipe
  whose members live under `allOf`, or a class that requires none of its
  properties — the processor emits no `properties: {}` / `required: []`. (Both
  are valid Draft 2020-12 but pure noise on property-less/requirement-less
  classes.)
- **Composed concrete classes are not property-merged at the source level.**
  Unlike the `inherits:` form (which copies the parent's `properties`/`required`
  into the child — see [§2](#2-inheritance-inherits)), a composed concrete
  class keeps its `allOf`/`anyOf`/`oneOf` in the emitted schema; the base's
  members apply via composition. It is closed with the composition-aware
  `unevaluatedProperties: false` (see [§7](#7-closure-of-additional-properties-strict))
  and its RST renders the flattened effective-property table
  (see [§8](#8-outputs)).

## 2. Inheritance (`inherits`)

A class declares a single parent with `inherits: <Class>` (or
`inherits: <namespace>:<Class>` for an imported parent). The processor copies
the parent's resolved `properties` and `required` into the child.

- **Property order is superclass-first.** The top-level superclass's
  properties come first, and each descendant appends its own. See
  [§4](#4-property-ordering).
- Inheritance currently assumes an **abstract parent**.

## 3. Property specialization — schema covariance (no renaming; `extends` removed)

A subclass **specializes** an inherited property by **redeclaring it under the
same name**. The subclass attributes are merged over the inherited definition
(subclass wins), and polymorphic refs are reconciled (a narrowing `$ref`
replaces an inherited `oneOf`/`anyOf`, and vice versa).

The governing invariant is **schema covariance (Liskov substitution): a parent
schema must always validate an instance of a subclass.** Concretely, a subclass
**may**:

- add new properties (the abstract parent is left open — see [§7](#7-closure-of-additional-properties-strict) — so it still accepts them);
- narrow an inherited property with additional constraints (e.g. add `const`,
  `enum`, `pattern`, tighter `minItems`/`maxItems`);
- specialize `description` / `$comment` / array sizes.

A subclass **may not**:

- **rename** an inherited property;
- **change** an inherited property's `type`, `const`, or `default` (adding one
  where the parent has none is a narrowing and *is* allowed).

Violations raise a `ValueError` during processing. The legacy **`extends`
keyword is not supported** and also raises; redeclare the property under its own
name instead.

## 4. Property ordering

Emitted properties follow the inheritance chain top-down:

1. top-level superclass properties (in their declared order),
2. then each descendant's own properties, appended,
3. an **overridden** property keeps its **inherited position** (it is merged in
   place, not moved to the end).

Example — `Allele`:

```text
id, type, name, description, aliases, extensions,   # from Entity (top-level)
digest,                                             # from Ga4ghIdentifiableObject
expressions,                                        # from Variation
location, state                                     # Allele's own
```

`type` is overridden by `Allele` (adds `const: "Allele"`) but keeps `Entity`'s
position 2.

## 5. Every class is emitted

Every class — **including abstract classes** — is emitted as its own JSON
Schema under `$defs`. Nothing is pruned, and abstract classes are **not**
collapsed into a `oneOf` of their descendants.

## 6. References

- **`$ref` must be local** — a `#/$defs/<Class>` (or `#/definitions/<Class>`)
  fragment. Any other form (a bare class name like `$ref: CategoricalVariant`,
  or an external path/URL) raises a `ValueError` naming the offending value and
  the file it's in, with a fix hint to use `$refCurie` instead. A cross-schema
  reference always goes through `$refCurie`, so it resolves the same way in
  every artifact (split `json/`, RST `def/`, and the merged document) rather
  than depending on a per-artifact fallback (see [History](#history)).
- `$refCurie` values are resolved against the schema's `namespaces` map into
  `$ref`s. Resolution walks the **entire** class definition — top-level
  `properties`, any class-level `allOf`/`anyOf`/`oneOf` composition, and
  primitive/array-alias bodies (e.g. refs nested under `items` / `contains`).
  It is **not** gated on a class being an abstract "container", so concrete
  composed classes (the recipes/profiles) and primitive aliases resolve their
  nested refs too (previously these leaked an unresolved `$refCurie` into the
  emitted schema).
- A resolved `$refCurie` is only well-formed if its `namespaces` mapping
  targets a `#/$defs/` fragment (e.g. `../vrs/vrs.yaml#/$defs/`). A mapping that
  omits the fragment yields a fragmentless, broken `$ref`; the processor does
  **not** currently validate this, so it surfaces later (in `source2splitjs`)
  rather than at resolve time.
- A `$ref` that targets an **abstract class stays a direct `$ref`** — it is
  *not* expanded into a `oneOf` of concrete descendants.
- Metaschema-only keywords (`inherits`, `protectedClassOf`, `header_level`)
  are stripped from the emitted JSON Schema. **`abstract` is the exception:**
  it survives as `abstract: true` on classes that are actually abstract, so a
  consumer of the per-class JSON can tell abstract and concrete classes apart
  without cross-referencing the source YAML. Concrete classes omit the key
  entirely rather than carrying `abstract: false`. It is not a standard JSON
  Schema keyword, but unknown keywords are ignored by validators, not
  rejected, so this doesn't affect validation.
- **`$comment` is stripped everywhere.** It is treated as an internal,
  source-only annotation: it stays in the `*-source.yaml` but is removed
  recursively from the emitted JSON Schema wherever it appears (class level,
  properties, or nested composition branches). (It is a valid JSON Schema
  keyword; the processor drops it by policy, not because it is invalid.)

## 7. Closure of additional properties (`strict`)

Whether a schema closes its objects is driven by the top-level `strict: true`
flag. For a **strict** schema the processor emits:

| Class shape | Emitted keyword |
|-------------|-----------------|
| Concrete, **not** composed | `additionalProperties: false` |
| Concrete, composed with `allOf` / `anyOf` / `oneOf` | `unevaluatedProperties: false` |
| Abstract | *nothing* — left open (see below) |

Why the distinction matters:

- `additionalProperties: false` is **blind to `allOf`/`$ref`/`oneOf`**: on a
  composed class it would see no locally-declared properties and reject the
  inherited/composed ones. Composed classes therefore use the
  composition-aware `unevaluatedProperties: false`.
- **Abstract classes deliberately omit `additionalProperties`.** JSON Schema
  already allows extra properties by default, so this does not loosen
  standalone validation. Crucially, emitting `additionalProperties: true`
  would mark *every* property "evaluated" and **defeat**
  `unevaluatedProperties: false` on any concrete class that composes the
  abstract class via `allOf`.

`strict` also enables `enforce_ordered`: every array property must declare an
`ordered: <bool>` attribute (`enforce_ordered` can be set independently).

## 8. Outputs

- `processor.for_js` — the cleaned JSON Schema document (descriptions
  RST-scrubbed to Markdown, metaschema-only keywords removed).
- `source2splitjs.split_defs_to_js(proc)` — one JSON file per class under
  `json/`, with cross-references rewritten to file paths.
- `y2t.main(proc)` — the `.rst` docs under `def/`. Highlights:
  - A **maturity note** (draft or trial use) is emitted as an admonition at the
    top of each class, **titled with the maturity level** (*Draft* / *Trial
    Use*) and colored via `:class:` (`warning` for draft, `note` for trial use,
    so the colors differ). Normative classes get no note. Its body ends with
    the RST **substitution reference** `|maturity-model|` rather than a
    hardcoded link, so each downstream doc site controls the link target/text.
    **The consuming Sphinx build must define the substitution** (otherwise the
    build errors with "Undefined substitution referenced"), e.g. in `conf.py`:

    ```python
    rst_prolog = """
    .. |maturity-model| replace:: `Maturity Model </appendices/maturity_model.html>`_
    """
    ```

  - Abstract classes are flagged with an **Abstract Class** notation.
  - **`allOf`-composed** classes (recipes/profiles) render a **flattened
    effective-property table**: the base class's properties overlaid with the
    subclass's local properties, showing each property's effective (**narrowed**)
    type (e.g. a `contains` constraint's specific member type). A referenced
    base that is itself `allOf`-composed (a two-level composition chain, e.g.
    a profile class composing another profile class rather than a plain
    entity) is flattened **recursively**, so its own composed properties (and
    its base's, transitively) are picked up too — not silently dropped, which
    a single-level lookup would do. `oneOf`/`anyOf` unions render a "one of /
    any of the following" summary, describing each member by whichever of
    its shape it has: a `$ref`/`$refCurie`/nested `oneOf`/`anyOf` (the
    referenced type), a `properties` narrowing ("an object constraining
    *X*"), a bare `required` list ("an object requiring *X*"), both
    together, or, only if none of those apply, the generic "an object with
    additional constraints" fallback. **Exception:** if *every* member of
    the `oneOf`/`anyOf` reduces to a bare `required` list (the "at least one
    of these properties must be present" idiom — e.g. `MappableConcept`'s
    `anyOf: [{required: [name]}, {required: [primaryCoding]}]`), this
    bullet-list summary is skipped entirely in favor of one concise sentence
    under **Additional Constraints** (see below) — "This class requires at
    least one of *name* or *primaryCoding*."
  - Each class table is followed by **Inherits:** (the class's own direct
    `inherits` target, if any — a cross-schema `namespace:Class` value
    resolves to the bare class name), **Composes:** (an `allOf`-composed
    class's base class(es), referenced via `$ref`/`$refCurie` at the top of
    an `allOf` member — the `allOf` equivalent of **Inherits:**, since
    composition and inheritance are separate, mutually exclusive mechanisms
    in this codebase's convention), **Subclasses:** (classes whose
    `inherits` resolves to it — the mirror of **Inherits:**), and **Used
    in:** (classes that reference it via `$ref`/`$refCurie`) cross-reference
    lists, in that order.
  - **Additional Constraints.** `render_additional_constraints` renders two
    otherwise-invisible constraint shapes under one shared **Additional
    Constraints** heading:
    - An `allOf` member's `if`/`then`/`else` (business-rule-style
      conditional narrowing — e.g. AMP/ASCO/CAP's tier-/methodType-dependent
      constraints) has neither a top-level `$ref` nor a top-level
      `properties` key (the condition/consequence properties are nested one
      level deeper), so `flatten_allof` silently skips it and the main
      property table shows only the unconditional base shape.
      - A member whose condition reduces to a single "property equals
        value" pin (`const`/`enum`/a bounds-style keyword on exactly one
        nested property — the common case) is rendered as a row in one
        shared **If property... / has value... / then property... /
        must...** table, one row per consequence property (or per bare
        `required` entry that has no narrowing of its own, rendered as "be
        provided"). This is flatter and easier to scan than prose when a
        class has many such branches (e.g. a `methodType`-keyed rule per
        criterion).
      - Anything that doesn't reduce that way — a compound (multi-property)
        condition, an `else`, or a condition pinned via a structural
        (`resolve_type`-resolved) constraint rather than a plain value —
        falls back to prose: `If <condition>, then: <bullet list>`.
    - A top-level `oneOf`/`anyOf` that's purely a set of bare `required`
      alternatives (the "at least one of these properties must be present"
      idiom — see above) renders as one concise sentence here instead of
      `resolve_composition`'s generic per-member bullet list, e.g. "This
      class requires at least one of *name* or *primaryCoding*." When this
      sentence is rendered, `render_class` skips calling
      `resolve_composition` for the class, so the same constraint isn't
      described twice.
    - **Styling convention** throughout both: a property/path name renders
      in *italics* (e.g. `*specifiedBy.methodType*`); a concrete value or
      pattern renders in **bold** (e.g. `**population_frequency**`,
      `**^(SBVS1|SBS1|OP4)(_.+)?$**`) — easier to visually distinguish "what
      property" from "what value" than the uniform ``code`` literal styling
      used elsewhere in the docs. Each consequence property (table cell or
      prose bullet) resolves to one of: a `const`/`enum` value ("have value
      **X**" / "have one of: **X**, **Y**"), a `pattern` (regex) constraint
      ("match the pattern **X**"), a structural narrowing via `resolve_type`
      ("be narrowed to: `X`" / "be one of: `X`, `Y`" for a `$ref`/union,
      still `:ref:`-linked rather than bolded), the
      JSON-Schema-boolean-`false` forbidden-property form ("not be
      provided"), or any other bounds-style keyword
      (`minimum`/`maximum`/`minLength`/`maxLength`/`format`) that
      `resolve_type` doesn't recognize. `resolve_type`'s internal
      `"_Not Specified_"` sentinel never leaks into rendered docs — every
      unhandled keyword falls back through `_describe_bounds`.
  - A **GA4GH Digest** section (prefix + inherent properties) is rendered for
    **concrete** GA4GH-identifiable classes only. Abstract classes omit it even
    when they carry/inherit a `ga4gh` block, since they are never instantiated —
    the digest applies to the concrete subclasses that inherit it.
  - `y2t` renders a source's **full transitive import closure** (all
    `*-source.yaml` beside it plus their imports, recursively — see
    [`_folder_processors`](src/ga4gh/gkm/metaschema/scripts/y2t.py)), but splits
    *where* each class lands: a source's **own** classes go into its own
    `def/` (mirroring how `source2splitjs` only emits a source's own classes
    into `json/`); every other class reached through the closure is rendered
    into the shared **top-level** `def/` (a profile's `def/XXX`'s parent, or
    `def/` itself for a non-profile source). Several sources in a folder can
    pull the same imported class into their closure — each render is
    identical regardless of which source triggers it, so this is a
    deduplicated union, not per-source duplication. A **profile source is the
    exception** for *rendering* scope — it never renders a sibling profile's
    own classes (see [§9](#9-profile-sub-namespaces)).
  - **Used in:**/**Subclasses:** cross-reference lists are computed over
    **every** `*-source.yaml` in the folder — base sources *and* every
    profile alike (see
    [`_folder_xref_processors`](src/ga4gh/gkm/metaschema/scripts/y2t.py)) —
    not just the closure of whichever source is currently rendering. This
    matters for shared top-level `def/` files: several sources can each pull
    in the same imported class and narrow/reference it differently, and
    computing cross-references only from the current invocation's own closure
    would make the file's xref list depend on which source's `y2t` run
    happened to write it last. Computing them folder-wide instead makes every
    render of a given class identical regardless of write order.

Imports are only pulled in as dependencies; a schema's own `json/` artifacts are
produced only when the scripts are run **on that schema's processor** (see the
tests for examples).

## 9. Profile sub-namespaces

A source file named **`XXX-profile-source.yaml`** contributes `XXX` as a
**sub-namespace**. This lets several profiles live side-by-side in one folder
(e.g. all of `va-spec/`) while each keeps a distinct output location and `$id`
space:

- **Outputs** go to `<parent>/json/XXX` and `<parent>/def/XXX` — nested inside
  the folder's shared `json`/`def` dirs, not as sibling top-level folders. A
  folder's only direct children are ever `json/` and `def/`.
- **Every class `$id`** is `.../<version>/json/XXX/<Class>` — the `XXX` segment
  is injected after `json`.
- **Validation:** the `XXX` taken from the filename must equal the `XXX` in the
  file's own `$id` (its final path segment, `.../<version>/XXX-profile-source.yaml`).
  A mismatch raises a `ValueError` (with a fix hint) during processing.
- **Docs:** a profile is a standalone unit for *rendering* — `y2t` renders that
  profile's own classes into `def/XXX`, and every other class in its own
  import closure into the shared top-level `def/`, so sibling profiles in the
  same folder never end up with each other's classes in their `def/XXX`.
  **Used in:**/**Subclasses:** cross-references, however, are computed across
  every source in the folder (base sources and every profile together — see
  [§8](#8-outputs)), so they stay complete on shared top-level `def/` files
  that more than one profile references.

Non-profile sources are unaffected: their outputs and `$id`s continue to derive
from the source's own location / `$id` (e.g. `va-core-source.yaml` at
`va-spec/` emits to `va-spec/json` with `$id` `.../<version>/json/<Class>`).

## 10. Sealed abstract classes (`sealed`)

By default, a `$ref`/`$refCurie` to an **abstract** class stays a direct
`$ref` — it validates *any* structurally-conforming subclass instance,
including ones the schema doesn't know about yet (see [§6](#6-references)).
That's the right default for an extensible type: it doesn't foreclose
downstream schemas defining new subclasses. Some abstract classes, though,
really do represent a **closed, fully-known set of subtypes** — every
subclass that will ever exist is already declared in the same file, and a
reference site should be restricted to exactly that set. `sealed: true` opts
a class into that narrower contract:

- **Only meaningful on an `abstract` class.** Setting `sealed: true` on a
  concrete class raises a `ValueError`.
- **Auto-derives a `oneOf`** from the class's own subclass tree (`inherits:`,
  resolved transitively, same-source only — a cross-source `inherits:
  namespace:Class` isn't counted, matching [§2](#2-inheritance-inherits)'s
  general rule). Only **concrete** descendants are listed; an abstract
  intermediate is skipped in favor of its own concrete descendants, since an
  abstract member's open contract would let a value double-match under
  `oneOf`'s "exactly one" requirement. A sealed class with **zero** concrete
  descendants raises — an empty `oneOf` can never be satisfied.
- **Mutually exclusive with a hand-authored `oneOf`/`anyOf`/`allOf`** on the
  same class — combining an auto-derived union with a manual one is
  ambiguous, and raises.
- **Materialized before any other processing runs**, so a sealed class is
  indistinguishable from a hand-authored container class (one that declares
  its own `oneOf`/`anyOf`/`allOf` directly) to everything downstream:
  `class_is_container`, the split per-class `json/` output, and `y2t`'s RST
  rendering (which renders the closed union as a "must match one of the
  following" list, alongside the class's own property table and
  **Subclasses:** cross-references) all just work, unchanged.
- **`sealed` itself is stripped** from the emitted JSON Schema once
  materialized into `oneOf` — like the other metaschema-only keywords, it
  carries no further information for a consumer of the output.
- **Idempotent.** Resolution runs again whenever `_init_from_raw` does —
  which happens more than once on the same class in two situations:
  `merge_imported()` re-derives everything after merging in every import's
  `raw_defs`, and `import_dependencies` builds one `YamlSchemaProcessor`
  instance *per import edge*, so the same file (and the same sealed class in
  it) can be independently resolved more than once and then merged together
  by `merge_imported()`. An internal marker on the class's raw def (stripped
  like `sealed` itself) records that its `oneOf` was already derived, so a
  repeat pass is a no-op instead of tripping the "already has `oneOf`"
  conflict guard against its own previously-derived union.
- **`y2t`'s RST** carries an explanatory **Sealed** note (alongside the
  **Abstract Class** note) spelling out the restriction in prose, since
  "must match one of the following" on its own doesn't say *why* the list is
  exhaustive.
- **Used in:/Subclasses: stay accurate for transitively-sealed hierarchies.**
  A sealed class's `oneOf` can `$ref` a *grandchild* class (reached through
  an abstract intermediate — e.g. `Variation`, sealed, directly `$ref`s
  `Allele`, whose actual parent is the abstract `MolecularVariation`).
  Without accounting for this, that `$ref` would show up as a spurious
  **Used in:** `Variation` entry on `Allele`'s page, duplicating the
  **Subclasses:** relationship already shown on `MolecularVariation`'s page.
  `build_cross_references`'s existing "skip the container's own subclass
  enumeration" rule (see [§8](#8-outputs)) was extended from a direct
  parent/child check to a transitive one to cover this.

Sealing a class is a **non-breaking, purely additive** change from the
perspective of anything that references it: every existing `$ref`/`$refCurie`
pointing at the sealed class keeps working exactly as written — only the
sealed class's *own* emitted schema gains the `oneOf`.

---

## 11. iriReference-preservation warnings (`LostIriReferenceWarning`)

A property whose schema offers `iriReference` as an alternative (e.g.
`specifiedBy: oneOf: [Method, iriReference]`) means "this can be a concrete
object, or an external reference to one." When a descendant/composing class
narrows that property, it's easy to accidentally narrow away the
`iriReference` alternative — leaving only the concrete type — without
noticing, since a narrower schema is still a *valid* narrowing in the schema
covariance sense (see [§3](#3-property-specialization--schema-covariance-no-renaming-extends-removed)),
so nothing here fails processing. The processor now emits a
`LostIriReferenceWarning` ([`warnings.warn`](https://docs.python.org/3/library/warnings.html),
not an error) whenever it detects this, covering both narrowing mechanisms:

- **`inherits:`.** The processor already merges a parent's property with the
  child's override into one effective schema (see [§3](#3-property-specialization--schema-covariance-no-renaming-extends-removed)).
  If the property offered `iriReference` before the child's override is
  applied but not after, it warns.
- **`allOf` composition.** Unlike `inherits:`, a composed class's local
  `properties` override is **not** merged with its composed base at the
  source level — the emitted schema keeps every `allOf` member as its own
  separate constraint (see [§1](#1-class-model)). JSON Schema's `allOf`
  applies all of them **simultaneously** (intersection): an instance's
  property value must satisfy the composed base's schema *and* the local
  override's schema at once. A local override that narrows to a bare
  concrete type therefore silently excludes `iriReference` regardless of
  what the composed base allows — a real bug found this way: `aac-2017`'s,
  `acmg-2015`'s, and `ccv-2022`'s `proposition` narrowings (composing
  `va.core:Statement`, whose `proposition` offers `iriReference`) each
  excluded it via a bare `$refCurie` override. The check resolves a
  composed base's *effective* properties recursively (through its own
  `allOf`/`inherits`, mirroring but not sharing code with `y2t.py`'s
  `flatten_allof`) so it also catches the case through a multi-level
  composition chain.

A description-only override (no `$ref`/`$refCurie`/`oneOf`/`anyOf`/`type`
key) never triggers this — it doesn't change what the property accepts, so
there's nothing to lose. This is deliberately a **warning, not an error**:
excluding `iriReference` in a narrowing is sometimes intentional (e.g. a
profile that genuinely wants to forbid external references for a specific
property), so the processor flags it for review rather than blocking the
build.

---

## Known limitations

- **No source-attribute validation.** The processor is a transform, not a
  validator: unknown/legacy class-level keys (e.g. a leftover
  `heritableProperties`, or a `namespaces` mapping missing its `#/$defs/`
  fragment) pass through without a dedicated error and only surface downstream.
  Targeted guards exist for specific removed/disallowed patterns (`extends`,
  the covariance rule, maturity ordering, non-local `$ref` — see
  [§6](#6-references)), but there's no holistic "shape of the source file"
  validation pass; adding one would need to enumerate the checks and decide how
  strict to be, not just patch one more case.
- **Test scope.** Automated tests cover every `*-source.yaml` currently in
  this repo (`gkm-core`, `vrs`, `cat-vrs`, `recipes`, and the `va-spec`
  schemas: `domain-entities`, `va-core`, and the `aac-2017` / `acmg-2015` /
  `ccv-2022` profiles) — there are no excluded or skipped fixtures. These are
  representative test fixtures for exercising the processor, though, not
  necessarily the full, real schemas maintained in the corresponding GKS
  product repos (va-spec, cat-vrs, etc.), which may be larger or drift as
  those repos evolve independently of this one.

## Testing

- [`tests/test_source_proc_units.py`](tests/test_source_proc_units.py) —
  unit tests for pure helpers and class predicates, plus **structural**
  assertions that the correct closure keyword is emitted per class shape.
- [`tests/test_schema_validation.py`](tests/test_schema_validation.py) —
  **behavioral** proof (via a real draft 2020-12 validator) that concrete
  classes reject extra properties (`additionalProperties`) and that
  `allOf`-composed classes reject extras (`unevaluatedProperties`) while still
  accepting composed properties.
- [`tests/test_gkm_vrs.py`](tests/test_gkm_vrs.py) — build, output-generation,
  ordering, abstract-emission, direct-ref, and `extends`-rejection checks
  across the in-scope schemas.

Run everything with `make test` (or `pytest tests/`).

---

## History

Behaviors intentionally **removed / changed** during the migration:

- `extends` (property renaming) — **removed**; now raises `ValueError`.
- `heritableProperties` / `heritableRequired` — **removed**; replaced by
  `properties` / `required` plus the explicit `abstract: true` flag.
- Abstract classes were previously **pruned** from output or collapsed into a
  `oneOf` union; they are now **emitted as their own schemas** and referenced
  directly.
- Abstract classes briefly emitted `additionalProperties: true`; this was
  **removed** because it defeats `unevaluatedProperties: false` on composed
  classes.
- `$refCurie` resolution was **ungated from `class_is_container`**: previously
  only abstract "container" classes had their class-level composition refs
  resolved, so concrete composed classes (recipes/profiles) and primitive/array
  aliases leaked unresolved `$refCurie`. Resolution now walks the whole class
  definition (see [§6](#6-references)).
- Empty `properties: {}` / `required: []` are now **omitted** rather than
  always emitted (see [§1](#1-class-model)).
- A bare or external `$ref` (anything but `#/$defs/<Class>`) is now
  **rejected** with a `ValueError`, rather than silently resolved by searching
  every import for a class with a matching tail-segment name. That fallback
  worked by coincidence (it happened to find the right class by name) rather
  than by declared intent, and could mask a genuinely wrong reference —
  `ccv-2022-profile-source.yaml` had two `$ref`s hardcoding a stale
  version/path segment that the fallback silently papered over. Fix: use
  `$refCurie: <namespace>:<Class>` (see [§6](#6-references)).
- `import_dependencies`/`merge_imported()` now **resolve** import paths
  (`Path.resolve()`) before storing/comparing them. Previously, the same
  imported file reached via two different relative routes (a diamond, e.g.
  `recipes` → `cat-vrs` → `gkm-core` *and* `recipes` → `vrs` → `gkm-core`)
  compared as two different files and `merge_imported()` raised. Diamond
  imports are common and expected, not an error. `_register_merge_import` also
  now memoizes by resolved path so a diamond doesn't re-walk the same file's
  import subtree once per route that reaches it. Separately, the
  post-merge curie-namespace remapping (which points every curie prefix used
  anywhere in the merged content at the now-local `#/$defs/`) was keyed by the
  wrong dictionary (import dependency names instead of curie prefixes) and
  has been corrected.
- `abstract` is now **preserved** (as `abstract: true`) in the emitted JSON
  Schema for classes that are actually abstract, rather than being stripped
  unconditionally like the other metaschema-only keywords (see
  [§6](#6-references)). Nothing internal to the processor depended on the
  stripped behavior — `class_is_abstract()` and everything built on it read
  the flag from the raw source schema, not from `for_js` — so this only
  changes what a consumer of the per-class JSON can observe.
- **`sealed` reintroduces, as an opt-in, the concretize-to-`oneOf` behavior**
  the "Abstract classes were previously pruned... or collapsed into a `oneOf`
  union" note above describes being removed. The old behavior applied
  unconditionally to every abstract class's *reference sites*; `sealed` is
  the opposite shape — a per-class opt-in that changes only the sealed
  class's *own* emitted schema (every existing `$ref` to it keeps working
  unchanged) — see [§10](#10-sealed-abstract-classes-sealed).
- Every class's `.rst` now carries an **Inherits:** line (its own direct
  `inherits` target, resolving a cross-schema `namespace:Class` value to the
  bare class name) immediately above **Subclasses:** — the mirror relation
  (a class's parent vs. its children) is now shown symmetrically, where
  previously only the "Some `X` attributes are inherited from `Y`" preface
  sentence on the property table hinted at the parent, and only for classes
  that render a property table at all (passthrough and primitive classes had
  no inheritance mention whatsoever).
- Every `allOf`-composed class's `.rst` now carries a **Composes:** line
  (the `allOf` equivalent of **Inherits:**) alongside it, and every `allOf`
  `if`/`then`/`else` member — previously silently dropped by `flatten_allof`
  and entirely invisible in the rendered docs — now renders under a
  **Conditional Constraints** heading, either as a row in an **If
  property.../has value.../then property.../must...** table (when the
  condition reduces to a single "property equals value" pin) or as prose
  (see [§8](#8-outputs)). A `then`/`else` consequence narrowed via `pattern`
  (or any other bounds-style keyword `resolve_type` doesn't recognize) was a
  real bug found in review: it fell through to `resolve_type`'s internal
  `"_Not Specified_"` sentinel, which leaked verbatim into the rendered
  docs (e.g. every CCV/ACMG `methodType`-keyed branch on
  `VariantOncogenicityEvidenceLine`/`VariantPathogenicityEvidenceLine`) —
  fixed by `_describe_bounds`.
- `flatten_allof` (the RST **Information Model** table for `allOf`-composed
  classes) now **recurses** into a referenced base that is itself
  `allOf`-composed, rather than reading only its top-level `properties`. A
  two-level composition chain — a profile class composing another profile
  class rather than a plain entity, e.g. `aac-2017`'s
  `DiagnosticEvidenceLine`/`PrognosticEvidenceLine`/`TherapeuticEvidenceLine`
  each composing `AmpAscoCapEvidenceLine` (itself composed from
  `va.core:EvidenceLine`) — previously rendered a table with only the
  subclass's own locally-added property, silently dropping everything the
  intermediate base itself composed in.
- **Conditional Constraints renamed to Additional Constraints**, and
  broadened to also cover a top-level `oneOf`/`anyOf` that's purely a set of
  bare `required` alternatives (previously described only via
  `resolve_composition`'s generic per-member bullet list, which rendered an
  uninformative duplicate line for e.g. `MappableConcept`'s `anyOf` before a
  separate fix taught `describe_composition_member` to name a `required`-only
  member's properties). That specific idiom now collapses to one concise
  sentence — "This class requires at least one of *name* or
  *primaryCoding*." — under the same heading as the `if`/`then`/`else`
  table/prose rendering, rather than two unrelated-looking sections. Also:
  property/path names throughout **Additional Constraints** now render in
  *italics* and concrete values/patterns in **bold**, replacing the uniform
  ``code`` literal styling used for both — easier to tell "what property"
  from "what value" at a glance (see [§8](#8-outputs)).
- **`LostIriReferenceWarning` added** (see [§11](#11-irireference-preservation-warnings-lostirireferencewarning)),
  a non-fatal `warnings.warn` the processor now emits whenever a narrowing
  (`inherits:` or `allOf` composition) drops an `iriReference` alternative a
  parent/composed base offered. Found by inspection, not by the new check
  itself (which didn't exist yet): `Statement.proposition`'s three profile
  narrowings each excluded `iriReference` via `allOf`'s intersection
  semantics, even after the base was fixed to offer it. The check exists so
  this class of bug — schema-covariance-valid, so nothing else catches it —
  gets flagged automatically going forward instead of relying on manual
  audits.
