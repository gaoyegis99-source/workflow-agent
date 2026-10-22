import pytest
from platform_contracts.knowledge import EvidenceRef, KnowledgeImportCreate, KnowledgeSearch
from platform_contracts.science import DocumentUploadFinalize, UploadCreate
from platform_knowledge.domain import normalize, segment
from pydantic import ValidationError

DIGEST = "sha256:" + "a" * 64
ASSET = {
    "project_id": "project",
    "asset_id": "asset",
    "version_id": "1",
    "version_manifest_digest": DIGEST,
}


def test_document_and_knowledge_contracts_are_strict():
    UploadCreate(filename="guide.md", format="markdown", size=12, digest=DIGEST)
    DocumentUploadFinalize(kind="DOCUMENT")
    KnowledgeImportCreate(source=ASSET)
    KnowledgeSearch(query="地下水", limit=20)
    EvidenceRef(
        project_id="project",
        document_id="document",
        version_id="1",
        unit_id="unit",
        content_digest=DIGEST,
        start_char=0,
        end_char=1,
        exact_text_digest=DIGEST,
        normalization="utf8-nfc-lf-v1",
    )
    with pytest.raises(ValidationError):
        KnowledgeSearch(query=" ")
    with pytest.raises(ValidationError):
        KnowledgeSearch(query="valid", limit=21)
    with pytest.raises(ValidationError):
        KnowledgeSearch(query="line\nbreak")
    with pytest.raises(ValidationError):
        DocumentUploadFinalize(kind="SPATIAL")
    with pytest.raises(ValidationError):
        KnowledgeImportCreate(source=ASSET, representation_id="normalized")


def test_normalization_and_segmentation_are_deterministic_and_exact():
    raw = b"\xef\xbb\xbf# Heading\r\n\r\nCafe\xcc\x81 groundwater\rparagraph\r\n"
    text = normalize(raw)
    assert text == "# Heading\n\nCafé groundwater\nparagraph\n"
    first = segment(text)
    assert first == segment(text)
    assert [text[item["start_char"] : item["end_char"]] for item in first] == [
        item["text"] for item in first
    ]
    assert [item["text"] for item in first] == ["# Heading\n", "Café groundwater\nparagraph\n"]


def test_long_paragraph_uses_fixed_overlap_and_rejects_binary_text():
    text = "x" * 4_500
    units = segment(text)
    assert [(item["start_char"], item["end_char"]) for item in units] == [
        (0, 2_000),
        (1_800, 3_800),
        (3_600, 4_500),
    ]
    with pytest.raises(ValueError):
        normalize(b"text\x00binary")
    with pytest.raises(ValueError):
        normalize(b" \n\t")
