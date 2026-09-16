#!/usr/bin/env python3
"""convert input .yaml to .rst artifacts"""

import os
import pathlib
import sys
from io import TextIOWrapper
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from ga4gh.gkm.metaschema.tools.source_proc import YamlSchemaProcessor

templates_dir = Path(__file__).resolve().parents[4] / "templates"
env = Environment(loader=FileSystemLoader(templates_dir))

# Mapping to corresponding hex color code and code for maturity status
MATURITY_MAPPING: dict[str, tuple[str, str]] = {
    "draft": ("D3D3D3", "D"),
    "trial use": ("FFFF99", "TU"),
    "normative": ("B6D7A8", "N"),
    "deprecated": ("EA9999", "X"),
}

# Mapping to corresponding code for ordered property in arrays
ORDERED_MAPPING: dict[bool, str] = {True: "&#8595;", False: "&#8942;"}


def _ref_label(ref: str) -> str:
    """Class-name label for a $ref path or $refCurie (namespace/path stripped)."""
    frag = ref.split("#")[-1]
    return frag.rsplit("/", 1)[-1].rsplit(":", 1)[-1]


def _array_item_type(class_property_definition: dict) -> str:
    """Effective item type of an array: prefer narrowed ``contains`` schemas
    (top-level or within ``allOf``) over the base ``items``."""
    contained = []
    if "contains" in class_property_definition:
        contained.append(class_property_definition["contains"])
    for member in class_property_definition.get("allOf", []):
        if isinstance(member, dict) and "contains" in member:
            contained.append(member["contains"])
    types: list = []
    for schema in contained:
        resolved = resolve_type(schema)
        if resolved != "_Not Specified_" and resolved not in types:
            types.append(resolved)
    if types:
        return " + ".join(types)
    return resolve_type(class_property_definition.get("items", {}))


def resolve_type(class_property_definition: dict) -> str:
    """Resolves a class/property definition to a concrete type label.

    Returns "_Not Specified_" if undetermined. Arrays report their effective
    item type (a narrowing ``contains`` wins over ``items``); ``allOf`` resolves
    to its primary member; $ref/$refCurie are shown as the bare class name.
    """
    d = class_property_definition
    if not isinstance(d, dict):
        return "_Not Specified_"
    if d.get("type") == "array":
        return _array_item_type(d)
    if "type" in d:
        return d["type"]
    if "$ref" in d:
        return f":ref:`{_ref_label(d['$ref'])}`"
    if "$refCurie" in d:
        return f":ref:`{_ref_label(d['$refCurie'])}`"
    if "allOf" in d:
        for member in d["allOf"]:
            resolved = resolve_type(member)
            if resolved != "_Not Specified_":
                return resolved
        return "_Not Specified_"
    if "oneOf" in d or "anyOf" in d:
        kw = "anyOf" if "anyOf" in d else "oneOf"
        deprecated_types = d.get("deprecated", [])
        resolved_deprecated = []
        resolved_active = []
        for property_type in d[kw]:
            resolved_type = resolve_type(property_type)
            if property_type in deprecated_types:
                resolved_deprecated.append(resolved_type + " (deprecated)")
            else:
                resolved_active.append(resolved_type)
        return " | ".join(resolved_active + resolved_deprecated)
    return "_Not Specified_"


def resolve_cardinality(class_property_name: str, class_property_attributes: dict, class_definition: dict) -> str:
    """Resolves class property cardinality from YAML definition.

    :param class_property_name: class property name
    :param class_property_attributes: class property attributes
    :param class_definition: class definition
    """
    if class_property_name in class_definition.get("required", []):
        min_count = "1"
    elif class_property_name in class_definition.get("heritableRequired", []):
        min_count = "1"
    else:
        min_count = "0"
    if class_property_attributes.get("type") == "array":
        max_count = class_property_attributes.get("maxItems", "m")
        min_count = class_property_attributes.get("minItems", 0)
    else:
        max_count = "1"
    return f"{min_count}..{max_count}"


