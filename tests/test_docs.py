from pathlib import Path

from manyeyes.config import reference_markdown


def test_configuration_reference_is_current():
    doc = (Path(__file__).parent.parent / "docs" / "configuration.md").read_text()
    assert reference_markdown() in doc, "run: manyeyes config --reference > update docs/configuration.md"


def test_configuration_doc_keeps_its_introduction():
    doc = (Path(__file__).parent.parent / "docs" / "configuration.md").read_text()
    assert doc.startswith("# ") and "<!-- generated: manyeyes config --reference -->" in doc
