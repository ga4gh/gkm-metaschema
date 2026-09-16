"""Scoped tests for the metaschema processor.

These tests exercise every ``*-source.yaml`` in this repo's test fixtures:
``gkm-core-source.yaml``, ``vrs-source.yaml`` (imports gkm-core),
``cat-vrs-source.yaml`` (imports gkm-core + vrs), ``recipes-source.yaml``
(imports cat-vrs), and the va-spec schemas under ``data/va-spec`` (base:
domain-entities + va-core; profiles: aac-2017, acmg-2015, ccv-2022).

They also lock in the removal of ``extends`` property renaming: subclasses
specialize an inherited property by reusing its name (auto-merge, subclass
attributes win) and may no longer rename inherited properties.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from ga4gh.gkm.metaschema.scripts.source2splitjs import split_defs_to_js
from ga4gh.gkm.metaschema.scripts.y2t import build_cross_references
from ga4gh.gkm.metaschema.scripts.y2t import describe_composition_member
from ga4gh.gkm.metaschema.scripts.y2t import flatten_allof
from ga4gh.gkm.metaschema.scripts.y2t import main as y2t
from ga4gh.gkm.metaschema.scripts.y2t import render_class
from ga4gh.gkm.metaschema.tools.source_proc import YamlSchemaProcessor

root = Path(__file__).parent
GKM_CORE = root / "data/gkm-core/gkm-core-source.yaml"
VRS = root / "data/vrs/vrs-source.yaml"
CAT_VRS = root / "data/catvrs/cat-vrs-source.yaml"
RECIPES = root / "data/catvrs/recipes-source.yaml"

# va-spec is flat: all sources live at the va-spec/ top level. domain-entities +
# va-core share one json/ + def/ output dir (va-spec/); each XXX-profile-source
# writes into its own sub-namespace dir nested inside those (va-spec/json/XXX +
# va-spec/def/XXX), so va-spec/ itself only ever has json/ and def/ children.
DOMAIN_ENTITIES = root / "data/va-spec/domain-entities-source.yaml"
VA_CORE = root / "data/va-spec/va-core-source.yaml"
VA_PROFILES = [
    root / "data/va-spec/aac-2017-profile-source.yaml",
    root / "data/va-spec/acmg-2015-profile-source.yaml",
    root / "data/va-spec/ccv-2022-profile-source.yaml",
]
VA_SPEC_ALL = [DOMAIN_ENTITIES, VA_CORE, *VA_PROFILES]


def _iter_dicts(node):
    """Yield every dict nested anywhere within ``node``."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _iter_dicts(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_dicts(item)


def _assert_no_extends(schema):
    for d in _iter_dicts(schema):
        assert "extends" not in d, "'extends' should not survive processing"


def test_gkm_core_builds():
    proc = YamlSchemaProcessor(GKM_CORE)
    _assert_no_extends(proc.processed_schema)
    _assert_no_extends(proc.for_js)


def test_vrs_builds():
    proc = YamlSchemaProcessor(VRS)
    _assert_no_extends(proc.processed_schema)
    _assert_no_extends(proc.for_js)


def test_cat_vrs_builds():
    proc = YamlSchemaProcessor(CAT_VRS)
    _assert_no_extends(proc.processed_schema)
    _assert_no_extends(proc.for_js)


def test_recipes_builds():
    proc = YamlSchemaProcessor(RECIPES)
    _assert_no_extends(proc.processed_schema)
    _assert_no_extends(proc.for_js)


def _render_one(proc, class_name, tmp_path):
    """Render a single class's .rst into tmp_path and return its text."""
    kw = proc.schema_def_keyword
    class_def = proc.processed_schema[kw][class_name]
    render_class(proc, class_name, class_def, tmp_path, used_in={}, subclasses={})
    return (tmp_path / f"{class_name}.rst").read_text()


def test_maturity_note_admonition(gkm_core_processor, recipes_processor, tmp_path):
    """The maturity note is an admonition titled with the maturity level
    (Draft / Trial Use), colored via :class: (warning / note), whose body links
    to the repo's /appendices/maturity_model.html.
    """
    # The link is an RST substitution reference so downstream doc sites can
    # configure the target via rst_prolog; the fragment only references it.
    link = "See |maturity-model|."

    def rendered(proc, level):
        defs = proc.processed_schema[proc.schema_def_keyword]
        cls = next(c for c, d in defs.items() if d.get("maturity") == level)
        return _render_one(proc, cls, tmp_path)

    trial = rendered(gkm_core_processor, "trial use")
    assert trial.startswith(".. admonition:: Trial Use\n    :class: note\n")
    assert "May change in future releases. " + link in trial

    draft = rendered(recipes_processor, "draft")
    assert draft.startswith(".. admonition:: Draft\n    :class: warning\n")
    assert "May change significantly in future releases. " + link in draft


