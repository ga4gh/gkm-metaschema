"""Unit tests for YamlSchemaProcessor pure helpers and class predicates.

These lock in behavior that the higher-level build tests exercise only
indirectly: RST scrubbing, CURIE resolution, the class-classification
predicates under the abstract/concrete convention, and the additionalProperties
policy (open on abstract, closed on strict concrete classes).
"""

import warnings

import pytest
import yaml

from ga4gh.gkm.metaschema.tools.source_proc import LostIriReferenceWarning
from ga4gh.gkm.metaschema.tools.source_proc import YamlSchemaProcessor


def _build_parent_child(tmp_path, parent_type_prop, child_type_prop):
    """Build a minimal parent(abstract)/child(concrete) schema where both define
    a ``type`` property, and run it through the processor. Raises if the child's
    specialization violates a processor rule."""
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://example.org/schema/cov/cov-source.yaml",
        "title": "Cov",
        "type": "object",
        "$defs": {
            "Parent": {
                "maturity": "draft",
                "abstract": True,
                "type": "object",
                "properties": {"type": parent_type_prop},
                "required": ["type"],
            },
            "Child": {
                "maturity": "draft",
                "inherits": "Parent",
                "description": "child",
                "properties": {"type": child_type_prop},
            },
        },
    }
    fp = tmp_path / "cov-source.yaml"
    with open(fp, "w") as f:
        yaml.safe_dump(schema, f)
    return YamlSchemaProcessor(fp)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (":ref:`Allele`", "Allele"),
        (":ref:`an allele <Allele>`", "an allele"),
        ("`RFC3986 <https://example.com>`_", "[RFC3986](https://example.com)"),
        # multiple links in one string must each convert independently; a
        # greedy URL group would collapse them into one broken link.
        (
            "see `A <https://a.example>`_ and `B <https://b.example>`_.",
            "see [A](https://a.example) and [B](https://b.example).",
        ),
        ("line one\nline two", "line one line two"),
        ("plain text, no markup", "plain text, no markup"),
    ],
)
def test_scrub_rst_markup(raw: str, expected: str) -> None:
    assert YamlSchemaProcessor._scrub_rst_markup(raw) == expected


def test_resolve_curie_uses_namespace_prefix(vrs_processor: YamlSchemaProcessor) -> None:
    # Arrange: vrs declares the gkm.core namespace
    # Act
    resolved = vrs_processor.resolve_curie("gkm.core:iriReference")
    # Assert
    assert resolved == "../gkm-core/gkm-core.yaml#/$defs/iriReference"


def test_resolve_curie_unknown_namespace_raises(vrs_processor: YamlSchemaProcessor) -> None:
    with pytest.raises(ValueError, match="undeclared namespace"):
        vrs_processor.resolve_curie("nope:Thing")


@pytest.mark.parametrize(
    "cls,abstract,primitive,container,ga4gh_identifiable",
    [
        # abstract, but no class-level oneOf/anyOf -> not a container
        ("Ga4ghIdentifiableObject", True, False, False, False),
        # abstract + sealed -> auto-derived oneOf -> a container
        ("Variation", True, False, True, False),
        ("Location", True, False, True, False),
        ("Allele", False, False, False, True),
        ("SequenceLocation", False, False, False, True),
        ("Expression", False, False, False, False),
        ("residue", False, True, False, False),
        ("sequenceString", False, True, False, False),
        ("Range", False, True, False, False),
    ],
)
def test_class_predicates(
    vrs_processor: YamlSchemaProcessor,
    cls: str,
    abstract: bool,
    primitive: bool,
    container: bool,
    ga4gh_identifiable: bool,
) -> None:
    assert vrs_processor.class_is_abstract(cls) is abstract
    assert vrs_processor.class_is_primitive(cls) is primitive
    assert vrs_processor.class_is_container(cls) is container
    assert vrs_processor.class_is_ga4gh_identifiable(cls) is ga4gh_identifiable


def test_abstract_class_is_left_open(vrs_processor: YamlSchemaProcessor) -> None:
    """Abstract schemas omit additionalProperties entirely (JSON Schema's
    default already allows extra properties). Emitting additionalProperties:
    true would mark every property "evaluated" and defeat unevaluatedProperties:
    false on any concrete class that composes the abstract class via allOf.
    """
    for abstract_cls in ("Variation", "Location", "SequenceExpression"):
        d = vrs_processor.for_js["$defs"][abstract_cls]
        assert "additionalProperties" not in d
        assert "unevaluatedProperties" not in d


