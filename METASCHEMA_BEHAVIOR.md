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
    type (e.g. a `contains` constraint's specific member type). `oneOf`/`anyOf`
    unions render a "one of / any of the following" summary.
  - Each class table is followed by **Used in:** (classes that reference it via
    `$ref`/`$refCurie`) and **Subclasses:** (classes whose `inherits` resolves
    to it) cross-reference lists.
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
