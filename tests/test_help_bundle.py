import json
from pathlib import Path

from minios_gui import load_localized_document, validate_document


ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "help"
BUNDLE = ROOT / "share" / "help"


def test_every_markdown_source_has_compiled_document():
    sources = sorted(SOURCE.rglob("*.md"))
    assert sources
    for source in sources:
        relative = source.relative_to(SOURCE).with_suffix(".json")
        compiled = BUNDLE / relative
        assert compiled.is_file(), relative
        validate_document(json.loads(compiled.read_text(encoding="utf-8")))
    assert not list(BUNDLE.rglob("*.md"))


def test_help_manifest_matches_compiled_files():
    manifest = json.loads((BUNDLE / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["product_kind"] == "minios-image-builder-help"
    assert manifest["schema_version"] == 1
    assert len(manifest["documents"]) == len(list(SOURCE.rglob("*.md")))
    for item in manifest["documents"]:
        assert (BUNDLE / item["compiled"]).is_file()


def test_localized_document_loader_keeps_locale_fallback():
    brazil = load_localized_document(
        BUNDLE, "boot-menu/overview.json", locale_name="pt_BR.UTF-8")
    english = load_localized_document(
        BUNDLE, "boot-menu/overview.json", locale_name="missing_LOCALE")
    assert brazil["nodes"]
    assert english["nodes"]
    assert brazil != english