def test_strict_concrete_class_forbids_additional_properties(
    vrs_processor: YamlSchemaProcessor,
) -> None:
    for concrete_cls in ("Allele", "SequenceLocation"):
        d = vrs_processor.for_js["$defs"][concrete_cls]
        # plain (non-composed) concrete classes close with additionalProperties
        assert "allOf" not in d
        assert d["additionalProperties"] is False
        assert "unevaluatedProperties" not in d


def test_strict_composed_class_uses_unevaluated_properties(
    recipes_processor: YamlSchemaProcessor,
) -> None:
    """Strict allOf-composed classes are closed with the composition-aware
    ``unevaluatedProperties`` rather than ``additionalProperties`` (which is
    blind to properties introduced via allOf and would reject them)."""
    for composed_cls in ("ProteinSequenceConsequence", "CanonicalAllele", "CategoricalCnv", "GeneFusion"):
        d = recipes_processor.for_js["$defs"][composed_cls]
        assert "allOf" in d
        assert d["unevaluatedProperties"] is False
        assert "additionalProperties" not in d


def test_empty_properties_and_required_are_omitted(
    gkm_core_processor: YamlSchemaProcessor,
    vrs_processor: YamlSchemaProcessor,
    recipes_processor: YamlSchemaProcessor,
) -> None:
    """Empty 'properties: {}' / 'required: []' are valid Draft 2020-12 but pure
    noise; the processor omits them. Classes that genuinely have members keep
    the keywords.
    """
    # allOf-composed recipes carry their members under 'allOf'; their top-level
    # properties/required are empty and must be dropped.
    gene_fusion = recipes_processor.for_js["$defs"]["GeneFusion"]
    assert "properties" not in gene_fusion
    assert "required" not in gene_fusion
    # closure keyword is still emitted (composition-aware)
    assert gene_fusion["unevaluatedProperties"] is False

    # A concrete class with real members keeps both keywords.
    allele = vrs_processor.for_js["$defs"]["Allele"]
    assert allele["properties"]
    assert allele["required"]

    # A class with properties but no required members keeps properties, drops
    # the empty required (Element has properties but requires none of them).
    element = gkm_core_processor.for_js["$defs"]["Element"]
    assert element["properties"]
    assert "required" not in element


def test_comment_is_stripped_from_emitted_schema(
    gkm_core_processor: YamlSchemaProcessor,
    vrs_processor: YamlSchemaProcessor,
    recipes_processor: YamlSchemaProcessor,
) -> None:
    """`$comment` is an internal source-only annotation and must not appear in
    the emitted JSON Schema — at class level, in properties, or nested inside
    composition branches.
    """
    import json

    for proc in (gkm_core_processor, vrs_processor, recipes_processor):
        for name, definition in proc.for_js["$defs"].items():
            assert "$comment" not in json.dumps(definition), f"{name} leaked a $comment"


def test_composition_refcuries_are_resolved(recipes_processor: YamlSchemaProcessor) -> None:
    """$refCurie values nested inside a class-level allOf/anyOf/oneOf must be
    resolved to real $refs. Concrete (non-container) composed classes such as
    GeneFusion carry $refCurie deep inside their allOf; ref resolution must not
    be gated on class_is_container(), or these leak unresolved into the emitted
    schema (a validator silently ignores the unknown $refCurie keyword).
    """
    import json

    # GeneFusion has $refCurie values buried in allOf -> contains -> anyOf.
    gene_fusion = json.dumps(recipes_processor.for_js["$defs"]["GeneFusion"])
    assert "Curie" not in gene_fusion

    # Nothing in the whole built schema should retain an unresolved *Curie key.
    for name, definition in recipes_processor.for_js["$defs"].items():
        assert "Curie" not in json.dumps(definition), f"{name} leaked an unresolved curie"


# --------------------------------------------------------------------------
# Schema covariance: a parent schema must validate a subclass instance, so a
# subclass may narrow/annotate an inherited property but may not change its
# type/const/default.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "guarded,parent_type_prop,child_type_prop",
    [
        ("type", {"type": "string"}, {"type": "integer"}),
        ("const", {"type": "string", "const": "A"}, {"type": "string", "const": "B"}),
        ("default", {"type": "string", "default": "a"}, {"type": "string", "default": "b"}),
    ],
)
def test_changing_inherited_type_const_default_is_rejected(tmp_path, guarded, parent_type_prop, child_type_prop):
    with pytest.raises(ValueError, match=guarded):
        _build_parent_child(tmp_path, parent_type_prop, child_type_prop)