def get_ancestor_with_attributes(class_name: str, proc: YamlSchemaProcessor) -> str:
    """Returns the ancestor class of the class name

    :param class_name: class name
    :param proc: yaml schema processor
    """
    if proc.class_is_passthrough(class_name):
        raw_def, proc = proc.get_local_or_inherited_class(class_name, raw=True)
        ancestor = raw_def.get("inherits")
        return get_ancestor_with_attributes(ancestor, proc)
    return class_name


def add_ga4gh_digest(class_definition: dict, f: TextIOWrapper) -> None:
    """Add GA4GH Digest table

    Will only include this table if both ``prefix`` and ``inherent`` are provided

    :param class_definition: Model definition
    :param f: RST file
    """
    ga4gh_digest = class_definition.get("ga4gh", {})
    if ga4gh_digest:
        print(
            f"""
**GA4GH Digest**

.. list-table::
    :class: clean-wrap
    :header-rows: 1
    :align: left
    :widths: auto

    *  - Prefix
       - Inherent

    *  - {ga4gh_digest.get("prefix", None)}
       - {str(ga4gh_digest.get("inherent", []))}\n""",
            file=f,
        )


def resolve_flags(class_property_attributes: dict) -> str:
    """Add badges for flags (maturity and ordered property)

    :param class_property_attributes: Property attributes for a class
    :return: Output for flag badges
    """
    flags = ""
    maturity = class_property_attributes.get("maturity")

    if maturity is not None:
        background_color, maturity_code = MATURITY_MAPPING.get(maturity, (None, None))
        if background_color and maturity_code:
            title = f"{maturity.title()} Maturity Level"
            flags += f"""
                        .. raw:: html

                            <span style="background-color: #{background_color}; color: black; padding: 2px 6px; border: 1px solid black; border-radius: 3px; font-weight: bold; display: inline-block; margin-bottom: 5px;" title="{title}">{maturity_code}</span>"""

    ordered = class_property_attributes.get("ordered")
    ordered_code = ORDERED_MAPPING.get(ordered, None)

    if ordered_code is not None:
        title = "Ordered" if ordered else "Unordered"
        if not flags:
            flags += """
                        .. raw:: html\n"""

        flags += f"""
                            <span style="background-color: #B2DFEE; color: black; padding: 2px 6px; border: 1px solid black; border-radius: 3px; font-weight: bold; display: inline-block; margin-bottom: 5px;" title="{title}">{ordered_code}</span>"""
    return flags


def describe_composition_member(member: dict) -> str:
    """Human-readable RST for a single allOf/oneOf/anyOf member schema."""
    if any(key in member for key in ("$ref", "$refCurie", "oneOf", "anyOf")):
        return resolve_type(member)
    if "properties" in member:
        fields = ", ".join(f"``{name}``" for name in member["properties"])
        if fields:
            return f"an object constraining {fields}"
    return "an object with additional constraints"


def resolve_composition(class_definition: dict) -> str:
    """RST describing an allOf/oneOf/anyOf composed class.

    Returns an empty string when the class is not composed. Used to give
    composed classes (which have no property table of their own) a useful
    Information Model rather than a blank section.
    """
    for keyword, label in (("oneOf", "one of"), ("anyOf", "any of")):
        if keyword in class_definition:
            members = class_definition[keyword]
            lines = [f"This class must match **{label}** the following:\n"]
            lines += [f"* {describe_composition_member(m)}" for m in members]
            return "\n".join(lines) + "\n"
    return ""


def _class_registry(proc: YamlSchemaProcessor) -> dict:
    """Map class name -> processed definition, across this schema and its imports.

    Cached on the processor. Used to resolve the base class(es) an allOf-composed
    class refines, so their (already-flattened) properties can be overlaid.
    """
    cached = getattr(proc, "_y2t_registry", None)
    if cached is not None:
        return cached
    reg: dict = {}

    def collect(p: YamlSchemaProcessor, seen: set) -> None:
        if id(p) in seen:
            return
        seen.add(id(p))
        for name, defn in p.processed_schema.get(p.schema_def_keyword, {}).items():
            reg.setdefault(name, defn)
        for imported in p.imports.values():
            collect(imported, seen)

    collect(proc, set())
    proc._y2t_registry = reg
    return reg