def test_abstract_class_has_no_ga4gh_digest(vrs_processor, tmp_path):
    """An abstract class must not render a GA4GH Digest section even though it
    carries a ga4gh block that its concrete subclasses inherit — it is never
    instantiated, so the digest applies only to the concrete subclasses.
    """
    # Ga4ghIdentifiableObject is abstract and defines the ga4gh prefix/inherent.
    assert vrs_processor.class_is_abstract("Ga4ghIdentifiableObject")
    assert "ga4gh" in vrs_processor.processed_schema[vrs_processor.schema_def_keyword]["Ga4ghIdentifiableObject"]
    abstract_rst = _render_one(vrs_processor, "Ga4ghIdentifiableObject", tmp_path)
    assert "GA4GH Digest" not in abstract_rst

    # A concrete GA4GH-identifiable subclass still renders the digest.
    concrete_rst = _render_one(vrs_processor, "Allele", tmp_path)
    assert "GA4GH Digest" in concrete_rst


def test_sealed_class_renders_explanatory_note(vrs_processor, tmp_path):
    """A sealed class's .rst carries a **Sealed** note explaining the closed
    subclass restriction, right alongside the **Abstract Class** note. A
    merely-abstract (not sealed) class gets the Abstract Class note but not
    the Sealed one.
    """
    assert vrs_processor.class_is_abstract("Variation")
    sealed_rst = _render_one(vrs_processor, "Variation", tmp_path)
    assert "**Abstract Class**" in sealed_rst
    assert "**Sealed**" in sealed_rst
    assert "Variation has a closed, exhaustive set of concrete subclasses" in sealed_rst

    assert vrs_processor.class_is_abstract("Ga4ghIdentifiableObject")
    unsealed_rst = _render_one(vrs_processor, "Ga4ghIdentifiableObject", tmp_path)
    assert "**Abstract Class**" in unsealed_rst
    assert "**Sealed**" not in unsealed_rst


def test_inherits_note_precedes_subclasses(vrs_processor, tmp_path):
    """A class's .rst notes its own direct 'inherits' target -- the mirror of
    Subclasses: (its direct children) -- immediately above Subclasses:,
    resolving a cross-schema 'namespace:Class' inherits value (e.g.
    'gkm-core:Entity') to the bare class name. A class with no parent gets no
    Inherits: line at all.
    """
    owners = {name: vrs_processor for name in vrs_processor.processed_schema[vrs_processor.schema_def_keyword]}
    used_in, subclasses = build_cross_references(owners)

    def render(class_name):
        kw = vrs_processor.schema_def_keyword
        class_def = vrs_processor.processed_schema[kw][class_name]
        render_class(vrs_processor, class_name, class_def, tmp_path, used_in, subclasses)
        return (tmp_path / f"{class_name}.rst").read_text()

    # MolecularVariation both inherits (Variation) and has subclasses
    # (Allele, ...) -- Inherits: must appear, and precede Subclasses:.
    rst = render("MolecularVariation")
    assert "**Inherits:** :ref:`Variation`" in rst
    assert rst.index("**Inherits:**") < rst.index("**Subclasses:**")

    # Ga4ghIdentifiableObject's raw 'inherits' is the cross-schema curie
    # 'gkm-core:Entity'; the namespace prefix must be stripped for the :ref:.
    cross_schema_rst = render("Ga4ghIdentifiableObject")
    assert "**Inherits:** :ref:`Entity`" in cross_schema_rst

    # 'inherits'-based classes have no allOf composition, so no Composes:.
    assert "**Composes:**" not in rst
    assert "**Composes:**" not in cross_schema_rst

    # Range has no 'inherits' at all -- no Inherits: line.
    assert "inherits" not in vrs_processor.raw_defs["Range"]
    no_parent_rst = render("Range")
    assert "**Inherits:**" not in no_parent_rst