@pytest.mark.parametrize(
    "parent_type_prop,child_type_prop",
    [
        # adding a const where the parent has none is a narrowing -> allowed
        ({"type": "string"}, {"type": "string", "const": "X"}),
        # adding a default where the parent has none -> allowed
        ({"type": "string"}, {"type": "string", "default": "x"}),
        # restating the same values -> allowed (no change)
        ({"type": "string", "const": "A"}, {"type": "string", "const": "A"}),
        # refining only the description -> allowed
        ({"type": "string"}, {"type": "string", "description": "more specific"}),
    ],
)
def test_narrowing_or_annotating_inherited_property_is_allowed(tmp_path, parent_type_prop, child_type_prop):
    proc = _build_parent_child(tmp_path, parent_type_prop, child_type_prop)
    # the child's type property built successfully and carries the merged result
    assert "type" in proc.for_js["$defs"]["Child"]["properties"]


# --------------------------------------------------------------------------
# $ref must be a local reference ("#/$defs/..."); anything else (a bare
# class name, an external path) is ambiguous and must use $refCurie instead.
# --------------------------------------------------------------------------


def _build_ref_schema(tmp_path, ref_value):
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://example.org/schema/refs/refs-source.yaml",
        "title": "Refs",
        "type": "object",
        "$defs": {
            "Other": {"maturity": "draft", "description": "other", "properties": {}},
            "Main": {
                "maturity": "draft",
                "description": "main",
                "properties": {"other": {"$ref": ref_value}},
            },
        },
    }
    fp = tmp_path / "refs-source.yaml"
    with open(fp, "w") as f:
        yaml.safe_dump(schema, f)
    return YamlSchemaProcessor(fp)


def test_bare_ref_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="refCurie"):
        _build_ref_schema(tmp_path, "Other")


def test_external_path_ref_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="refCurie"):
        _build_ref_schema(tmp_path, "/ga4gh/schema/other/1.x/json/Other")


def test_local_ref_is_allowed(tmp_path):
    proc = _build_ref_schema(tmp_path, "#/$defs/Other")
    assert proc.for_js["$defs"]["Main"]["properties"]["other"]["$ref"] == "#/$defs/Other"


# --------------------------------------------------------------------------
# `sealed: true` on an abstract class auto-derives a `oneOf` of its concrete
# descendants, restricting $ref/$refCurie sites to a closed, known set
# instead of the open contract an abstract class has by default.
# --------------------------------------------------------------------------


def _build_sealed_schema(tmp_path, defs):
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://example.org/schema/sealed/sealed-source.yaml",
        "title": "Sealed",
        "type": "object",
        "$defs": defs,
    }
    fp = tmp_path / "sealed-source.yaml"
    with open(fp, "w") as f:
        yaml.safe_dump(schema, f)
    return YamlSchemaProcessor(fp)


_SHAPE_HIERARCHY = {
    "Shape": {
        "maturity": "draft",
        "abstract": True,
        "sealed": True,
        "description": "A closed shape hierarchy.",
        "properties": {"kind": {"type": "string", "description": "discriminator"}},
        "required": ["kind"],
    },
    # Polygon is abstract and has no properties of its own -- it must NOT
    # appear in Shape's oneOf (an open/abstract member would let anything
    # matching its (empty) shape double-match, breaking oneOf's "exactly
    # one" requirement); only its concrete descendant Square should.
    "Polygon": {
        "maturity": "draft",
        "abstract": True,
        "inherits": "Shape",
        "description": "An abstract intermediate.",
    },
    "Circle": {
        "maturity": "draft",
        "inherits": "Shape",
        "description": "A concrete circle.",
        "properties": {"radius": {"type": "number", "description": "r"}},
        "required": ["radius"],
    },
    "Square": {
        "maturity": "draft",
        "inherits": "Polygon",
        "description": "A concrete square, reached transitively through Polygon.",
        "properties": {"side": {"type": "number", "description": "s"}},
        "required": ["side"],
    },
}


