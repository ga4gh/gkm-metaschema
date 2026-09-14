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


def flatten_allof(class_definition: dict, proc: YamlSchemaProcessor):
    """Flatten an allOf-composed class to its effective property set.

    Overlays each referenced base class's properties (in order) with the local
    ``properties``. Returns (effective_properties, sorted_required). Local/added
    names win but keep the position they hold in the base (superclass-first
    ordering).
    """
    registry = _class_registry(proc)
    effective: dict = {}
    required: set = set()
    for member in class_definition.get("allOf", []):
        base_name = _ref_class_name(member)
        if base_name and base_name in registry:
            base = registry[base_name]
            for name, attribs in base.get("properties", {}).items():
                effective.setdefault(name, attribs)
            required.update(base.get("required", []))
        if "properties" in member:
            for name, attribs in member["properties"].items():
                effective[name] = _merge_property(effective.get(name, {}), attribs)
            required.update(member.get("required", []))
    # fold in any properties declared directly on the class as well
    for name, attribs in class_definition.get("properties", {}).items():
        effective[name] = _merge_property(effective.get(name, {}), attribs)
    required.update(class_definition.get("required", []))
    return effective, sorted(required)


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
    """All source processors in the same folder as ``proc`` (including it).

    A folder's docs cover every ``*-source.yaml`` beside it (e.g. cat-vrs +
    recipes), so cross-references are computed over the whole folder.
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


def build_cross_references(owners: dict):
    """Compute (used_in, subclasses) over the class closure in ``owners``.

    ``subclasses[X]`` = classes whose ``inherits`` resolves to X.
    ``used_in[X]``   = classes that reference X via $ref/$refCurie (property or
    composition), excluding the abstract-parent-enumerates-its-subclass mirror.
    Only targets present in the closure are recorded, so every :ref: resolves.
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
            if parent.get(target) == name:  # skip subclass-enumeration mirror
                continue
            used_in.setdefault(target, set()).add(name)
    return used_in, subclasses


def _print_xrefs(f, class_name: str, used_in: dict, subclasses: dict) -> None:
    """Append 'Subclasses:' and 'Used in:' :ref: lists for a class."""
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
        print("**Computational Definition**\n", file=f)
        print(class_definition["description"], file=f)
        if proc.class_is_passthrough(class_name):
            composition = resolve_composition(class_definition)
            if composition:
                print("\n**Information Model**\n", file=f)
                print(composition, file=f)
            _print_xrefs(f, class_name, used_in, subclasses)
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
                    _print_xrefs(f, class_name, used_in, subclasses)
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
        composition = resolve_composition(class_definition)
        if composition:
            print("\n" + composition, file=f)
        _print_xrefs(f, class_name, used_in, subclasses)


def main(proc_schema: YamlSchemaProcessor) -> None:
    """Generate .rst for every class in the folder's import closure.

    All ``*-source.yaml`` beside ``proc_schema``, plus their imports (recursively
    across levels), are rendered into ``proc_schema.def_fp`` so the folder is
    self-contained and its 'Used in:' / 'Subclasses:' lists are accurate from
    that folder's perspective.
    """
    processors = _folder_processors(proc_schema)
    owners = _closure_owners(processors)
    used_in, subclasses = build_cross_references(owners)
    def_fp = proc_schema.def_fp
    os.makedirs(def_fp, exist_ok=True)
    for class_name, owner in owners.items():
        class_definition = owner.processed_schema[owner.schema_def_keyword][class_name]
        render_class(owner, class_name, class_definition, def_fp, used_in, subclasses)

    # Normalize generated RST: strip trailing whitespace on every line and end
    # each file with a single newline, so output matches what pre-commit produces
    # and re-running the generator never dirties the working tree.
    for rst_file in def_fp.glob("*.rst"):
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
