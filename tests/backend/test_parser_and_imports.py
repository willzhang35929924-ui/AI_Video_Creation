from __future__ import annotations

import socket
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.errors import AppError
from app.services.imports import parse_uploaded_document, validate_url_target
from app.services.parser import DeterministicScriptParser


ROOT = Path(__file__).resolve().parents[2]


def test_demo_script_parses_three_structured_shots() -> None:
    raw = (ROOT / "fixtures" / "demo_script.md").read_text(encoding="utf-8")
    result = DeterministicScriptParser().parse(raw)

    assert result["title"] == "雨夜归还：人物一致性验证"
    assert [shot["order"] for shot in result["shots"]] == [1, 2, 3]
    assert result["shots"][0]["duration_seconds"] == 5
    assert result["shots"][0]["camera_move"] == "缓慢推进"
    assert "深蓝色笔记本" in result["shots"][1]["visual_prompt"]
    assert "左眼下方小痣" in result["shots"][2]["visual_prompt"]


def test_parser_falls_back_to_paragraphs_and_preserves_content() -> None:
    raw = "第一段没有分镜标签。\n\n第二段仍应保留。"
    result = DeterministicScriptParser().parse(raw)
    assert len(result["shots"]) == 2
    assert "第一段" in result["shots"][0]["narration"]
    assert result["warnings"]


def test_text_and_docx_importers_extract_visible_content() -> None:
    md_data = (ROOT / "fixtures" / "demo_script.md").read_bytes()
    md_text, md_mime = parse_uploaded_document("demo_script.md", md_data, 2_000_000)
    assert md_mime == "text/markdown"
    assert "分镜 3" in md_text

    docx_data = (ROOT / "fixtures" / "demo_script.docx").read_bytes()
    docx_text, docx_mime = parse_uploaded_document("demo_script.docx", docx_data, 2_000_000)
    assert docx_mime.endswith("wordprocessingml.document")
    assert "雨夜街口" in docx_text
    assert "咖啡店窗边" in docx_text


@pytest.mark.parametrize(
    ("filename", "data", "expected_code"),
    [
        ("empty.md", b"", "empty_document"),
        ("fake.docx", b"not-a-zip", "invalid_docx"),
        ("binary.txt", b"hello\x00world", "invalid_text_document"),
        ("script.exe", b"MZ", "unsupported_document_type"),
    ],
)
def test_importer_rejects_invalid_files(filename: str, data: bytes, expected_code: str) -> None:
    with pytest.raises(AppError) as raised:
        parse_uploaded_document(filename, data, 2_000_000)
    assert raised.value.code == expected_code


def test_url_import_is_disabled_by_default() -> None:
    with pytest.raises(AppError) as raised:
        validate_url_target("https://example.com/doc", Settings(allow_external_doc_import=False))
    assert raised.value.code == "url_import_disabled"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/doc",
        "http://localhost/doc",
        "http://169.254.169.254/latest/meta-data",
        "file:///etc/passwd",
        "http://10.0.0.2/doc",
    ],
)
def test_url_validator_blocks_ssrf_targets(url: str) -> None:
    settings = Settings(allow_external_doc_import=True)
    with pytest.raises(AppError) as raised:
        validate_url_target(url, settings)
    assert raised.value.code in {"unsafe_url", "invalid_url"}


def test_url_validator_enforces_allowlist_and_dns_rebinding_check() -> None:
    settings = Settings(
        allow_external_doc_import=True,
        doc_import_allowed_hosts=("docs.example.test",),
    )

    def public_resolver(*_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    assert validate_url_target("https://docs.example.test/page", settings, public_resolver) == "docs.example.test"

    with pytest.raises(AppError) as not_allowed:
        validate_url_target("https://example.test/page", settings, public_resolver)
    assert not_allowed.value.code == "host_not_allowed"

    def private_resolver(*_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.2", 443))]

    with pytest.raises(AppError) as rebound:
        validate_url_target("https://docs.example.test/page", settings, private_resolver)
    assert rebound.value.code == "unsafe_url"

