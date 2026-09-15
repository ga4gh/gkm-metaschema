import io
import json
import os
import re
import shutil
from pathlib import Path

import pytest
import yaml

from ga4gh.gkm.metaschema.scripts.source2classes import main as s2c
from ga4gh.gkm.metaschema.scripts.y2t import main as y2t
from ga4gh.gkm.metaschema.tools.source_proc import YamlSchemaProcessor

root = Path(__file__).parent

processor = YamlSchemaProcessor(root / "data/vrs/vrs-source.yaml")
# Round-trip the processed schema through YAML in memory (no file artifact).
_yaml_buffer = io.StringIO()
processor.js_yaml_dump(_yaml_buffer)
target = yaml.load(_yaml_buffer.getvalue(), Loader=yaml.SafeLoader)


def test_mv_is_passthrough():
    assert processor.class_is_passthrough("MolecularVariation")


def test_se_not_passthrough():
    assert not processor.class_is_passthrough("SequenceExpression")


def test_class_is_subclass():
    # Allele inherits Variation (directly), so it is a subclass of Variation
    # but not of the unrelated Location hierarchy.
    assert processor.class_is_subclass("Allele", "Variation")
    assert not processor.class_is_subclass("Allele", "Location")


def test_yaml_target_match():
    d2 = processor.for_js
    assert d2 == target


def test_merged_create():
    p = YamlSchemaProcessor(root / "data/vrs/vrs-source.yaml")
    p.merge_imported()
    assert True


def test_merged_create_diamond_import():
    """``recipes-source.yaml`` imports cat-vrs (which imports gkm-core + vrs)
    *and* gkm-core/vrs directly -- gkm-core is reached via two different
    routes (a diamond). ``merge_imported()`` must flatten this without
    raising, with no unresolved curie leaking and every $ref made local.
    """
    p = YamlSchemaProcessor(root / "data/catvrs/recipes-source.yaml")
    p.merge_imported()

    # gkm-core, reached via two different import paths, is merged exactly
    # once (a dict key can't duplicate; this just confirms it made it in).
    assert "Coding" in p.for_js["$defs"]
    # every class from the whole transitive closure is present
    for cls in ("CategoricalVariant", "DefiningAlleleConstraint", "Allele", "GeneFusion"):
        assert cls in p.for_js["$defs"]

    doc = json.dumps(p.for_js)
    assert "Curie" not in doc, "an unresolved *Curie key leaked into the merged document"
    for match in re.finditer(r'"\$ref":\s*"([^"]+)"', doc):
        assert match.group(1).startswith("#/"), f"non-local $ref survived the merge: {match.group(1)}"


def test_class_create():
    s2c(processor)
    assert True


def test_docs_create():
    defs = processor.def_fp
    shutil.rmtree(defs, ignore_errors=True)
    os.makedirs(defs)
    y2t(processor)
    assert True


if __name__ == "__main__":
    pytest.main([__file__])