def test_composes_note_for_allof_composed_class(tmp_path):
    """An allOf-composed class (e.g. AmpAscoCapEvidenceLine, which allOf's
    va.core:EvidenceLine plus local narrowing) gets a **Composes:** note --
    the allOf equivalent of **Inherits:**, which flatten_allof's own
    property-merging doesn't otherwise surface anywhere in the rendered page.
    """
    proc = YamlSchemaProcessor(root / "data/va-spec/aac-2017-profile-source.yaml")
    rst = _render_one(proc, "AmpAscoCapEvidenceLine", tmp_path)
    assert "**Composes:** :ref:`EvidenceLine`" in rst


def test_conditional_constraints_rendered_for_if_then_allof(tmp_path):
    """allOf `if`/`then` members (AMP/ASCO/CAP's tier-dependent business
    rules on VariantClinicalSignificanceStatement) are entirely invisible to
    flatten_allof (no top-level $ref or properties key) -- previously
    silently dropped from the rendered docs. Each branch's condition reduces
    to a single "property equals value" pin, so all of them render as rows
    in one **Conditional Constraints** table: a const-narrowed nested path
    ('have value'), a structural narrowing resolved via resolve_type ('be
    one of'/'be narrowed to'), and a JSON-Schema-boolean-false forbidden
    property ('not be provided') all need to render correctly.
    """
    proc = YamlSchemaProcessor(root / "data/va-spec/aac-2017-profile-source.yaml")
    rst = _render_one(proc, "VariantClinicalSignificanceStatement", tmp_path)
    assert "**Conditional Constraints**" in rst
    assert "If property..." in rst
    assert "has value..." in rst
    assert "then property..." in rst
    assert "must..." in rst

    assert "``classification.primaryCoding.code``\n      - ``tier i``\n" in rst
    assert "``classification.name``\n      - have value ``Tier I``" in rst
    assert "``strength.primaryCoding.code``\n      - have value ``strong``" in rst
    assert "``direction``\n      - have value ``supports``" in rst
    assert "be one of: :ref:`iriReference`, :ref:`DiagnosticEvidenceLine`" in rst

    # tier iii/iv forbid strength entirely (JSON-Schema boolean false).
    assert "``classification.primaryCoding.code``\n      - ``tier iii``\n" in rst
    assert "``strength``\n      - not be provided" in rst

    # an allOf-composed class with no if/then member gets no Conditional
    # Constraints section (AmpAscoCapEvidenceLine's allOf is just a base ref
    # + local property narrowing, no conditional branches).
    ampascocap_rst = _render_one(proc, "AmpAscoCapEvidenceLine", tmp_path)
    assert "**Conditional Constraints**" not in ampascocap_rst


def test_conditional_constraints_pattern_narrowing_is_readable(tmp_path):
    """resolve_type only recognizes type/$ref/$refCurie/allOf/oneOf/anyOf --
    a then-branch that narrows a property via `pattern` (regex) instead of
    const/enum/type falls through resolve_type's cases and used to leak its
    internal "_Not Specified_" sentinel straight into the rendered docs.
    VariantOncogenicityEvidenceLine's CCV methodType branches narrow
    evidenceOutcome.primaryCoding.code this way and are a real-world
    regression fixture for it -- each is a single-value if-condition, so
    they render as table rows.
    """
    proc = YamlSchemaProcessor(root / "data/va-spec/ccv-2022-profile-source.yaml")
    rst = _render_one(proc, "VariantOncogenicityEvidenceLine", tmp_path)
    assert "_Not Specified_" not in rst
    assert (
        "``specifiedBy.methodType``\n      - ``population_frequency``\n"
        "      - ``evidenceOutcome.primaryCoding.code``\n"
        "      - match the pattern ``^(SBVS1|SBS1|OP4)(_.+)?$``"
    ) in rst
    # a required-only consequence (no narrowing of its own) reads as "be provided".
    assert (
        "``directionOfEvidenceProvided``\n      - one of: ``supports``, ``disputes``\n"
        "      - ``strengthOfEvidenceProvided``\n"
        "      - be provided"
    ) in rst


