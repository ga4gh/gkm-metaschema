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
- Metaschema-only keywords (`inherits`, `abstract`, `protectedClassOf`,
  `header_level`) are stripped from the emitted JSON Schema.
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
    type (e.g. a `contains` constraint's specific member type). `oneOf`/`anyOf`
    unions render a "one of / any of the following" summary.
  - Each class table is followed by **Used in:** (classes that reference it via
    `$ref`/`$refCurie`) and **Subclasses:** (classes whose `inherits` resolves
    to it) cross-reference lists.
  - A **GA4GH Digest** section (prefix + inherent properties) is rendered for
    **concrete** GA4GH-identifiable classes only. Abstract classes omit it even
    when they carry/inherit a `ga4gh` block, since they are never instantiated —
    the digest applies to the concrete subclasses that inherit it.
  - `y2t` is a **folder-level** build: it renders every class in a folder's
    import closure (all `*-source.yaml` beside it plus their imports,
    recursively) into that folder's `def/`, so the folder is self-contained and
    its cross-reference lists are accurate *from that folder's perspective*. A
    **profile source is the exception** — it is its own documentation unit (see
    [§9](#9-profile-sub-namespaces)) and is never grouped with siblings.

Imports are only pulled in as dependencies; a schema's own `json/` artifacts are
produced only when the scripts are run **on that schema's processor** (see the
tests for examples).

## 9. Profile sub-namespaces

A source file named **`XXX-profile-source.yaml`** contributes `XXX` as a
**sub-namespace**. This lets several profiles live side-by-side in one folder
(e.g. all of `va-spec/`) while each keeps a distinct output location and `$id`
space:

- **Outputs** go to `<parent>/XXX/json` and `<parent>/XXX/def` (not the folder's
  own `json`/`def`).
- **Every class `$id`** is `.../<version>/XXX/json/<Class>` — the `XXX` segment
  is injected before `json`.
- **Validation:** the `XXX` taken from the filename must equal the `XXX` in the
  file's own `$id` (its final path segment, `.../<version>/XXX-profile-source.yaml`).
  A mismatch raises a `ValueError` (with a fix hint) during processing.
- **Docs:** a profile is a standalone documentation unit — `y2t` renders only
  that profile's import closure into `XXX/def`, so sibling profiles in the same
  folder never cross-contaminate each other's docs.

Non-profile sources are unaffected: their outputs and `$id`s continue to derive
from the source's own location / `$id` (e.g. `va-core-source.yaml` at
`va-spec/` emits to `va-spec/json` with `$id` `.../<version>/json/<Class>`).

---

## Known limitations

- **Bare local `$ref`s in `recipes-source.yaml`.** The cat-vrs recipe classes
  compose with `allOf: [ {$ref: CategoricalVariant}, … ]` using **bare** local
  references (`$ref: CategoricalVariant`, `$ref: DefiningAlleleConstraint`, …)
  rather than `#/$defs/CategoricalVariant`. In the split per-class `json/`
  artifacts these are rewritten to resolvable file paths (e.g.
  `/ga4gh/schema/cat-vrs/1.x/json/CategoricalVariant`), but in the in-place
  merged document (`for_js`) they remain bare and would not resolve in a
  standalone validator. (The cross-schema `$refCurie` references that used to
  leak unresolved are now resolved — see [§6](#6-references).) End-to-end
  instance validation additionally depends on the referenced per-class files
  being served at their `$id` paths. The `allOf` closure *mechanism* is proven
  behaviorally against a processor-generated synthetic class, and the real
  recipe classes are proven structurally to meet both closure preconditions
  (see [Testing](#testing)).
- **`merge_imported()` fails on the recipes import graph.** Recipes import
  cat-vrs (which imports vrs + gkm-core) *and* vrs/gkm-core directly; the merge
  step asserts a single location per import name and raises on this diamond.
- **No source-attribute validation.** The processor is a transform, not a
  validator: unknown/legacy class-level keys (e.g. a leftover
  `heritableProperties`, or a `namespaces` mapping missing its `#/$defs/`
  fragment) pass through without a dedicated error and only surface downstream.
  Targeted guards exist for specific removed patterns (`extends`, the covariance
  rule, maturity ordering).
- **Test scope.** Automated tests currently cover `gkm-core`, `vrs`,
  `cat-vrs`, `recipes`, and the `va-spec` schemas (`base/domain-entities`,
  `base/va-core`, and the `aac-2017` / `acmg-2015` / `ccv-2022` profiles).
  Other GKS source YAMLs are excluded while the set is mid-migration; a few
  legacy tests are skipped for removed fixtures.

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