def _ref_class_name(member: dict) -> str | None:
    """Extract the referenced class name from a $ref (path) or $refCurie member."""
    ref = member.get("$ref") or member.get("$refCurie")
    if not ref:
        return None
    frag = ref.split("#")[-1]  # drop any JSON-pointer fragment
    name = frag.rsplit("/", 1)[-1]  # last path segment
    return name.rsplit(":", 1)[-1] or None  # strip a CURIE namespace prefix


# Mutually-exclusive "what kind of thing is this" keywords. If a refinement
# supplies one, the base's others are cleared so the narrower type wins.
_TYPE_KEYS = ("type", "$ref", "$refCurie", "oneOf", "anyOf")


def _merge_property(base: dict, refinement: dict) -> dict:
    """Overlay a refinement onto a base property definition.

    If the refinement redefines the type (any of ``_TYPE_KEYS``), the base's
    type-signal keys are dropped first so the narrower type replaces the base's
    rather than colliding with it; otherwise base facets (e.g. ``type: array`` /
    ``items``) survive alongside the refinement (e.g. ``contains``/``minItems``).
    """
    merged = dict(base)
    if any(key in refinement for key in _TYPE_KEYS):
        for key in _TYPE_KEYS:
            merged.pop(key, None)
    merged.update(refinement)
    return merged


def flatten_allof(class_definition: dict, proc: YamlSchemaProcessor, _seen: frozenset = frozenset()):
    """Flatten an allOf-composed class to its effective property set.

    Overlays each referenced base class's properties (in order) with the local
    ``properties``. Returns (effective_properties, sorted_required). Local/added
    names win but keep the position they hold in the base (superclass-first
    ordering).

    A referenced base that is itself allOf-composed (has no flat top-level
    ``properties`` of its own -- e.g. a recipe/profile class composing another
    recipe/profile class, rather than a plain entity) is flattened recursively,
    so its own composed properties (and its base's, transitively) are picked up
    too, not silently dropped. ``_seen`` guards against a cyclic ``allOf`` chain.
    """
    registry = _class_registry(proc)
    effective: dict = {}
    required: set = set()
    for member in class_definition.get("allOf", []):
        base_name = _ref_class_name(member)
        if base_name and base_name in registry and base_name not in _seen:
            base = registry[base_name]
            if "allOf" in base:
                base_effective, base_required = flatten_allof(base, proc, _seen | {base_name})
            else:
                base_effective, base_required = base.get("properties", {}), base.get("required", [])
            for name, attribs in base_effective.items():
                effective.setdefault(name, attribs)
            required.update(base_required)
        if "properties" in member:
            for name, attribs in member["properties"].items():
                effective[name] = _merge_property(effective.get(name, {}), attribs)
            required.update(member.get("required", []))
    # fold in any properties declared directly on the class as well
    for name, attribs in class_definition.get("properties", {}).items():
        effective[name] = _merge_property(effective.get(name, {}), attribs)
    required.update(class_definition.get("required", []))
    return effective, sorted(required)