def test_conditional_constraints_complex_condition_falls_back_to_prose(tmp_path):
    """A condition that isn't a single "property equals value" pin -- here, a
    compound (multi-property) `if` -- doesn't reduce to a table row, so it
    falls back to the original prose rendering rather than being dropped or
    misrepresented. Uses a synthetic schema since none of the real va-spec/
    catvrs fixtures happen to have a compound if-condition.
    """
    source = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://example.org/compound-condition-source.yaml",
        "title": "Compound Condition Test",
        "$defs": {
            "Widget": {
                "maturity": "draft",
                "description": "A widget with a compound conditional rule.",
                "properties": {
                    "kind": {"type": "string"},
                    "size": {"type": "string"},
                    "color": {"type": "string"},
                },
                "allOf": [
                    {
                        "if": {
                            "properties": {
                                "kind": {"const": "gadget"},
                                "size": {"const": "large"},
                            },
                        },
                        "then": {"properties": {"color": {"const": "red"}}},
                    },
                ],
            },
        },
    }
    source_fp = tmp_path / "compound-condition-source.yaml"
    source_fp.write_text(yaml.safe_dump(source))
    proc = YamlSchemaProcessor(source_fp)
    rst = _render_one(proc, "Widget", tmp_path)
    assert "**Conditional Constraints**" in rst
    # falls back to prose, not a table -- a compound if has no single "If
    # property.../has value..." row to contribute.
    assert "If property..." not in rst
    assert "If ``kind`` is ``gadget`` and ``size`` is ``large``, then:" in rst
    assert "* ``color`` must be: ``red``" in rst


def test_used_in_omits_transitive_subclass_enumeration_mirror(vrs_processor):
    """Sealed Variation's auto-derived oneOf directly $refs Allele (a
    grandchild, reached transitively through the abstract MolecularVariation)
    -- but Allele's Used in: must not list Variation for it, or it duplicates
    the Subclasses: relationship already shown on MolecularVariation's page.
    Regression test for a bug where the mirror-suppression only caught a
    *direct* parent/child pair, not a transitive one.
    """
    owners = {name: vrs_processor for name in vrs_processor.processed_schema[vrs_processor.schema_def_keyword]}
    used_in, subclasses = build_cross_references(owners)
    assert "Variation" not in used_in.get("Allele", set())
    # the direct relationship is unaffected: MolecularVariation's own
    # Subclasses: still correctly lists Allele.
    assert "Allele" in subclasses.get("MolecularVariation", set())


def test_composition_member_required_only_is_described_specifically():
    """describe_composition_member only handled $ref/$refCurie/oneOf/anyOf
    and `properties`-bearing members -- a `required`-only member (no
    properties/refs of its own, e.g. MappableConcept's anyOf: [{required:
    [name]}, {required: [primaryCoding]}], meaning "at least one of these
    must be present") fell through to the generic "an object with
    additional constraints" fallback for *both* branches, rendering an
    unhelpful duplicate line instead of naming what's actually required.
    """
    assert describe_composition_member({"required": ["name"]}) == "an object requiring ``name``"
    assert describe_composition_member({"required": ["name", "primaryCoding"]}) == (
        "an object requiring ``name``, ``primaryCoding``"
    )
    # a member with both a properties narrowing and a required list combines both.
    assert describe_composition_member({"properties": {"code": {}}, "required": ["code"]}) == (
        "an object constraining ``code`` and requiring ``code``"
    )
    # genuinely nothing to describe still falls back to the generic text.
    assert describe_composition_member({}) == "an object with additional constraints"


def test_mappable_concept_anyof_members_render_distinctly(gkm_core_processor, tmp_path):
    """Regression fixture for the bug above: MappableConcept's anyOf (either
    `name` or `primaryCoding` must be present) previously rendered as two
    identical, uninformative "an object with additional constraints"
    bullets -- now each names the property it actually requires.
    """
    rst = _render_one(gkm_core_processor, "MappableConcept", tmp_path)
    assert "an object with additional constraints" not in rst
    assert "* an object requiring ``name``" in rst
    assert "* an object requiring ``primaryCoding``" in rst


def test_flatten_allof_recurses_through_composed_base(tmp_path):
    """flatten_allof only read a referenced base's top-level `properties` --
    a single level. DiagnosticEvidenceLine's allOf base, AmpAscoCapEvidenceLine,
    is itself allOf-composed (no flat top-level `properties` of its own; its
    shape comes from composing va.core:EvidenceLine plus local narrowing), so
    the old single-level lookup silently dropped every property
    AmpAscoCapEvidenceLine contributes -- the flattened table showed only
    DiagnosticEvidenceLine's own locally-added `targetProposition`, missing
    the 12 properties composed in from EvidenceLine plus AmpAscoCapEvidenceLine's
    own strengthOfEvidenceProvided.
    """
    proc = YamlSchemaProcessor(root / "data/va-spec/aac-2017-profile-source.yaml")
    class_def = proc.processed_schema[proc.schema_def_keyword]["DiagnosticEvidenceLine"]
    effective, required = flatten_allof(class_def, proc)
    assert "targetProposition" in effective  # DiagnosticEvidenceLine's own narrowing
    # properties composed into AmpAscoCapEvidenceLine's own allOf, from both
    # va.core:EvidenceLine (transitively) and AmpAscoCapEvidenceLine itself.
    assert "id" in effective
    assert "specifiedBy" in effective
    assert "strengthOfEvidenceProvided" in effective
    assert "targetProposition" in required

    rst = _render_one(proc, "DiagnosticEvidenceLine", tmp_path)
    assert "**Information Model**" in rst
    assert "specifiedBy" in rst
    assert "strengthOfEvidenceProvided" in rst