def test_sealed_class_derives_oneof_of_concrete_descendants(tmp_path):
    proc = _build_sealed_schema(tmp_path, _SHAPE_HIERARCHY)
    shape = proc.for_js["$defs"]["Shape"]
    assert shape["oneOf"] == [{"$ref": "#/$defs/Circle"}, {"$ref": "#/$defs/Square"}]
    # Square is reached transitively through the abstract Polygon; Polygon
    # itself (abstract, so uninstantiable) must not appear in the union.
    refs = {member["$ref"] for member in shape["oneOf"]}
    assert "#/$defs/Polygon" not in refs
    # sealed is fully materialized into oneOf; the flag itself is stripped.
    assert "sealed" not in shape
    # abstract survives (see the abstract-in-JSON feature), own properties
    # and type: object are untouched -- sealed only adds oneOf alongside them.
    assert shape["abstract"] is True
    assert shape["type"] == "object"
    assert "kind" in shape["properties"]


def test_sealed_on_non_abstract_class_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="not abstract"):
        _build_sealed_schema(
            tmp_path,
            {"Foo": {"maturity": "draft", "sealed": True, "description": "x", "properties": {}}},
        )


@pytest.mark.parametrize("composition_kw", ["oneOf", "anyOf", "allOf"])
def test_sealed_with_hand_authored_composition_is_rejected(tmp_path, composition_kw):
    with pytest.raises(ValueError, match="ambiguous"):
        _build_sealed_schema(
            tmp_path,
            {
                "Foo": {
                    "maturity": "draft",
                    "abstract": True,
                    "sealed": True,
                    "description": "x",
                    composition_kw: [{"$ref": "#/$defs/Bar"}],
                },
                "Bar": {"maturity": "draft", "description": "y", "properties": {}},
            },
        )


def test_sealed_with_no_concrete_descendants_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="no concrete subclasses"):
        _build_sealed_schema(
            tmp_path,
            {"Foo": {"maturity": "draft", "abstract": True, "sealed": True, "description": "x", "properties": {}}},
        )


def test_sealed_with_only_abstract_descendants_is_rejected(tmp_path):
    """An abstract descendant alone doesn't satisfy 'at least one concrete
    descendant' -- it can never itself be instantiated."""
    with pytest.raises(ValueError, match="no concrete subclasses"):
        _build_sealed_schema(
            tmp_path,
            {
                "Foo": {"maturity": "draft", "abstract": True, "sealed": True, "description": "x", "properties": {}},
                "Bar": {"maturity": "draft", "abstract": True, "inherits": "Foo", "description": "y"},
            },
        )


def test_sealed_resolution_is_idempotent_across_reprocessing(tmp_path):
    """_resolve_sealed_classes runs again whenever _init_from_raw does (e.g.
    merge_imported() re-derives processed_schema/for_js after merging in
    every import's raw_defs). A sealed class already resolved in an earlier
    pass must not trip the "already has oneOf" conflict guard against its
    own previously-derived oneOf -- regression test for a real bug where
    merge_imported() raised on every sealed class.
    """
    proc = _build_sealed_schema(tmp_path, _SHAPE_HIERARCHY)
    before = proc.for_js["$defs"]["Shape"]["oneOf"]
    proc.merge_imported()  # re-invokes _init_from_raw on the same instance
    after = proc.for_js["$defs"]["Shape"]["oneOf"]
    assert after == before == [{"$ref": "#/$defs/Circle"}, {"$ref": "#/$defs/Square"}]


# _build_sealed_schema is a generic minimal-schema builder despite its name
# (just wraps a $defs dict) -- reused below for the LostIriReferenceWarning tests.


def _irireference_narrowing_defs(override):
    """Minimal Parent(abstract)/Widget/iriReference/Child hierarchy: Parent.thing
    offers iriReference; Child narrows 'thing' to ``override`` via inherits."""
    return {
        "Parent": {
            "maturity": "draft",
            "abstract": True,
            "description": "An abstract parent offering iriReference for 'thing'.",
            "properties": {
                "thing": {"oneOf": [{"$ref": "#/$defs/Widget"}, {"$ref": "#/$defs/iriReference"}]},
            },
        },
        "Widget": {"maturity": "draft", "description": "A concrete widget."},
        "iriReference": {"maturity": "draft", "description": "An external reference."},
        "Child": {
            "maturity": "draft",
            "inherits": "Parent",
            "description": "A concrete child narrowing 'thing'.",
            "properties": {"thing": override},
        },
    }


def test_inherited_irireference_narrowed_away_warns(tmp_path):
    """A subclass narrowing an inherited property that offered iriReference
    to a schema that no longer does -- warns, since a conforming instance
    can no longer use an external reference for that property even though
    the parent class says it should be able to.
    """
    defs = _irireference_narrowing_defs({"$ref": "#/$defs/Widget"})
    with pytest.warns(LostIriReferenceWarning, match="Child.thing"):
        _build_sealed_schema(tmp_path, defs)