def _describe_schema_paths(attribs: dict, path: list) -> list:
    """Recursively walk a JSON-Schema-shaped dict (an allOf `if` condition,
    or a `then`/`else` consequence) to describe every leaf constraint it
    pins down.

    Yields ``(dotted.path, kind, description)`` triples: ``kind`` is
    ``"value"`` for a ``const``/``enum``/``pattern``/bounds/boolean-schema pin
    (rendered "must be") or ``"type"`` for a structural narrowing resolved via
    ``resolve_type`` (rendered "is narrowed to"). A property with its own
    nested ``properties`` recurses, extending the dotted path -- e.g.
    ``strength: {properties: {primaryCoding: {properties: {code:
    {const: strong}}}}}`` yields a single ``strength.primaryCoding.code``
    leaf, not a bare ``strength`` entry. A property schema'd as the JSON
    Schema boolean ``false`` (e.g. ``strength: false``, meaning "this
    property must not be present") is described rather than recursed into,
    since it has no ``properties`` of its own to walk.
    """
    results = []
    for name, member in attribs.get("properties", {}).items():
        new_path = path + [name]
        dotted = ".".join(new_path)
        if isinstance(member, bool):
            desc = "is not permitted" if member is False else "is permitted with any value"
            results.append((dotted, "raw", desc))
        elif "const" in member:
            results.append((dotted, "value", f"``{member['const']}``"))
        elif "enum" in member:
            values = ", ".join(f"``{v}``" for v in member["enum"])
            results.append((dotted, "value", f"one of: {values}"))
        elif "pattern" in member:
            results.append((dotted, "raw", f"must match the pattern ``{member['pattern']}``"))
        elif "properties" in member:
            results.extend(_describe_schema_paths(member, new_path))
        else:
            resolved = resolve_type(member)
            kind = "type"
            if resolved == "_Not Specified_":
                # resolve_type only recognizes type/$ref/$refCurie/allOf/
                # oneOf/anyOf -- describe whatever bound-style constraint
                # keywords *are* present instead of leaking its internal
                # "not specified" sentinel into rendered docs.
                kind = "value"
                resolved = _describe_bounds(member) or "further constrained (see source)"
            results.append((dotted, kind, resolved))
    return results


def _describe_bounds(member: dict) -> str:
    """Describe numeric/length/format constraint keywords resolve_type
    doesn't understand (it only recognizes type/$ref/$refCurie/allOf/oneOf/
    anyOf), joined into one clause. Empty string if none are present."""
    parts = []
    for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength"):
        if key in member:
            parts.append(f"{key}: ``{member[key]}``")
    if "format" in member:
        parts.append(f"format: ``{member['format']}``")
    return "; ".join(parts)


def _condition_leaf(if_schema: dict) -> tuple | None:
    """If ``if_schema`` pins down exactly one property to a concrete value
    (``const``/``enum``/a bounds-style keyword), return ``(dotted.path,
    value_description)``. Returns None for anything that doesn't reduce to
    that single "property equals value" shape -- a compound (multi-property)
    condition, or one resolved via ``resolve_type``/a pattern/boolean-schema
    pin -- which reads more naturally as prose than as a table row.
    """
    leaves = _describe_schema_paths(if_schema, [])
    if len(leaves) != 1:
        return None
    path, kind, desc = leaves[0]
    if kind != "value":
        return None
    return path, desc


def _must_phrase(kind: str, desc: str) -> str:
    """Render a then/else leaf's (kind, description) as the verb phrase for
    the **must...** table column, e.g. "have value ``X``", "match the
    pattern ``Y``", "not be provided"."""
    if kind == "raw":
        if desc == "is not permitted":
            return "not be provided"
        if desc == "is permitted with any value":
            return "be provided, with any value"
        # e.g. "must match the pattern ``X``" -> "match the pattern ``X``"
        return desc.removeprefix("must ")
    if kind == "value":
        return f"have value {desc}" if desc.startswith("``") else f"have {desc}"
    # kind == "type": a structural narrowing resolved via resolve_type.
    if " | " in desc:
        return f"be one of: {desc.replace(' | ', ', ')}"
    return f"be narrowed to: {desc}"


def _consequence_rows(branch: dict) -> list:
    """Table rows -- (dotted.path, must_phrase) -- for a then/else branch:
    one per leaf constraint, plus one per ``required`` entry that has no
    constraint of its own (bare presence, e.g. ``required: [strength]`` with
    no narrowing on ``strength`` itself)."""
    rows = []
    leaves = _describe_schema_paths(branch, [])
    covered = [path for path, _kind, _desc in leaves]
    for path, kind, desc in leaves:
        rows.append((path, _must_phrase(kind, desc)))
    for name in branch.get("required", []):
        if not any(p == name or p.startswith(name + ".") for p in covered):
            rows.append((name, "be provided"))
    return rows