def _generate_outputs(proc, clean=True):
    """Write the per-class json/ split schemas and def/ .rst docs for a schema.

    ``clean=False`` keeps existing def/ artifacts, needed when two schemas
    (e.g. cat-vrs and recipes) share one output directory.
    """
    split_defs_to_js(proc)  # creates <schema_dir>/json/<Class>
    if clean:
        shutil.rmtree(proc.def_fp, ignore_errors=True)
    os.makedirs(proc.def_fp, exist_ok=True)
    y2t(proc)  # creates <schema_dir>/def/<Class>.rst


def test_gkm_core_outputs_generated():
    """gkm-core must emit json/ and def/ artifacts like vrs does. Because it is
    only pulled in as an import elsewhere, nothing generated these before; the
    scoped suite now drives them directly. Abstract classes are emitted too."""
    proc = YamlSchemaProcessor(GKM_CORE)
    _generate_outputs(proc)
    # concrete class
    assert (proc.json_fp / "Coding").exists()
    assert (proc.def_fp / "Coding.rst").exists()
    # abstract class is emitted as well
    assert (proc.json_fp / "Entity").exists()


def test_vrs_outputs_generated():
    proc = YamlSchemaProcessor(VRS)
    _generate_outputs(proc)
    assert (proc.json_fp / "Allele").exists()
    assert (proc.def_fp / "Allele.rst").exists()
    # abstract class emitted
    assert (proc.json_fp / "Variation").exists()


def test_catvrs_outputs_generated():
    """cat-vrs and recipes live in the same directory and share json/ and def/.
    Generate cat-vrs first (clean), then recipes without wiping cat-vrs's docs."""
    cat = YamlSchemaProcessor(CAT_VRS)
    _generate_outputs(cat, clean=True)
    assert (cat.json_fp / "CategoricalVariant").exists()
    assert (cat.def_fp / "CategoricalVariant.rst").exists()

    rec = YamlSchemaProcessor(RECIPES)
    _generate_outputs(rec, clean=False)
    assert (rec.json_fp / "ProteinSequenceConsequence").exists()
    assert (rec.def_fp / "ProteinSequenceConsequence.rst").exists()
    # cat-vrs docs survived the recipes generation in the shared directory
    assert (cat.def_fp / "CategoricalVariant.rst").exists()


@pytest.mark.parametrize("src", VA_SPEC_ALL, ids=lambda p: str(p.relative_to(root)))
def test_va_spec_builds(src):
    proc = YamlSchemaProcessor(src)
    _assert_no_extends(proc.processed_schema)
    _assert_no_extends(proc.for_js)


def test_va_spec_base_outputs_generated():
    """domain-entities and va-core share va-spec/json and va-spec/def; generate
    domain-entities first (clean), then va-core without wiping its docs."""
    de = YamlSchemaProcessor(DOMAIN_ENTITIES)
    _generate_outputs(de, clean=True)
    assert (de.json_fp / "Condition").exists()
    assert (de.def_fp / "Condition.rst").exists()

    vc = YamlSchemaProcessor(VA_CORE)
    _generate_outputs(vc, clean=False)
    assert (vc.json_fp / "Method").exists()
    assert (vc.def_fp / "Method.rst").exists()
    # domain-entities docs survived va-core generation in the shared directory
    assert (de.def_fp / "Condition.rst").exists()


@pytest.mark.parametrize("src", VA_PROFILES, ids=lambda p: p.name.replace("-profile-source.yaml", ""))
def test_va_spec_profile_outputs_generated(src):
    proc = YamlSchemaProcessor(src)
    _generate_outputs(proc)
    assert proc.json_fp.is_dir() and any(proc.json_fp.iterdir())
    assert proc.def_fp.is_dir() and any(proc.def_fp.iterdir())


