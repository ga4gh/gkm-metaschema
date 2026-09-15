#!/usr/bin/env python3
"""convert yaml on stdin to json on stdout"""

import copy
import json
import re
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

import yaml

SCHEMA_DEF_KEYWORD_BY_VERSION = {
    "https://json-schema.org/draft-07/schema": "definitions",
    "https://json-schema.org/draft/2020-12/schema": "$defs",
}


ref_re = re.compile(r":ref:`(.*?)(\s?<.*>)?`")
# Non-greedy on BOTH the label and the URL: a description may contain several
# `text <url>`_ links, and a greedy URL group would swallow everything from the
# first '<' to the last '>`_', collapsing all links into one broken match.
link_re = re.compile(r"`(.*?)\s?\<(.*?)\>`_")
curie_re = re.compile(r"(\S+):(\S+)")
defs_re = re.compile(r"#/(\$defs|definitions)/.*")

maturity_levels = {"deprecated": 0, "draft": 1, "trial use": 2, "normative": 3}


class YamlSchemaProcessor:
    def __init__(self, schema_fp, root_fp=None):
        # Resolved so schema_fp is canonical everywhere it's compared (e.g. a
        # diamond import reaching the same file via two different relative
        # routes must compare equal -- see _register_merge_import).
        self.schema_fp = Path(schema_fp).resolve()
        self.imported = root_fp is not None
        self.root_schema_fp = root_fp
        self.raw_schema = self.load_schema(schema_fp)
        self.id = self.raw_schema["$id"]
        self.yaml_key = self.raw_schema.get("yaml-target", "yaml")
        self.json_key = self.raw_schema.get("json-target", "json")
        self.defs_key = self.raw_schema.get("def-target", "def")
        # A ``XXX-profile-source.yaml`` file contributes ``XXX`` as a sub-namespace:
        # its outputs live under ``<parent>/{yaml,json,def}/XXX`` (nested inside the
        # shared output dirs, not as sibling top-level folders) and every class $id
        # is ``.../<version>/json/XXX/<Class>``. XXX is taken from the filename and
        # must match the $id's final path segment.
        self.sub_namespace = self._profile_sub_namespace()
        parent = self.schema_fp.parent
        # schema_root_name = str(self.schema_fp.stem)[:-7]  # removes "-source"

        def _out_fp(key):
            return parent / key / self.sub_namespace if self.sub_namespace else parent / key

        self.yaml_fp = _out_fp(self.yaml_key)
        self.json_fp = _out_fp(self.json_key)
        self.def_fp = _out_fp(self.defs_key)
        # self.def_fp = self.schema_fp.parent / self.raw_schema.get('def-target', f'def/{schema_root_name}')
        self.namespaces = self.raw_schema.get("namespaces", [])
        self.schema_def_keyword = SCHEMA_DEF_KEYWORD_BY_VERSION[self.raw_schema["$schema"]]
        self.raw_defs = self.raw_schema.get(self.schema_def_keyword, None)
        self.imports = {}
        self.import_dependencies()
        self.strict = self.raw_schema.get("strict", False)
        self.enforce_ordered = self.raw_schema.get("enforce_ordered", self.strict)
        self._init_from_raw()

    _PROFILE_SUFFIX = "-profile-source.yaml"

    def _profile_sub_namespace(self):
        """Sub-namespace ``XXX`` for a ``XXX-profile-source.yaml`` file, else None.

        Validates that the ``XXX`` taken from the filename matches the ``XXX`` in
        this schema's own ``$id`` (its final path segment); raises otherwise.
        """
        name = self.schema_fp.name
        if not name.endswith(self._PROFILE_SUFFIX):
            return None
        xxx_file = name[: -len(self._PROFILE_SUFFIX)]
        if not xxx_file:
            return None
        id_last = urlparse(self.id).path.rstrip("/").rsplit("/", 1)[-1]
        xxx_id = id_last[: -len(self._PROFILE_SUFFIX)] if id_last.endswith(self._PROFILE_SUFFIX) else id_last
        if xxx_id != xxx_file:
            raise ValueError(
                f"Profile source '{name}' has a sub-namespace '{xxx_file}' (from the "
                f"filename), but its $id's final path segment is '{id_last}', which "
                f"gives '{xxx_id}'. For an 'XXX-profile-source.yaml' file the 'XXX' "
                f"must be identical in the filename and the $id. Fix: set the $id's "
                f"last path segment to '{name}' (e.g. "
                f"'.../<version>/{name}')."
            )
        return xxx_file

    def _init_from_raw(self):
        self.has_children_urls = {}
        self.has_children = {}
        self.build_inheritance_dicts()
        self.has_protected_members = defaultdict(set)
        self.processed_schema = copy.deepcopy(self.raw_schema)
        self.defs = self.processed_schema.get(self.schema_def_keyword, None)
        self.processed_classes = set()
        self.process_schema()
        self.check_processed_schema()
        self.for_js = copy.deepcopy(self.processed_schema)
        self.clean_for_js()

    def build_inheritance_dicts(self):
        # For all classes:
        #   If an abstract class, register oneOf/anyOf enumerations
        #   If it inherits from a class, register the inheritance
        for cls, cls_def in self.raw_defs.items():
            cls_url = f"#/{self.schema_def_keyword}/{cls}"
            if self.class_is_container(cls):
                maps_to_urls = self.has_children_urls.get(cls_url, set())
                maps_to = self.has_children.get(cls, set())
                if "oneOf" in cls_def:
                    records = cls_def["oneOf"]
                elif "anyOf" in cls_def:
                    records = cls_def["anyOf"]
                elif "allOf" in cls_def:
                    records = cls_def["allOf"]
                else:
                    records = [{"$ref": cls_def["$ref"]}]
                for record in records:
                    if not isinstance(record, dict):
                        continue

                    if "$ref" in record:
                        mapped = record["$ref"]
                    elif "$refCurie" in record:
                        mapped = self.resolve_curie(record["$refCurie"])
                    maps_to_urls.add(mapped)
                    maps_to.add(mapped.split("/")[-1])
                self.has_children_urls[cls_url] = maps_to_urls
                self.has_children[cls] = maps_to
            if "inherits" in cls_def:
                target = cls_def["inherits"]
                if ":" in target:
                    continue  # Ignore mappings from definitions in other sources
                target_url = f"#/{self.schema_def_keyword}/{target}"
                maps_to_urls = self.has_children_urls.get(target_url, set())
                maps_to = self.has_children.get(target, set())
                maps_to_urls.add(cls_url)
                maps_to.add(cls)
                self.has_children_urls[target_url] = maps_to_urls
                self.has_children[target] = maps_to

    def get_all_descendants(self, cls):
        out = set()
        for descendant in self.has_children.get(cls, []):
            out.add(descendant)
            out.update(self.get_all_descendants(descendant))
        return out

    def merge_imported(self):
        # register all import namespaces and create process order
        # note: relying on max_recursion_depth errors and not checking for cyclic imports
        self.import_locations = {}
        self.import_processors = {}
        self.import_process_order = []
        self._register_merge_import(self)

        # check that all classes defined in imports are unique
        defined_classes = self.processed_classes
        for key in self.import_process_order:
            other = self.import_processors[key]
            assert len(defined_classes & other.processed_classes) == 0
            defined_classes.update(other.processed_classes)

        # Every curie prefix declared anywhere in the merged content -- by
        # this source itself, or by any transitively-imported source -- must
        # now resolve locally, since everything lives in one flat document
        # after the merge. Curie prefixes (e.g. "gkm.core") are a distinct
        # namespace from import dependency names (e.g. "gkm-core"), so this
        # is keyed by ``namespaces:`` keys, not ``import_process_order``.
        for ns in self.raw_schema.get("namespaces", {}):
            self.namespaces[ns] = f"#/{self.schema_def_keyword}/"
        for key in self.import_process_order:
            other = self.import_processors[key]
            for ns in other.raw_schema.get("namespaces", {}):
                self.namespaces[ns] = f"#/{self.schema_def_keyword}/"
            self.raw_defs.update(other.raw_defs)

        # revise all class.inherits attributes from CURIE to local defs
        for cls in defined_classes:
            cls_inherits_prop = self.raw_defs[cls].get("inherits", "")
            if curie_re.match(cls_inherits_prop):
                self.raw_defs[cls]["inherits"] = cls_inherits_prop.split(":")[1]

            # check all class.properties match expected definitions style
            self.raw_defs[cls] = self._check_local_defs_property(self.raw_defs[cls])

        # clear imports
        self.imports = {}

        # update title
        self.raw_schema["title"] = self.raw_schema["title"] + "-Merged-Imports"

        # reprocess raw_schema
        self.raw_defs = self.raw_schema.get(self.schema_def_keyword, None)
        self._init_from_raw()

    def _check_local_defs_property(self, obj):
        try:
            for k, v in obj.items():
                if isinstance(v, dict):
                    obj[k] = self._check_local_defs_property(v)
                elif isinstance(v, list):
                    l = []  # noqa: E741
                    for element in v:
                        l.append(self._check_local_defs_property(element))
                    obj[k] = l
                elif isinstance(v, str) and k == "$ref":
                    match = defs_re.match(v)
                    assert match, v
                    if match.group(1) != self.schema_def_keyword:
                        obj[k] = re.sub(re.escape(match.group(1)), self.schema_def_keyword, v)
        except AttributeError:
            return obj
        return obj

    def _register_merge_import(self, proc, seen=None):
        # ``seen`` memoizes which imported files have already had their own
        # import subtree walked, keyed by resolved schema_fp. A diamond (e.g.
        # cat-vrs importing both gkm-core directly and vrs, which itself
        # imports gkm-core) would otherwise re-descend into the same file's
        # imports once per distinct path that reaches it -- redundant, and
        # exponential on deeper graphs.
        if seen is None:
            seen = set()
        for name, other in proc.imports.items():
            if other.schema_fp not in seen:
                seen.add(other.schema_fp)
                self._register_merge_import(other, seen)
            if name in self.import_locations:
                # check that all imports from imported point to same locations
                if self.import_locations[name] != other.schema_fp:
                    raise ValueError(
                        f"Import name {name!r} resolves to two different files: "
                        f"{self.import_locations[name]} and {other.schema_fp}. Fix: use the same import "
                        f"path for {name!r} everywhere it's imported."
                    )
            else:
                self.import_locations[name] = other.schema_fp
                self.import_processors[name] = other
                self.import_process_order.append(name)
        return

    @staticmethod
    def load_schema(schema_fp):
        with open(schema_fp) as f:
            schema = yaml.load(f, Loader=yaml.SafeLoader)
        return schema

    def import_dependencies(self):
        for dependency in self.raw_schema.get("imports", []):
            fp = Path(self.raw_schema["imports"][dependency])
            if not fp.is_absolute():
                base_path = self.schema_fp.parent
                fp = base_path.joinpath(fp)
            # Resolve so the same underlying file reached via two different
            # relative routes (e.g. a diamond import) compares equal by path
            # rather than by the literal (unnormalized) string used to reach it.
            fp = fp.resolve()
            if self.imported:
                root_fp = self.root_schema_fp
            else:
                root_fp = self.schema_fp
            self.imports[dependency] = YamlSchemaProcessor(fp, root_fp=root_fp)

    def process_schema(self):
        if self.defs is None:
            return

        for schema_class in self.defs:
            self.process_schema_class(schema_class)

    def check_processed_schema(self):
        for cls in self.processed_classes:
            cls_def = self.defs[cls]
            if "inherits" in cls_def:
                inherited_cls_name = cls_def["inherits"]
                if ":" in inherited_cls_name:
                    namespace, inherited_cls_split_name = inherited_cls_name.split(":")
                    inherited_cls_def = self.imports[namespace].defs[inherited_cls_split_name]
                else:
                    inherited_cls_def = self.defs[inherited_cls_name]
                if "maturity" not in cls_def:
                    raise ValueError(
                        f"Class '{cls}' is missing the required 'maturity' field. "
                        f"Fix: add 'maturity' to '{cls}' with one of "
                        f"{sorted(maturity_levels)}."
                    )
                if "maturity" not in inherited_cls_def:
                    raise ValueError(
                        f"Parent class '{inherited_cls_name}' (inherited by '{cls}') "
                        "is missing the required 'maturity' field. Fix: add "
                        f"'maturity' to '{inherited_cls_name}'."
                    )
                if maturity_levels[inherited_cls_def["maturity"]] < maturity_levels[cls_def["maturity"]]:
                    raise ValueError(
                        f"Class '{cls}' has maturity '{cls_def['maturity']}', which "
                        f"is more mature than its parent '{inherited_cls_name}' "
                        f"('{inherited_cls_def['maturity']}'). A subclass may not be "
                        "more mature than the class it inherits from. Fix: lower the "
                        f"maturity of '{cls}' to at most '{inherited_cls_def['maturity']}', "
                        f"or raise the maturity of '{inherited_cls_name}'."
                    )

    def class_is_abstract(self, schema_class):
        schema_class_def, _ = self.get_local_or_inherited_class(schema_class, raw=True)
        return bool(schema_class_def.get("abstract", False))

    def class_is_container(self, schema_class):
        cls_def, _ = self.get_local_or_inherited_class(schema_class, raw=True)
        return self.class_is_abstract(schema_class) and ("oneOf" in cls_def or "anyOf" in cls_def or "allOf" in cls_def)

    def class_is_protected(self, schema_class):
        schema_class_def, _ = self.get_local_or_inherited_class(schema_class, raw=True)
        return "protectedClassOf" in schema_class_def

    def class_is_ga4gh_identifiable(self, schema_class):
        schema_class_def, _ = self.get_local_or_inherited_class(schema_class, raw=True)
        return "ga4gh" in schema_class_def and "prefix" in schema_class_def["ga4gh"]

    def class_is_passthrough(self, schema_class):
        if not self.class_is_abstract(schema_class):
            return False
        raw_class_definition, _ = self.get_local_or_inherited_class(schema_class, raw=True)
        if "properties" not in raw_class_definition and raw_class_definition.get("inherits", False):
            return True
        return False

    def class_is_primitive(self, schema_class):
        schema_class_def, _ = self.get_local_or_inherited_class(schema_class, raw=True)
        schema_class_type = schema_class_def.get("type", "abstract")
        if schema_class_type not in ["abstract", "object"]:
            return True
        return False

    def class_is_subclass(self, schema_class, parent_class):
        schema_class_fragment = f"#/{self.schema_def_keyword}/{schema_class}"
        parent_class_fragment = f"#/{self.schema_def_keyword}/{parent_class}"
        children = self.concretize_class_ref(parent_class_fragment)
        return schema_class_fragment in children

    def js_json_dump(self, stream):
        json.dump(self.for_js, stream, indent=3, sort_keys=False)

    def js_yaml_dump(self, stream):
        yaml.dump(self.for_js, stream, sort_keys=False)

    def resolve_curie(self, curie):
        if ":" not in curie:
            raise ValueError(
                f"CURIE reference {curie!r} is not of the form '<namespace>:"
                "<identifier>'. Fix: use a declared namespace prefix, e.g. "
                "'vrs:Variation'."
            )
        namespace, identifier = curie.split(":")
        if namespace not in self.namespaces:
            known = sorted(self.namespaces) or ["<none declared>"]
            raise ValueError(
                f"CURIE reference {curie!r} uses the undeclared namespace "
                f"'{namespace}'. Known namespaces: {known}. Fix: add '{namespace}' "
                f"to the 'namespaces' block of '{self.schema_fp.name}' (mapping it "
                "to the target schema's '#/$defs/'), or correct the prefix."
            )
        return self.namespaces[namespace] + identifier

    def process_property_tree_refs(self, raw_node, processed_node):
        if isinstance(raw_node, dict):
            for k, v in raw_node.items():
                if k.endswith("Curie"):
                    new_k = k[:-5]
                    processed_node[new_k] = self.resolve_curie(v)
                    del processed_node[k]
                elif k == "$ref":
                    if not v.startswith("#/"):
                        raise ValueError(
                            f"Ambiguous $ref {v!r} in '{self.schema_fp.name}'. A '$ref' value must be a local "
                            f"reference starting with '#/{self.schema_def_keyword}/' (e.g. "
                            f"'#/{self.schema_def_keyword}/{v.rsplit('/', 1)[-1].split('.')[0] or 'MyClass'}'). "
                            "Fix: for a reference to a class in another schema, use "
                            "'$refCurie: <namespace>:<Class>' instead."
                        )
                    if self.imported:
                        # TODO: fix below hard-coded name convention, yuck.
                        rel_root = self.schema_fp.parent.relative_to(self.root_schema_fp.parent, walk_up=True)
                        schema_stem = self.schema_fp.stem.split("-")[0]
                        processed_node[k] = str(rel_root / f"{schema_stem}.json{v}")
                else:
                    self.process_property_tree_refs(raw_node[k], processed_node[k])
        elif isinstance(raw_node, list):
            for raw_item, processed_item in zip(raw_node, processed_node):
                self.process_property_tree_refs(raw_item, processed_item)
        return

    def get_local_or_inherited_class(self, schema_class, raw=False):
        components = schema_class.split(":")
        if len(components) == 1:
            inherited_class_name = components[0]
            if raw:
                inherited_class = self.raw_schema[self.schema_def_keyword][inherited_class_name]
            else:
                self.process_schema_class(inherited_class_name)
                inherited_class = self.processed_schema[self.schema_def_keyword][inherited_class_name]
            proc = self
        elif len(components) == 2:
            inherited_class_name = components[1]
            proc = self.imports[components[0]]
            if raw:
                inherited_class = proc.raw_schema[proc.schema_def_keyword][inherited_class_name]
            else:
                inherited_class = proc.processed_schema[proc.schema_def_keyword][inherited_class_name]
        else:
            raise ValueError(
                f"Class reference {schema_class!r} has too many ':' separators. "
                "Fix: use either a local class name ('MyClass') or a single "
                "namespaced reference ('namespace:MyClass')."
            )
        return inherited_class, proc

    def get_class_uri(self, schema_class, mode):
        abs_path = self.get_class_abs_path(schema_class, mode)
        parsed_url = urlparse(self.id)
        return f"{parsed_url.scheme}://{parsed_url.netloc}{abs_path}"

    def get_class_abs_path(self, schema_class, mode):
        if mode == "json":
            export_key = self.json_key
        elif mode == "yaml":
            export_key = self.yaml_key
        else:
            raise ValueError("mode must be json or yaml")
        if self.class_is_protected(schema_class):
            frag_containing_class = self.raw_defs[schema_class]["protectedClassOf"]
            class_ref = f"{frag_containing_class}#/{self.schema_def_keyword}/{schema_class}"
        else:
            class_ref = schema_class
        parsed_url = urlparse(self.id)
        parsed_id_path = parsed_url.path
        base = Path(parsed_id_path).parent
        # Profile sources inject their sub-namespace after the export key so
        # class $ids read ``.../<version>/json/XXX/<Class>``.
        if self.sub_namespace:
            revised_path = base.joinpath(export_key, self.sub_namespace, class_ref)
        else:
            revised_path = base.joinpath(export_key, class_ref)
        return str(revised_path)

    def process_schema_class(self, schema_class):
        raw_class_def = self.raw_schema[self.schema_def_keyword][schema_class]
        if schema_class in self.processed_classes:
            return
        processed_class_def = self.processed_schema[self.schema_def_keyword][schema_class]

        # Check GKS maturity model on all schemas
        if "maturity" not in processed_class_def:
            raise ValueError(
                f"Class '{schema_class}' is missing the required 'maturity' field. "
                f"Fix: add a 'maturity' key to '{schema_class}' with one of "
                f"{sorted(maturity_levels)}, e.g. 'maturity: trial use'."
            )
        if processed_class_def["maturity"] not in maturity_levels:
            raise ValueError(
                f"Class '{schema_class}' has an invalid maturity "
                f"'{processed_class_def['maturity']}'. Fix: set 'maturity' on "
                f"'{schema_class}' to one of {sorted(maturity_levels)}."
            )

        if self.class_is_protected(schema_class):
            containing_class = self.raw_defs[schema_class]["protectedClassOf"]
            self.has_protected_members[containing_class].add(schema_class)
            if containing_class in self.has_children:
                for descendant in self.get_all_descendants(containing_class):
                    self.has_protected_members[descendant].add(schema_class)

        if self.class_is_primitive(schema_class):
            # Primitive/type-alias classes (e.g. array aliases) skip the
            # property/composition ref passes below, but may still carry nested
            # $refCurie/$ref values (e.g. under 'items'/'contains'/'anyOf').
            # Resolve them here so they don't leak unresolved into the schema.
            self.process_property_tree_refs(raw_class_def, processed_class_def)
            self.processed_classes.add(schema_class)
            return
        inherited_properties = {}
        inherited_required = set()
        inherits = processed_class_def.get("inherits", None)
        if inherits is not None:
            inherited_class, proc = self.get_local_or_inherited_class(inherits)
            # Inherit the parent's properties / required. Abstract and concrete
            # classes alike carry their members under 'properties' / 'required';
            # inheritance currently assumes an abstract parent.
            inherited_properties |= copy.deepcopy(inherited_class.get("properties", {}))
            inherited_required |= set(inherited_class.get("required", []))

            # inherit ga4gh keys
            if "ga4gh" in processed_class_def or "ga4gh" in inherited_class:
                if "ga4gh" not in processed_class_def:
                    if not self.class_is_abstract(schema_class):
                        raise ValueError(
                            f"Concrete class '{schema_class}' inherits a 'ga4gh' "
                            f"identifier config from '{inherits}' but does not define "
                            "its own 'ga4gh.prefix'. A concrete GA4GH-identifiable "
                            "class needs its own prefix. Fix: add a 'ga4gh' block to "
                            f"'{schema_class}' with a 'prefix' (e.g. 'ga4gh:\\n  "
                            "prefix: XX\\n  inherent: [type, ...]')."
                        )
                    processed_class_def["ga4gh"] = copy.deepcopy(inherited_class["ga4gh"])
                elif "ga4gh" not in inherited_class:
                    pass
                else:
                    ga4gh_inherent = set(inherited_class["ga4gh"]["inherent"])
                    ga4gh_inherent |= set(processed_class_def["ga4gh"].get("inherent", []))
                    processed_class_def["ga4gh"]["inherent"] = sorted(ga4gh_inherent)

        # Abstract and concrete classes both store members under properties/required.
        prop_k = "properties"
        req_k = "required"
        raw_class_properties = raw_class_def.get(prop_k, {})  # Nested inheritance!
        processed_class_properties = processed_class_def.get(prop_k, {})
        processed_class_required = set(processed_class_def.get(req_k, []))
        # Process refs
        self.process_property_tree_refs(raw_class_properties, processed_class_properties)
        # Resolve refs nested inside any class-level composition keyword. This is
        # not gated on class_is_container(): concrete allOf/anyOf/oneOf-composed
        # classes (the recipes/profiles) carry $refCurie values here too, and
        # gating left them unresolved (they leaked into the emitted schema).
        for key in ("allOf", "anyOf", "oneOf"):
            if key in raw_class_def:
                self.process_property_tree_refs(raw_class_def[key], processed_class_def[key])

        specialized = []
        for prop, prop_attribs in processed_class_properties.items():
            # 'extends' no longer exists. A subclass specializes an inherited
            # property by redeclaring it under the same name; it cannot rename.
            if "extends" in prop_attribs:
                raise ValueError(
                    f"Property '{schema_class}.{prop}' uses 'extends: "
                    f"{prop_attribs['extends']!r}', which is no longer supported. "
                    "Properties are inherited and specialized by matching the "
                    "inherited property's name, and inherited properties cannot be "
                    f"renamed. Fix: remove the 'extends' key from '{prop}'. If you "
                    f"were renaming '{prop_attribs['extends']}' to '{prop}', instead "
                    f"declare '{prop}' under the inherited name '{prop_attribs['extends']}'."
                )
            # Specialize an inherited property of the same name (subclass attributes
            # win). Merge in place so the property keeps the position it holds in
            # the superclass; only genuinely new subclass properties are appended.
            if prop in inherited_properties:
                merged = inherited_properties[prop]
                # Schema covariance: a parent schema must still validate a
                # subclass instance. A subclass may narrow/annotate an inherited
                # property (add constraints, refine description/comment/array
                # sizes) but may not change its type, const, or default (nor
                # rename it). Adding one of these where the parent has none is a
                # narrowing and is allowed.
                for guarded in ("type", "const", "default"):
                    if guarded in merged and guarded in prop_attribs and merged[guarded] != prop_attribs[guarded]:
                        raise ValueError(
                            f"Property '{schema_class}.{prop}' changes the inherited "
                            f"'{guarded}' from {merged[guarded]!r} to "
                            f"{prop_attribs[guarded]!r}. This breaks schema covariance: "
                            f"a parent schema must validate every subclass instance, "
                            f"but changing '{guarded}' makes some subclass instances "
                            "invalid against the parent. Fix: remove the conflicting "
                            f"'{guarded}' from '{schema_class}.{prop}' (a subclass may "
                            "only add constraints, or refine description/$comment/array "
                            f"sizes). If '{schema_class}' genuinely needs a different "
                            f"'{guarded}', define '{prop}' as a new property rather than "
                            "inheriting it."
                        )
                # reconcile polymorphic refs when the subclass narrows the type
                if "$ref" in prop_attribs:
                    merged.pop("oneOf", None)
                    merged.pop("anyOf", None)
                if "oneOf" in prop_attribs or "anyOf" in prop_attribs:
                    merged.pop("$ref", None)
                merged.update(prop_attribs)
                specialized.append(prop)
            # Validate required array attribute for GKS specs
            if self.enforce_ordered and prop_attribs.get("type", "") == "array":
                if "ordered" not in prop_attribs:
                    raise ValueError(
                        f"Array property '{schema_class}.{prop}' is missing the "
                        "required 'ordered' attribute (required because this schema "
                        "is strict / enforce_ordered). Fix: add 'ordered: true' if "
                        "element order is meaningful, otherwise 'ordered: false'."
                    )
                if not isinstance(prop_attribs["ordered"], bool):
                    raise ValueError(
                        f"Array property '{schema_class}.{prop}' has a non-boolean "
                        f"'ordered' value ({prop_attribs['ordered']!r}). "
                        "Fix: set 'ordered' to a YAML boolean, 'true' or 'false'."
                    )
            if self.strict and prop_attribs.get("type", "") == "object":
                if prop_attribs.get("additionalProperties", None) is None:
                    raise ValueError(
                        f"Object property '{schema_class}.{prop}' must declare "
                        "'additionalProperties' (required because this schema is "
                        "strict). Fix: add 'additionalProperties: false' to close the "
                        "object, or 'additionalProperties: true' to allow extra keys."
                    )
        # Drop specialized props from the subclass set so they aren't appended a
        # second time; the merged value already lives in its inherited position.
        for prop in specialized:
            del processed_class_properties[prop]

        # Validate class structures for GKS specs. Concrete classes omit
        # 'type' in source; the processor injects "type": "object" so every
        # emitted class (abstract and concrete alike) is a valid object schema.
        processed_class_def["type"] = "object"
        if self.class_is_ga4gh_identifiable(schema_class):
            prefix = processed_class_def["ga4gh"]["prefix"]
            if not isinstance(prefix, str) or prefix == "":
                raise ValueError(
                    f"GA4GH-identifiable class '{schema_class}' has an invalid "
                    f"'ga4gh.prefix' ({prefix!r}). Fix: set 'ga4gh.prefix' on "
                    f"'{schema_class}' to a non-empty string, e.g. 'prefix: VA'."
                )
            inherent = processed_class_def["ga4gh"]["inherent"]
            if len(inherent) < 2:
                raise ValueError(
                    f"GA4GH-identifiable class '{schema_class}' defines "
                    f"{len(inherent)} inherent propert(ies) ({inherent}) but at "
                    "least 2 are required (the computed identifier must be defined "
                    "by 'type' plus at least one other property). Fix: add the "
                    "identifying properties to 'ga4gh.inherent' on "
                    f"'{schema_class}'."
                )
            if "type" not in inherent:
                raise ValueError(
                    f"GA4GH-identifiable class '{schema_class}' omits 'type' from "
                    f"'ga4gh.inherent' ({inherent}). The class type must contribute "
                    "to the computed identifier. Fix: add 'type' to "
                    f"'ga4gh.inherent' on '{schema_class}'."
                )

        # Emit 'properties'/'required' only when non-empty. An empty
        # 'properties: {}' / 'required: []' is valid Draft 2020-12 but pure
        # noise on property-less/requirement-less classes (e.g. Element,
        # Condition, allOf-composed recipes whose members live under allOf).
        merged_properties = inherited_properties | processed_class_properties
        if merged_properties:
            processed_class_def[prop_k] = merged_properties
        else:
            processed_class_def.pop(prop_k, None)
        merged_required = sorted(inherited_required | processed_class_required)
        if merged_required:
            processed_class_def[req_k] = merged_required
        else:
            processed_class_def.pop(req_k, None)
        # Close concrete classes only. Abstract classes are left open (no
        # additionalProperties keyword): JSON Schema already allows extra
        # properties by default, and emitting additionalProperties: true would
        # mark every property "evaluated", defeating unevaluatedProperties: false
        # on any concrete class that composes the abstract class via allOf.
        if self.strict and not self.class_is_abstract(schema_class):
            # allOf/anyOf/oneOf-composed classes need the composition-aware
            # 'unevaluatedProperties'; plain 'additionalProperties' is blind to
            # properties introduced via those applicators and would reject them.
            if any(applicator in processed_class_def for applicator in ("allOf", "anyOf", "oneOf")):
                processed_class_def["unevaluatedProperties"] = False
            else:
                processed_class_def["additionalProperties"] = False
        self.processed_classes.add(schema_class)

    @staticmethod
    def _scrub_rst_markup(string):
        string = ref_re.sub(r"\g<1>", string)
        string = link_re.sub(r"[\g<1>](\g<2>)", string)
        string = string.replace("\n", " ")
        return string

    @staticmethod
    def _strip_key(node, key):
        """Recursively delete every occurrence of ``key`` from a nested
        dict/list structure (mutates in place)."""
        if isinstance(node, dict):
            node.pop(key, None)
            for value in node.values():
                YamlSchemaProcessor._strip_key(value, key)
        elif isinstance(node, list):
            for item in node:
                YamlSchemaProcessor._strip_key(item, key)

    def clean_for_js(self):
        self.for_js.pop("namespaces", None)
        self.for_js.pop("strict", None)
        self.for_js.pop("enforce_ordered", None)
        self.for_js.pop("imports", None)
        # `$comment` is an internal, source-only annotation: it stays in the
        # *-source.yaml but is stripped from the emitted JSON Schema wherever it
        # appears (class level, properties, or nested composition branches).
        self._strip_key(self.for_js, "$comment")
        for schema_class, schema_definition in self.for_js.get(self.schema_def_keyword, {}).items():
            # Every class (abstract included) is emitted as its own JSON Schema,
            # and $refs stay direct (no concretization to oneOf of descendants).
            # Strip metaschema-only keywords that are not valid JSON Schema.
            schema_definition.pop("inherits", None)
            schema_definition.pop("protectedClassOf", None)
            schema_definition.pop("abstract", None)
            schema_definition.pop("header_level", None)
            if "description" in schema_definition:
                schema_definition["description"] = self._scrub_rst_markup(schema_definition["description"])
            if "properties" in schema_definition:
                for p, p_def in schema_definition["properties"].items():
                    if "description" in p_def:
                        p_def["description"] = self._scrub_rst_markup(p_def["description"])

    def concretize_class_ref(self, cls_url):
        # Still used by class_is_subclass to walk the inheritance/container tree.
        children = self.has_children_urls.get(cls_url, None)
        if children is None:
            return {cls_url}
        out = set()
        for child in children:
            out.update(self.concretize_class_ref(child))
        return out