def _render_conditional_prose(f, member: dict) -> None:
    """Render one allOf `if`/`then`/`else` member as prose bullets -- the
    general fallback for a condition/consequence shape that doesn't reduce
    to a single "property equals value" table row (see _condition_leaf)."""
    condition = _describe_schema_paths(member["if"], [])
    cond_text = " and ".join(f"``{p}`` {d}" if k == "raw" else f"``{p}`` is {d}" for p, k, d in condition)
    print(f"If {cond_text or 'the condition below holds'}, then:\n", file=f)
    for branch_key, lede in (("then", None), ("else", "Otherwise")):
        branch = member.get(branch_key)
        if not branch:
            continue
        if lede:
            print(f"\n{lede}:\n", file=f)
        for path, kind, desc in _describe_schema_paths(branch, []):
            if kind == "raw":
                # desc is a complete, self-contained clause (its own verb).
                print(f"* ``{path}`` {desc}", file=f)
                continue
            verb = "must be" if kind == "value" else "is narrowed to"
            print(f"* ``{path}`` {verb}: {desc}", file=f)
        if branch.get("required"):
            req = ", ".join(f"``{r}``" for r in branch["required"])
            print(f"* Required: {req}", file=f)
    print(file=f)


def render_conditional_constraints(f, class_definition: dict) -> None:
    """Render allOf `if`/`then`/`else` members -- business-rule-style
    conditional narrowing (e.g. AMP/ASCO/CAP tier-dependent constraints) --
    under a **Conditional Constraints** heading.

    `flatten_allof` only understands a base `$ref` or a flat `properties`
    member; an `if`/`then` member has neither at its own top level (the
    condition/consequence properties are nested one level deeper), so it's
    silently skipped by the main Information Model table. Without this,
    conditional rules were entirely invisible in the rendered docs.

    A member whose condition reduces to a single "property equals value" pin
    (the common case -- e.g. AMP/ASCO/CAP's tier-/methodType-keyed rules) is
    collected into one **If property... / has value... / then property... /
    must...** table, one row per consequence -- flatter and easier to scan
    than prose when a class has many such branches. Anything that doesn't
    reduce that way (a compound condition, an `else`, or a condition pinned
    via a structural/pattern/boolean-schema constraint rather than a plain
    value) falls back to the previous prose rendering.
    """
    branches = [m for m in class_definition.get("allOf", []) if "if" in m]
    if not branches:
        return
    table_rows = []
    prose_members = []
    for member in branches:
        leaf = None if "else" in member else _condition_leaf(member["if"])
        rows = _consequence_rows(member.get("then", {})) if leaf else []
        if leaf is None or not rows:
            prose_members.append(member)
            continue
        if_path, has_value = leaf
        table_rows.extend((if_path, has_value, then_path, must) for then_path, must in rows)
    print("\n**Conditional Constraints**\n", file=f)
    if table_rows:
        print(
            """.. list-table::
   :class: clean-wrap
   :header-rows: 1
   :align: left
   :widths: auto

   *  - If property...
      - has value...
      - then property...
      - must...""",
            file=f,
        )
        for if_path, has_value, then_path, must in table_rows:
            row = f"""\
   *  - ``{if_path}``
      - {has_value}
      - ``{then_path}``
      - {must}"""
            print(row, file=f)
        print(file=f)
    for member in prose_members:
        _render_conditional_prose(f, member)


def render_information_model(f, properties: dict, required: list, note: str = "") -> None:
    """Render an Information Model list-table for a property set.

    Shared by ordinary classes and allOf-composed classes.
    """
    if not properties:
        return
    print(
        f"""
{note}
.. list-table::
   :class: clean-wrap
   :header-rows: 1
   :align: left
   :widths: auto

   *  - Field
      - Flags
      - Type
      - Limits
      - Description""",
        file=f,
    )
    synthetic = {"required": required}
    for name, attribs in properties.items():
        row = f"""\
   *  - {name}
      - {resolve_flags(attribs)}
      - {resolve_type(attribs)}
      - {resolve_cardinality(name, attribs, synthetic)}
      - {attribs.get("description", "")}"""
        print("\n".join(line.rstrip() for line in row.splitlines()), file=f)


