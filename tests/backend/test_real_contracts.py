from io import BytesIO
import asyncio

from PIL import Image
from sqlalchemy import select

from app.models import SourceDocument
from app.services.providers import ComfyUIH3Provider
from app.services.imports import pin_public_url
from app.core.errors import AppError
import pytest


def character_version(client):
    character = client.post("/api/characters", json={"id": "contract_hero", "slug": "contract_hero", "display_name": "测试角色"}).json()
    version = client.post(f"/api/characters/{character['id']}/versions", json={"identity": {"fixed_identity": "成年原创角色"}}).json()
    return character, version


def test_real_character_cannot_approve_empty_version(client):
    character, version = character_version(client)
    client.app.state.settings.h3_demo_mode = False
    result = client.post(f"/api/characters/{character['id']}/versions/{version['id']}/approve", json={})
    assert result.status_code == 409
    assert result.json()["error"]["code"] == "approved_primary_required"


def test_header_only_png_is_rejected_by_actual_decoder(client):
    character, version = character_version(client)
    result = client.post(f"/api/characters/{character['id']}/assets", data={"version_id": version["id"], "asset_type": "front_face"}, files={"file": ("bad.png", b"\x89PNG\r\n\x1a\nnot-an-image", "image/png")})
    assert result.status_code == 422
    assert result.json()["error"]["code"] == "image_decode_failed"


def test_reference_asset_ids_are_frozen_in_shot(client):
    character, version = character_version(client)
    image = BytesIO()
    Image.new("RGB", (32, 48), "#777777").save(image, format="PNG")
    asset = client.post(f"/api/characters/{character['id']}/assets", data={"version_id": version["id"], "asset_type": "front_face", "is_primary_reference": "true", "approve": "true"}, files={"file": ("face.png", image.getvalue(), "image/png")}).json()
    assert client.post(f"/api/characters/{character['id']}/versions/{version['id']}/approve", json={}).status_code == 200
    project = client.post("/api/projects", json={"name": "Snapshot contract"}).json()
    shot = client.post(f"/api/projects/{project['id']}/shots", json={"order": 1, "scene_title": "A"}).json()
    assigned = client.post(f"/api/shots/{shot['id']}/assign-character", json={"character_id": character["id"], "character_version_id": version["id"], "asset_ids": [asset["id"]]}).json()
    assert assigned["reference_asset_ids_json"] == [asset["id"]]
    assert client.post(f"/api/assets/{asset['id']}/review", json={"status": "needs_review", "reason": "cannot mutate released identity"}).status_code == 409
    assert client.patch(f"/api/characters/{character['id']}/versions/{version['id']}", json={"identity": {"fixed_identity": "changed identity"}}).status_code == 409
    assert client.post(f"/api/characters/{character['id']}/versions", json={"identity": {"fixed_identity": "new identity"}}).status_code == 201
    persisted = client.get(f"/api/projects/{project['id']}/shots").json()[0]
    assert persisted["character_asset_version_id"] == version["id"]
    assert persisted["reference_asset_ids_json"] == [asset["id"]]


def test_source_document_snapshot_is_persisted(client):
    project = client.post("/api/imports/text", json={"name": "Source contract", "text": "An original source document."}).json()
    with client.app.state.session_factory() as session:
        source = session.scalar(select(SourceDocument).where(SourceDocument.project_id == project["id"]))
        assert source.original_text == "An original source document."
        assert len(source.sha256) == 64
    assert client.get(f"/api/projects/{project['id']}/script").json() is None


def test_provider_mode_mismatch_never_falls_back(client):
    project = client.post("/api/projects", json={"name": "Mode boundary"}).json()
    shot = client.post(f"/api/projects/{project['id']}/shots", json={"order": 1}).json()
    response = client.post(f"/api/shots/{shot['id']}/generate", json={"parameters": {"requested_mode": "real"}})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "provider_mode_mismatch"


def test_real_offline_socket_reports_disconnected(settings):
    settings.h3_demo_mode = False
    settings.comfyui_base_url = "http://127.0.0.1:1"
    settings.comfyui_client_timeout_seconds = 0.2
    result = asyncio.run(ComfyUIH3Provider(settings).health())
    assert result.connected is False
    assert result.ready is False
    assert result.reference_to_video_available is False


def test_composition_rejects_duplicate_clips_before_tool_call(client):
    project = client.post("/api/projects", json={"name": "No duplicate media"}).json()
    result = client.post(f"/api/projects/{project['id']}/compositions", json={"video_ids": ["same", "same"]})
    assert result.status_code == 422
    assert result.json()["error"]["code"] == "duplicate_clip"


def test_url_connection_pins_public_ip_and_preserves_tls_host(settings):
    settings.allow_external_doc_import = True
    settings.doc_import_allowed_hosts = ("docs.example.com",)
    resolver = lambda *_args, **_kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))]
    target, sni, host = pin_public_url("https://docs.example.com/path?a=1", settings, resolver)
    assert target == "https://93.184.216.34/path?a=1"
    assert sni == host == "docs.example.com"


def test_dns_rebinding_between_validation_and_connection_is_rejected(settings):
    settings.allow_external_doc_import = True
    settings.doc_import_allowed_hosts = ("docs.example.com",)
    answers = iter(["93.184.216.34", "127.0.0.1"])
    def resolver(*_args, **_kwargs):
        return [(2, 1, 6, "", (next(answers), 443))]
    with pytest.raises(AppError) as error:
        pin_public_url("https://docs.example.com/doc", settings, resolver)
    assert error.value.code == "unsafe_url"