def test_profile_def_contains_only_own_classes():
    """A profile's def/XXX holds exactly its own classes -- the same set as
    json/XXX -- not the classes it reaches via import. Those land in the
    shared top-level def/ instead (see y2t.main's own_def_fp/top_def_fp
    split). A regression back to full-closure-per-profile (every class y2t
    can see duplicated into every profile's own subfolder) would pass
    test_va_spec_profile_outputs_generated (it only checks non-empty) but
    fail this.
    """
    proc = YamlSchemaProcessor(root / "data/va-spec/aac-2017-profile-source.yaml")
    _generate_outputs(proc)
    own_json_classes = {p.name for p in proc.json_fp.iterdir()}
    own_def_classes = {p.stem for p in proc.def_fp.glob("*.rst")}
    assert own_def_classes == own_json_classes

    # classes reached only via import (owned by va-core/gkm-core, not
    # aac-2017) must NOT be duplicated into aac-2017's own subfolder...
    assert "EvidenceLine" not in own_def_classes
    assert "Coding" not in own_def_classes
    # ...they land in the shared top-level def/ instead.
    top_def_fp = proc.def_fp.parent
    assert (top_def_fp / "EvidenceLine.rst").exists()
    assert (top_def_fp / "Coding.rst").exists()


def test_cross_references_span_whole_folder_not_just_rendering_source():
    """Used in:/Subclasses: on a shared top-level def/ file (e.g. gkm-core's
    Coding, pulled in by every va-spec profile) must reflect every profile
    that references it, not just whichever profile's y2t run wrote the file
    -- otherwise the list silently depends on build order (see
    METASCHEMA_BEHAVIOR.md History). Generating *only* aac-2017 here must
    still surface acmg-2015's and ccv-2022's own references to Coding, which
    proves the cross-reference computation spans the whole folder
    (y2t._folder_xref_processors), not just aac-2017's own closure
    (y2t._folder_processors). This holds regardless of what any other test
    already wrote to this shared file: render_class overwrites the file (not
    appends), so its content after this call is entirely attributable to
    this call's own computation.
    """
    aac = YamlSchemaProcessor(root / "data/va-spec/aac-2017-profile-source.yaml")
    _generate_outputs(aac)
    coding_rst = (aac.def_fp.parent / "Coding.rst").read_text()
    assert "AmpAscoCapEvidenceLine" in coding_rst  # aac-2017's own reference
    assert "VariantPathogenicityStatement" in coding_rst  # acmg-2015, a sibling
    assert "VariantOncogenicityStatement" in coding_rst  # ccv-2022, a sibling


def _copy_va_spec_data_tree(tmp_path):
    """Copy the whole ``tests/data/`` tree -- not just va-spec -- into an
    isolated ``tmp_path``, preserving the relative sibling layout va-spec's
    imports depend on (``../gkm-core/...``, ``../vrs/...``,
    ``../catvrs/...``). Build artifacts are excluded so each test starts
    from a clean slate. Returns the copied ``va-spec`` directory.
    """
    dest = tmp_path / "data"
    shutil.copytree(root / "data", dest, ignore=shutil.ignore_patterns("build", "json", "def"))
    return dest / "va-spec"


def _run_make(cwd):
    """Run ``make`` in ``cwd`` with this venv's console scripts (source2classes,
    source2splitjs, y2t) on PATH, matching how the real Makefile invokes them.
    """
    env = os.environ.copy()
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    result = subprocess.run(["make"], cwd=cwd, env=env, capture_output=True, text=True)
    assert result.returncode == 0, f"make failed in {cwd}:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"


def _throwaway_class_yaml(name):
    return (
        f"\n  {name}:\n"
        "    maturity: draft\n"
        "    description: throwaway class for a prune.mk regression test\n"
        "    type: object\n"
        "    properties:\n"
        "      note:\n"
        "        type: string\n"
    )