def _folder_processors(proc: YamlSchemaProcessor) -> list:
    """All source processors in the same folder as ``proc`` (including it),
    scoped to what ``proc`` itself renders.

    A non-profile folder's docs cover every non-profile ``*-source.yaml``
    beside it (e.g. cat-vrs + recipes). A profile source is scoped to just
    itself: it renders its own classes into its own ``def/XXX``, plus the rest
    of its closure into the shared top-level ``def/`` (see ``main``) -- it
    never renders a sibling profile's own classes.
    """
    if getattr(proc, "sub_namespace", None):
        return [proc]
    procs = {proc.schema_fp.resolve(): proc}
    for src in sorted(proc.schema_fp.parent.glob("*-source.yaml")):
        if src.name.endswith(YamlSchemaProcessor._PROFILE_SUFFIX):
            continue
        key = src.resolve()
        if key not in procs:
            procs[key] = YamlSchemaProcessor(src)
    return list(procs.values())


def _folder_xref_processors(proc: YamlSchemaProcessor) -> list:
    """Every ``*-source.yaml`` in ``proc``'s folder -- base sources AND every
    profile alike -- for computing complete Used in:/Subclasses: cross
    references.

    Unlike ``_folder_processors`` (which scopes *rendering* to what a given
    source's ``y2t`` invocation should write), cross-references must span the
    whole folder regardless of who's rendering: a shared top-level ``def/``
    file (e.g. an imported class several profiles narrow) needs every
    referencing class counted, not just the current invocation's own closure
    -- otherwise whichever source's ``y2t`` run writes the file last silently
    drops the other sources' references.
    """
    procs = {proc.schema_fp.resolve(): proc}
    for src in sorted(proc.schema_fp.parent.glob("*-source.yaml")):
        key = src.resolve()
        if key not in procs:
            procs[key] = YamlSchemaProcessor(src)
    return list(procs.values())


def _closure_owners(processors: list) -> dict:
    """Map class name -> owning processor across the processors and their
    imports (recursively). First definition wins; class names are unique."""
    owners: dict = {}

    def walk(p: YamlSchemaProcessor, seen: set) -> None:
        if id(p) in seen:
            return
        seen.add(id(p))
        for name in p.processed_schema.get(p.schema_def_keyword, {}):
            owners.setdefault(name, p)
        for imported in p.imports.values():
            walk(imported, seen)

    seen: set = set()
    for p in processors:
        walk(p, seen)
    return owners