def test_inherited_irireference_retained_does_not_warn(tmp_path):
    """A subclass that re-offers iriReference alongside its narrowed type
    does not warn -- the capability is preserved, just narrowed."""
    override = {"oneOf": [{"$ref": "#/$defs/Widget"}, {"$ref": "#/$defs/iriReference"}]}
    defs = _irireference_narrowing_defs(override)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _build_sealed_schema(tmp_path, defs)
    assert not any(issubclass(w.category, LostIriReferenceWarning) for w in caught)


def test_inherited_description_only_override_does_not_warn(tmp_path):
    """A subclass override that only refines the description (no type-
    narrowing keys) doesn't touch the inherited oneOf at all -- the merge
    keeps it, so there's nothing to warn about."""
    defs = _irireference_narrowing_defs({"description": "a more specific description"})
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _build_sealed_schema(tmp_path, defs)
    assert not any(issubclass(w.category, LostIriReferenceWarning) for w in caught)


def _irireference_composition_defs(override):
    """Minimal Base/Widget/iriReference/Composed hierarchy: Base.thing offers
    iriReference; Composed narrows 'thing' to ``override`` via its own local
    allOf properties member (composition, not inherits)."""
    return {
        "Base": {
            "maturity": "draft",
            "description": "A base class offering iriReference for 'thing'.",
            "properties": {
                "thing": {"oneOf": [{"$ref": "#/$defs/Widget"}, {"$ref": "#/$defs/iriReference"}]},
            },
        },
        "Widget": {"maturity": "draft", "description": "A concrete widget."},
        "iriReference": {"maturity": "draft", "description": "An external reference."},
        "Composed": {
            "maturity": "draft",
            "description": "A class composing Base and narrowing 'thing'.",
            "allOf": [
                {"$ref": "#/$defs/Base"},
                {"properties": {"thing": override}},
            ],
        },
    }


def test_composed_irireference_narrowed_away_warns(tmp_path):
    """An allOf-composed class's own local override of a property its
    composed base offers iriReference for -- narrowed to a bare ref --
    warns. Unlike the inherits case, this isn't merged by the processor at
    all: the emitted schema keeps both allOf members as separate,
    simultaneously-applied constraints, so the local override silently
    excludes iriReference regardless of what the base allows (real
    regression fixture: Statement.proposition composed into
    VariantPathogenicityStatement, see PR #86).
    """
    defs = _irireference_composition_defs({"$ref": "#/$defs/Widget"})
    with pytest.warns(LostIriReferenceWarning, match="Composed.thing"):
        _build_sealed_schema(tmp_path, defs)


def test_composed_irireference_retained_does_not_warn(tmp_path):
    """A composed class that re-offers iriReference alongside its narrowed
    type in its own local override does not warn."""
    override = {"oneOf": [{"$ref": "#/$defs/Widget"}, {"$ref": "#/$defs/iriReference"}]}
    defs = _irireference_composition_defs(override)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _build_sealed_schema(tmp_path, defs)
    assert not any(issubclass(w.category, LostIriReferenceWarning) for w in caught)


def test_composed_description_only_override_does_not_warn(tmp_path):
    """A composed class's local override that only refines the description
    (no type-narrowing keys) doesn't change what the base's schema for that
    property allows -- nothing to warn about."""
    defs = _irireference_composition_defs({"description": "a more specific description"})
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _build_sealed_schema(tmp_path, defs)
    assert not any(issubclass(w.category, LostIriReferenceWarning) for w in caught)