@pytest.mark.skipif(shutil.which("make") is None, reason="make is not available")
def test_prune_removes_stale_profile_class_output(tmp_path):
    """prune.mk must remove a class's stale json/XXX + def/XXX output once
    that class is removed from its own profile source. Runs the REAL
    va-spec Makefile/prune.mk (copied into an isolated tmp_path so it can
    safely mutate a source and rebuild without touching the committed
    fixtures).
    """
    va_spec = _copy_va_spec_data_tree(tmp_path)
    aac = va_spec / "aac-2017-profile-source.yaml"
    original = aac.read_text()

    aac.write_text(original + _throwaway_class_yaml("ZzzThrowawayTestClass"))
    _run_make(va_spec)
    json_out = va_spec / "json/aac-2017/ZzzThrowawayTestClass"
    def_out = va_spec / "def/aac-2017/ZzzThrowawayTestClass.rst"
    assert json_out.exists()
    assert def_out.exists()

    # remove the class and rebuild -- prune.mk must clean up its stale output
    aac.write_text(original)
    _run_make(va_spec)
    assert not json_out.exists()
    assert not def_out.exists()
    # the profile's real classes survived the rebuild + prune
    assert (va_spec / "json/aac-2017/AmpAscoCapEvidenceLine").exists()
    assert (va_spec / "def/aac-2017/AmpAscoCapEvidenceLine.rst").exists()


@pytest.mark.skipif(shutil.which("make") is None, reason="make is not available")
def test_prune_leaves_stale_top_level_def_alone(tmp_path):
    """The shared top-level def/ is intentionally NOT pruned when a class is
    removed from a base source -- it's a regenerated closure, cleared only
    by ``make clean`` (see METASCHEMA_BEHAVIOR.md's prune.mk rationale).
    json/ for the same removed class IS pruned, same as any other class
    removal.
    """
    va_spec = _copy_va_spec_data_tree(tmp_path)
    domain_entities = va_spec / "domain-entities-source.yaml"
    original = domain_entities.read_text()

    domain_entities.write_text(original + _throwaway_class_yaml("ZzzThrowawayBaseClass"))
    _run_make(va_spec)
    json_out = va_spec / "json/ZzzThrowawayBaseClass"
    def_out = va_spec / "def/ZzzThrowawayBaseClass.rst"
    assert json_out.exists()
    assert def_out.exists()

    domain_entities.write_text(original)
    _run_make(va_spec)
    assert not json_out.exists()  # json/ is pruned
    assert def_out.exists()  # def/ is intentionally left stale


@pytest.mark.parametrize("src", VA_PROFILES, ids=lambda p: p.name.replace("-profile-source.yaml", ""))
def test_profile_sub_namespace_routing(src):
    """A ``XXX-profile-source.yaml`` routes its outputs and class $ids through the
    ``XXX`` sub-namespace, nested inside the shared ``json``/``def`` dirs
    (``<parent>/{json,def}/XXX`` and ``.../json/XXX/<Class>``)."""
    xxx = src.name.replace("-profile-source.yaml", "")
    proc = YamlSchemaProcessor(src)
    assert proc.sub_namespace == xxx
    assert proc.json_fp == src.parent / "json" / xxx
    assert proc.def_fp == src.parent / "def" / xxx
    some_class = next(iter(proc.for_js["$defs"]))
    assert f"/json/{xxx}/{some_class}" in proc.get_class_uri(some_class, "json")


def test_profile_ref_to_base_class_omits_sub_namespace(tmp_path):
    """A profile class's $ref to a class owned by a non-profile source (e.g.
    va-core's EvidenceLine) must resolve to that source's own path, with no
    sub-namespace segment injected -- the sub-namespace belongs to the
    referencing profile, not to a class the profile doesn't own."""
    proc = YamlSchemaProcessor(root / "data/va-spec/aac-2017-profile-source.yaml")
    split_defs_to_js(proc)
    amp = json.loads((proc.json_fp / "AmpAscoCapEvidenceLine").read_text())
    evidence_line_ref = amp["allOf"][0]["$ref"]
    assert evidence_line_ref.endswith("/json/EvidenceLine")
    assert "/aac-2017/" not in evidence_line_ref


def test_profile_ref_to_sibling_class_includes_sub_namespace(tmp_path):
    """A profile class's $ref to another class defined in the *same* profile
    source must include the profile's own sub-namespace segment, matching how
    that sibling class's own json/ output is routed."""
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://example.org/schema/xxxprof/1.0.0/xxxprof-profile-source.yaml",
        "title": "XxxProf",
        "type": "object",
        "$defs": {
            "First": {
                "maturity": "draft",
                "description": "first",
                "type": "object",
                "properties": {"second": {"$ref": "#/$defs/Second"}},
            },
            "Second": {
                "maturity": "draft",
                "description": "second",
                "type": "object",
                "properties": {"name": {"type": "string"}},
            },
        },
    }
    fp = tmp_path / "xxxprof-profile-source.yaml"
    with open(fp, "w") as f:
        yaml.safe_dump(schema, f)
    proc = YamlSchemaProcessor(fp)
    split_defs_to_js(proc)
    first = json.loads((proc.json_fp / "First").read_text())
    second_ref = first["properties"]["second"]["$ref"]
    assert second_ref.endswith("/json/xxxprof/Second")