def _collect_ref_names(node, out: set) -> None:
    """Collect referenced class names from every $ref/$refCurie under ``node``."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("$ref", "$refCurie") and isinstance(value, str):
                out.add(_ref_label(value))
            else:
                _collect_ref_names(value, out)
    elif isinstance(node, list):
        for item in node:
            _collect_ref_names(item, out)


def _inherits_from(descendant: str, ancestor: str, parent: dict) -> bool:
    """True if ``descendant`` reaches ``ancestor`` by walking ``inherits``
    upward zero or more times (i.e. ``ancestor`` is ``descendant``'s direct or
    transitive superclass)."""
    seen: set = set()
    current = parent.get(descendant)
    while current is not None and current not in seen:
        if current == ancestor:
            return True
        seen.add(current)
        current = parent.get(current)
    return False


def build_cross_references(owners: dict):
    """Compute (used_in, subclasses) over the class closure in ``owners``.

    ``subclasses[X]`` = classes whose ``inherits`` resolves to X.
    ``used_in[X]``   = classes that reference X via $ref/$refCurie (property or
    composition), excluding the abstract-parent-enumerates-its-subclass mirror
    -- direct (a class's own oneOf/anyOf lists its immediate children) or
    transitive (a sealed class's auto-derived oneOf lists concrete
    descendants reached through an abstract intermediate; see
    _resolve_sealed_classes). Only targets present in the closure are
    recorded, so every :ref: resolves.
    """
    parent: dict = {}
    for name, proc in owners.items():
        raw = proc.raw_schema.get(proc.schema_def_keyword, {}).get(name, {})
        inherits = raw.get("inherits")
        if isinstance(inherits, str):
            parent[name] = inherits.rsplit(":", 1)[-1]
    subclasses: dict = {}
    for child, par in parent.items():
        if par in owners:
            subclasses.setdefault(par, set()).add(child)
    used_in: dict = {}
    for name, proc in owners.items():
        refs: set = set()
        _collect_ref_names(proc.processed_schema[proc.schema_def_keyword][name], refs)
        for target in refs:
            if target == name or target not in owners:
                continue
            if _inherits_from(target, name, parent):  # skip subclass-enumeration mirror
                continue
            used_in.setdefault(target, set()).add(name)
    return used_in, subclasses


def _print_xrefs(f, proc: YamlSchemaProcessor, class_name: str, used_in: dict, subclasses: dict) -> None:
    """Append 'Inherits:', 'Composes:', 'Subclasses:', and 'Used in:' :ref:
    lists for a class. 'Inherits:' shows the class's own direct 'inherits'
    target (if any); 'Composes:' is the allOf-composition equivalent -- the
    base class(es) an allOf-composed class builds on via $ref/$refCurie,
    which 'inherits:' doesn't capture since composition is a separate
    mechanism (see flatten_allof). Both precede 'Subclasses:' (the mirror --
    a class's direct children), regardless of the class's shape (passthrough,
    primitive, or a normal properties/composition class all call this)."""
    inherits = proc.raw_defs[class_name].get("inherits")
    if isinstance(inherits, str):
        print("\n**Inherits:** :ref:`" + _ref_label(inherits) + "`", file=f)
    composes = []
    for member in proc.raw_defs[class_name].get("allOf", []):
        base_name = _ref_class_name(member)
        if base_name and base_name not in composes:
            composes.append(base_name)
    if composes:
        print("\n**Composes:** " + ", ".join(f":ref:`{c}`" for c in composes), file=f)
    subs = sorted(subclasses.get(class_name, []))
    if subs:
        print("\n**Subclasses:** " + ", ".join(f":ref:`{s}`" for s in subs), file=f)
    uses = sorted(used_in.get(class_name, []))
    if uses:
        print("\n**Used in:** " + ", ".join(f":ref:`{u}`" for u in uses), file=f)


def render_class(
    proc: YamlSchemaProcessor, class_name: str, class_definition: dict, def_fp, used_in: dict, subclasses: dict
) -> None:
    """Render one class's .rst into ``def_fp`` using its owning processor."""
    with open(def_fp / (class_name + ".rst"), "w") as f:
        maturity = class_definition.get("maturity", "")
        template = env.get_template("maturity")
        if maturity == "draft":
            print(template.render(info="warning", label="Draft", modifier="significantly"), file=f)
            print(file=f)
        elif maturity == "trial use":
            print(template.render(info="note", label="Trial Use", modifier=""), file=f)
            print(file=f)
        if proc.class_is_abstract(class_name):
            print(
                "**Abstract Class** — not instantiated directly; concrete subclasses inherit its attributes.\n",
                file=f,
            )
            if class_definition.get("sealed", False):
                print(
                    f"**Sealed** — {class_name} has a closed, exhaustive set of "
                    "concrete subclasses; every one is listed below. No other "
                    "subclass is permitted, and a conforming instance must be "
                    "exactly one of these types.\n",
                    file=f,
                )
        print("**Computational Definition**\n", file=f)
        print(class_definition["description"], file=f)
        if proc.class_is_passthrough(class_name):
            composition = resolve_composition(class_definition)
            if composition:
                print("\n**Information Model**\n", file=f)
                print(composition, file=f)
            _print_xrefs(f, proc, class_name, used_in, subclasses)
            return
        if "heritableProperties" in class_definition:
            p = "heritableProperties"
        elif "properties" in class_definition:
            p = "properties"
        else:
            # No top-level property table (e.g. an allOf-composed recipe whose
            # members live under 'allOf', now that empty 'properties: {}' is
            # omitted). A composed class renders from its composition below; a
            # primitive just gets cross-references; anything else has nothing
            # to render and is a bug.
            p = None
            if not any(k in class_definition for k in ("allOf", "anyOf", "oneOf")):
                if proc.class_is_primitive(class_name):
                    _print_xrefs(f, proc, class_name, used_in, subclasses)
                    return
                raise ValueError(class_name, class_definition)
        ancestor = proc.raw_defs[class_name].get("inherits")
        if ancestor:
            ancestor = get_ancestor_with_attributes(ancestor, proc)
            inheritance = f"Some {class_name} attributes are inherited from :ref:`{ancestor}`.\n"
        else:
            inheritance = ""

        # Abstract classes never carry a GA4GH Digest section: they are not
        # instantiated, so the digest (prefix/inherent) applies only to the
        # concrete subclasses that inherit it.
        if not proc.class_is_abstract(class_name):
            add_ga4gh_digest(class_definition, f)

        print("\n**Information Model**", file=f)
        if "allOf" in class_definition:
            # allOf = composition: show the effective (flattened) property table.
            effective, required = flatten_allof(class_definition, proc)
            if effective:
                render_information_model(f, effective, required)
            else:
                composition = resolve_composition(class_definition)
                if composition:
                    print("\n" + composition, file=f)
        elif p is not None:
            render_information_model(f, class_definition[p], class_definition.get("required", []), inheritance)
        render_conditional_constraints(f, class_definition)
        composition = resolve_composition(class_definition)
        if composition:
            print("\n" + composition, file=f)
        _print_xrefs(f, proc, class_name, used_in, subclasses)


def main(proc_schema: YamlSchemaProcessor) -> None:
    """Generate .rst for ``proc_schema``'s transitive import closure, splitting
    where each class lands: ``proc_schema``'s own classes (mirroring ``json/``)
    go into ``proc_schema.def_fp``; every other class reached through the
    closure (all ``*-source.yaml`` beside ``proc_schema`` plus their imports,
    recursively) is rendered into the shared top-level ``def/`` -- ``def_fp``'s
    parent for a profile source, or ``def_fp`` itself for a non-profile one.

    Multiple sources in a folder can pull the same imported class into their
    closure; each render is identical regardless of which source triggers it,
    so writing them all into the same shared top-level dir is a no-op union,
    not a conflict. Used in:/Subclasses: labels are computed over the whole
    folder (see ``_folder_xref_processors``), not just this invocation's own
    closure, so they stay complete and build-order-independent even on shared
    files multiple sources render into.
    """
    owners = _closure_owners(_folder_processors(proc_schema))
    xref_owners = _closure_owners(_folder_xref_processors(proc_schema))
    used_in, subclasses = build_cross_references(xref_owners)
    own_def_fp = proc_schema.def_fp
    top_def_fp = own_def_fp.parent if proc_schema.sub_namespace else own_def_fp
    os.makedirs(own_def_fp, exist_ok=True)
    os.makedirs(top_def_fp, exist_ok=True)
    own_defs = proc_schema.processed_schema.get(proc_schema.schema_def_keyword, {})
    for class_name, owner in owners.items():
        class_definition = owner.processed_schema[owner.schema_def_keyword][class_name]
        target_fp = own_def_fp if class_name in own_defs else top_def_fp
        render_class(owner, class_name, class_definition, target_fp, used_in, subclasses)

    # Normalize generated RST: strip trailing whitespace on every line and end
    # each file with a single newline, so output matches what pre-commit produces
    # and re-running the generator never dirties the working tree.
    for fp in {own_def_fp, top_def_fp}:
        for rst_file in fp.glob("*.rst"):
            text = rst_file.read_text()
            rst_file.write_text("\n".join(line.rstrip() for line in text.splitlines()).rstrip("\n") + "\n")


def cli():
    source_file = pathlib.Path(sys.argv[1])
    p = YamlSchemaProcessor(source_file)
    os.makedirs(p.def_fp, exist_ok=True)
    if p.defs is None:
        exit(0)
    main(p)


if __name__ == "__main__":
    cli()
