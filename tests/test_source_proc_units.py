"""Unit tests for YamlSchemaProcessor pure helpers and class predicates.

These lock in behavior that the higher-level build tests exercise only
indirectly: RST scrubbing, CURIE resolution, the class-classification
predicates under the abstract/concrete convention, and the additionalProperties
policy (open on abstract, closed on strict concrete classes).
"""

import pytest
import yaml

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
        ("Ga4ghIdentifiableObject", True, False, False, False),
        # abstract, but no class-level oneOf/anyOf -> not a container
        ("Variation", True, False, False, False),
        ("Location", True, False, False, False),
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