def test_profile_sub_namespace_mismatch_raises(tmp_path):
    """If the filename's XXX and the $id's final segment disagree, raise."""
    fp = tmp_path / "xyz-profile-source.yaml"
    fp.write_text(
        '$schema: "https://json-schema.org/draft/2020-12/schema"\n'
        '$id: "https://w3id.org/ga4gh/schema/va-spec/1.0.0-msp.test/WRONG-profile-source.yaml"\n'
        "title: X\n"
        "$defs: {}\n"
    )
    with pytest.raises(ValueError, match="sub-namespace"):
        YamlSchemaProcessor(fp)


def test_same_name_override_merges_inherited_attributes():
    """Allele.type specializes the inherited ``type`` property by name.

    The subclass supplies ``const``/``default``/``description`` while the
    inherited ``type: string`` (from Ga4ghIdentifiableObject) is preserved
    via the auto-merge that replaced ``extends``.
    """
    proc = YamlSchemaProcessor(VRS)
    allele_type = proc.defs["Allele"]["properties"]["type"]
    assert allele_type["const"] == "Allele"
    assert allele_type["default"] == "Allele"
    # inherited attribute retained through the name-based merge
    assert allele_type["type"] == "string"


def test_property_order_is_superclass_first():
    """Emitted properties follow the inheritance chain: top-level superclass
    properties first, each descendant appending its own, with overridden
    properties keeping their inherited position."""
    proc = YamlSchemaProcessor(VRS)
    order = list(proc.for_js["$defs"]["Allele"]["properties"].keys())
    # Entity (top-level superclass) contributes id, type, ... first.
    assert order[0] == "id"
    assert order[1] == "type"  # overridden by Allele but keeps Entity's position
    # digest (Ga4ghIdentifiableObject) and expressions (Variation) come before
    # Allele's own location/state, which are appended last.
    assert order.index("digest") < order.index("location")
    assert order.index("expressions") < order.index("location")
    assert order[-2:] == ["location", "state"]


def test_abstract_classes_are_emitted_as_object_schemas():
    """Every class, abstract included, is emitted with type: object and no
    `inherits` leaking into output. Abstract classes carry `abstract: true`
    so a consumer of the per-class JSON can tell them apart from concrete
    classes without cross-referencing the source YAML; concrete classes omit
    the key entirely rather than carrying `abstract: false`."""
    proc = YamlSchemaProcessor(VRS)
    defs = proc.for_js["$defs"]
    for abstract_cls in ("Ga4ghIdentifiableObject", "Variation", "Location"):
        assert abstract_cls in defs, f"{abstract_cls} should be emitted"
        emitted = defs[abstract_cls]
        assert emitted.get("type") == "object"
        assert emitted.get("abstract") is True
        assert "inherits" not in emitted

    concrete = defs["Allele"]
    assert "abstract" not in concrete


def test_ref_to_abstract_class_stays_direct():
    """A $ref to an abstract class is NOT expanded into a oneOf of concrete
    descendants; it remains a direct reference.

    Allele.state references the abstract SequenceExpression. The old
    concretization would have replaced the $ref with a oneOf of the three
    concrete SequenceExpression subclasses.
    """
    proc = YamlSchemaProcessor(VRS)
    state = proc.for_js["$defs"]["Allele"]["properties"]["state"]
    assert state.get("$ref") == "#/$defs/SequenceExpression"
    assert "oneOf" not in state


def test_renaming_inherited_property_is_rejected(tmp_path):
    """A subclass that uses ``extends`` to rename a property must error."""
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://example.org/schema/test/test-source.yaml",
        "title": "Test",
        "type": "object",
        "$defs": {
            "Parent": {
                "maturity": "draft",
                "description": "parent",
                "heritableProperties": {
                    "subject": {"type": "string", "description": "the subject"},
                },
            },
            "Child": {
                "maturity": "draft",
                "inherits": "Parent",
                "description": "child",
                "type": "object",
                "properties": {
                    "variant": {"extends": "subject", "description": "renamed"},
                },
            },
        },
    }
    fp = tmp_path / "test-source.yaml"
    with open(fp, "w") as f:
        yaml.safe_dump(schema, f)

    with pytest.raises(ValueError, match="extends"):
        YamlSchemaProcessor(fp)