def _lost_irireference_warnings(tmp_path, defs):
    """Build ``defs`` and return the LostIriReferenceWarning messages emitted."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _build_sealed_schema(tmp_path, defs)
    return [str(w.message) for w in caught if issubclass(w.category, LostIriReferenceWarning)]


_WIDGET = {"$ref": "#/$defs/Widget"}
_WIDGET_OR_IRI = {"oneOf": [{"$ref": "#/$defs/Widget"}, {"$ref": "#/$defs/iriReference"}]}


def _irireference_chain_defs(intermediate, leaf_override):
    """Base.thing offers iriReference; ``intermediate`` (a class def) sits
    between Base and Leaf; Leaf composes Intermediate and overrides 'thing'
    with ``leaf_override``."""
    return {
        "Base": {
            "maturity": "draft",
            "abstract": True,
            "description": "A base class offering iriReference for 'thing'.",
            "properties": {"thing": _WIDGET_OR_IRI},
        },
        "Widget": {"maturity": "draft", "description": "A concrete widget."},
        "iriReference": {"maturity": "draft", "description": "An external reference."},
        "Intermediate": {"maturity": "draft", "description": "An intermediate class.", **intermediate},
        "Leaf": {
            "maturity": "draft",
            "description": "A class composing Intermediate and narrowing 'thing'.",
            "allOf": [{"$ref": "#/$defs/Intermediate"}, {"properties": {"thing": leaf_override}}],
        },
    }


_ANNOTATION_ONLY_INTERMEDIATES = {
    "allOf": {"allOf": [{"$ref": "#/$defs/Base"}, {"properties": {"thing": {"description": "refined"}}}]},
    "inherits": {"inherits": "Base", "properties": {"thing": {"description": "refined"}}},
}


@pytest.mark.parametrize("via", sorted(_ANNOTATION_ONLY_INTERMEDIATES))
def test_composed_irireference_lost_through_annotation_only_intermediate_warns(tmp_path, via):
    """An intermediate class that only refines 'thing''s description keeps
    Base's iriReference alternative, so a Leaf narrowing 'thing' to a bare
    Widget through it still loses iriReference and warns -- for an
    intermediate that composes Base (allOf) or inherits it (PR #87 review)."""
    defs = _irireference_chain_defs(_ANNOTATION_ONLY_INTERMEDIATES[via], _WIDGET)
    messages = _lost_irireference_warnings(tmp_path, defs)
    assert any("Leaf.thing" in m for m in messages)


@pytest.mark.parametrize("via", sorted(_ANNOTATION_ONLY_INTERMEDIATES))
def test_composed_irireference_retained_through_annotation_only_intermediate_does_not_warn(tmp_path, via):
    """Same chain, but Leaf re-offers iriReference -- no warning."""
    defs = _irireference_chain_defs(_ANNOTATION_ONLY_INTERMEDIATES[via], _WIDGET_OR_IRI)
    assert _lost_irireference_warnings(tmp_path, defs) == []


def _irireference_array_composition_defs(override):
    """Base.things is an array whose items offer iriReference; Composed
    overrides 'things' with ``override`` via its local allOf member."""
    return {
        "Base": {
            "maturity": "draft",
            "description": "A base class whose 'things' items offer iriReference.",
            "properties": {"things": {"type": "array", "ordered": False, "items": _WIDGET_OR_IRI}},
        },
        "Widget": {"maturity": "draft", "description": "A concrete widget."},
        "iriReference": {"maturity": "draft", "description": "An external reference."},
        "Composed": {
            "maturity": "draft",
            "description": "A class composing Base and overriding 'things'.",
            "allOf": [{"$ref": "#/$defs/Base"}, {"properties": {"things": override}}],
        },
    }


def test_composed_items_only_narrowing_warns(tmp_path):
    """An array override giving only 'items' (no repeated 'type: array')
    still applies under allOf, so narrowing the items to a bare Widget drops
    the base's iriReference item alternative and warns (PR #87 review)."""
    defs = _irireference_array_composition_defs({"items": _WIDGET})
    messages = _lost_irireference_warnings(tmp_path, defs)
    assert any("Composed.things" in m for m in messages)


@pytest.mark.parametrize(
    "override",
    [
        pytest.param({"items": {"description": "refined item"}}, id="items-annotation-only"),
        pytest.param({"type": "array"}, id="type-only"),
        pytest.param({"minItems": 1}, id="array-size-only"),
        pytest.param({"items": _WIDGET_OR_IRI}, id="items-retain-irireference"),
    ],
)
def test_composed_array_override_keeping_irireference_does_not_warn(tmp_path, override):
    """Array overrides that leave the items' iriReference alternative in
    place -- annotating the items, repeating the type, or sizing the array --
    don't warn."""
    defs = _irireference_array_composition_defs(override)
    assert _lost_irireference_warnings(tmp_path, defs) == []


def test_composed_property_forbidden_with_false_does_not_warn(tmp_path):
    """Narrowing a property to boolean schema false forbids it outright --
    an intentional removal, not an accidental loss of iriReference."""
    defs = _irireference_composition_defs(False)
    assert _lost_irireference_warnings(tmp_path, defs) == []
