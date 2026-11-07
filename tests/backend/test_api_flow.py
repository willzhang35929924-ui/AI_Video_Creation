from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[2]


def assert_status(response, expected: int):
    assert response.status_code == expected, response.text
    return response.json()


def create_parsed_project(client: TestClient) -> tuple[str, list[dict]]:
    raw = (ROOT / "fixtures" / "demo_script.md").read_text(encoding="utf-8")
    project = assert_status(
        client.post(
            "/api/imports/text",
            json={"name": "雨夜归还：API 测试", "text": raw, "source_type": "markdown"},
        ),
        201,
    )
    parsed = assert_status(client.post(f"/api/projects/{project['id']}/parse"), 200)
    assert parsed["shot_count"] == 3
    shots = assert_status(client.get(f"/api/projects/{project['id']}/shots"), 200)
    return project["id"], shots


def create_approved_character(client: TestClient) -> tuple[str, str]:
    character = assert_status(
        client.post(
            "/api/characters",
            json={
                "id": "hero_test",
                "slug": "hero_test",
                "display_name": "林岚测试",
                "description": "虚构测试人物",
            },
        ),
        201,
    )
    version = assert_status(
        client.post(
            f"/api/characters/{character['id']}/versions",
            json={
                "identity": {
                    "fixed_identity": "自然椭圆脸、深棕色短发、左眼下方小痣",
                    "default_clothing": "鼠尾草绿风衣",
                    "negative_prompt": "不要改变身份、发型、痣的位置和衣服",
                }
            },
        ),
        201,
    )
    assert_status(
        client.post(
            f"/api/characters/{character['id']}/versions/{version['id']}/approve",
            json={},
        ),
        200,
    )
    return character["id"], version["id"]


def test_health_openapi_and_demo_provider_boundaries(client: TestClient) -> None:
    health = assert_status(client.get("/api/health"), 200)
    assert health["status"] == "ok"
    assert health["mode"] == "demo"
    assert health["real_model_verified"] is False

    provider = assert_status(client.get("/api/providers/h3/health"), 200)
    assert provider["provider"] == "demo"
    assert provider["mode"] == "demo_watermarked_video"
    assert provider["reference_to_video_available"] is False

    capabilities = assert_status(client.get("/api/providers/h3/capabilities"), 200)
    assert capabilities["real_model_verified"] is False
    assert capabilities["demo_disclaimer"]
    assert_status(client.get("/openapi.json"), 200)


def test_full_persistent_demo_workflow_and_dry_run(client: TestClient) -> None:
    project_id, shots = create_parsed_project(client)

    gate = client.post(f"/api/shots/{shots[0]['id']}/generate", json={})
    assert gate.status_code == 409
    assert gate.json()["error"]["code"] == "script_not_approved"

    assert_status(client.post(f"/api/projects/{project_id}/script/approve", json={}), 200)
    character_id, version_id = create_approved_character(client)
    for shot in shots:
        assigned = assert_status(
            client.post(
                f"/api/shots/{shot['id']}/assign-character",
                json={"character_id": character_id, "character_version_id": version_id},
            ),
            200,
        )
        assert assigned["character_asset_version_id"] == version_id
    assert_status(client.post(f"/api/projects/{project_id}/shots/approve-all", json={}), 200)

    generated = assert_status(client.post(f"/api/shots/{shots[0]['id']}/generate", json={"seed": 42}), 202)
    job = generated["job"]
    video = generated["video_revision"]
    assert job["status"] == "succeeded"
    assert job["provider"] == "demo"
    assert video["relative_path"].endswith(".mp4")
    assert video["metadata_json"]["playable_video"] is True
    assert video["metadata_json"]["watermark_burned_in"] is True
    assert video["metadata_json"]["disclaimer"] == "模拟流程通过；真实模型未验证"

    replay = client.get(f"/api/media/{video['relative_path']}")
    assert replay.status_code == 200
    assert replay.headers["content-type"].startswith("video/mp4")
    assert b"ftyp" in replay.content[:32]

    checks = assert_status(client.post(f"/api/videos/{video['id']}/quality-check"), 200)
    statuses = {item["check_type"]: item["status"] for item in checks["checks"]}
    assert statuses["file_readable"] == "passed"
    assert statuses["media_decode"] in {"passed", "not_available"}
    assert checks["automatic_check_only"] is True

    approved = assert_status(client.post(f"/api/videos/{video['id']}/approve", json={}), 200)
    assert approved["review_status"] == "approved"

    revised = assert_status(
        client.post(
            f"/api/videos/{video['id']}/revise",
            json={
                "instruction": "保持脸型、短发、左眼下方小痣和风衣不变，镜头推进更缓慢。",
                "seed": 43,
            },
        ),
        202,
    )
    newer = revised["video_revision"]
    assert newer["id"] != video["id"]
    assert newer["revision"] == video["revision"] + 1
    assert revised["editing_semantics"].startswith("基于相同人物参考重新生成")
    history = assert_status(client.get(f"/api/videos/{newer['id']}"), 200)["history"]
    assert len(history) == 2

    assert_status(client.post(f"/api/videos/{newer['id']}/quality-check"), 200)
    assert_status(client.post(f"/api/videos/{newer['id']}/approve", json={}), 200)

    account = assert_status(
        client.post(
            "/api/publishing/accounts",
            json={"platform": "douyin", "display_name": "抖音占位测试账号", "auth_status": "placeholder"},
        ),
        201,
    )
    preset = assert_status(
        client.post(
            "/api/publishing/presets",
            json={
                "account_id": account["id"],
                "name": "竖屏私密预设",
                "title_template": "雨夜归还",
                "caption_template": "人物一致性验证",
                "hashtags": ["原创角色", "短片"],
                "visibility": "private",
            },
        ),
        201,
    )
    persisted = assert_status(client.get(f"/api/publishing/presets?account_id={account['id']}"), 200)
    assert persisted[0]["id"] == preset["id"]

    batch = assert_status(
        client.post(
            "/api/publishing/batches/dry-run",
            json={
                "items": [
                    {
                        "video_revision_id": newer["id"],
                        "account_id": account["id"],
                        "preset_id": preset["id"],
                    }
                ]
            },
        ),
        201,
    )
    assert batch["dry_run"] is True
    assert batch["items_json"][0]["external_publish_calls"] == 0
    assert batch["items_json"][0]["action"] == "would_validate_only"
    assert "未调用任何平台发布接口" in batch["notice"]

    audit = assert_status(client.get("/api/audit-events"), 200)
    actions = {entry["action"] for entry in audit}
    assert {"parse_script", "approved", "submitted", "quality_check", "dry_run_validated"} <= actions


def test_approved_shot_edit_invalidates_approval_and_video(client: TestClient) -> None:
    project_id, shots = create_parsed_project(client)
    assert_status(client.post(f"/api/projects/{project_id}/script/approve", json={}), 200)
    character_id, version_id = create_approved_character(client)
    assert_status(
        client.post(
            f"/api/shots/{shots[0]['id']}/assign-character",
            json={"character_id": character_id, "character_version_id": version_id},
        ),
        200,
    )
    assert_status(client.post(f"/api/shots/{shots[0]['id']}/approve", json={}), 200)
    generated = assert_status(client.post(f"/api/shots/{shots[0]['id']}/generate", json={}), 202)
    video_id = generated["video_revision"]["id"]

    changed = assert_status(
        client.patch(f"/api/shots/{shots[0]['id']}", json={"visual_prompt": "修改后的画面提示"}),
        200,
    )
    assert changed["status"] == "draft"
    assert changed["revision"] == 2
    assert_status(client.get(f"/api/videos/{video_id}"), 200)["review_status"] == "needs_regeneration"


def test_character_reassignment_invalidates_old_video_and_blocks_reapproval(client: TestClient) -> None:
    project_id, shots = create_parsed_project(client)
    assert_status(client.post(f"/api/projects/{project_id}/script/approve", json={}), 200)
    character_id, version_id = create_approved_character(client)
    shot_id = shots[0]["id"]
    assert_status(
        client.post(
            f"/api/shots/{shot_id}/assign-character",
            json={"character_id": character_id, "character_version_id": version_id},
        ),
        200,
    )
    assert_status(client.post(f"/api/shots/{shot_id}/approve", json={}), 200)
    video = assert_status(client.post(f"/api/shots/{shot_id}/generate", json={}), 202)[
        "video_revision"
    ]
    assert_status(client.post(f"/api/videos/{video['id']}/quality-check"), 200)
    assert_status(client.post(f"/api/videos/{video['id']}/approve", json={}), 200)

    version_2 = assert_status(
        client.post(
            f"/api/characters/{character_id}/versions",
            json={
                "identity": {
                    "fixed_identity": "林岚身份档案第二版",
                    "default_clothing": "鼠尾草绿风衣",
                }
            },
        ),
        201,
    )
    assert_status(
        client.post(
            f"/api/characters/{character_id}/versions/{version_2['id']}/approve",
            json={},
        ),
        200,
    )
    reassigned = assert_status(
        client.post(
            f"/api/shots/{shot_id}/assign-character",
            json={
                "character_id": character_id,
                "character_version_id": version_2["id"],
            },
        ),
        200,
    )
    assert reassigned["status"] == "draft"
    stale = assert_status(client.get(f"/api/videos/{video['id']}"), 200)
    assert stale["review_status"] == "needs_regeneration"

    blocked = client.post(f"/api/videos/{video['id']}/approve", json={})
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "video_needs_regeneration"


def test_docx_upload_asset_hash_and_path_protection(client: TestClient) -> None:
    with (ROOT / "fixtures" / "demo_script.docx").open("rb") as handle:
        imported = assert_status(
            client.post(
                "/api/imports/file",
                data={"name": "DOCX 导入"},
                files={"file": ("demo_script.docx", handle, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
            ),
            201,
        )
    assert "分镜 2" in imported["source_text"]
    assert not Path(imported["source_uri"]).is_absolute()

    character_id, _approved_version_id = create_approved_character(client)
    # Approved versions are immutable. Upload new material to a fresh draft
    # version instead of silently mutating the approved identity snapshot.
    draft_version = assert_status(
        client.post(
            f"/api/characters/{character_id}/versions",
            json={"identity": {"fixed_identity": "素材哈希测试版本"}},
        ),
        201,
    )
    version_id = draft_version["id"]
    from io import BytesIO
    from PIL import Image
    buffer = BytesIO()
    Image.new("RGB", (16, 16), "gray").save(buffer, format="PNG")
    png = buffer.getvalue()
    asset = assert_status(
        client.post(
            f"/api/characters/{character_id}/assets",
            data={
                "version_id": version_id,
                "asset_type": "front_face",
                "tags": "正脸,测试",
                "is_primary_reference": "true",
                "approve": "true",
            },
            files={"file": ("face.png", png, "image/png")},
        ),
        201,
    )
    assert len(asset["sha256"]) == 64
    assert asset["status"] == "approved"
    assert asset["is_primary_reference"] is True
    asset_response = client.get(f"/api/media/{asset['relative_path']}")
    assert asset_response.status_code == 200
    assert asset_response.content == png

    traversal = client.get("/api/media/%2e%2e/%2e%2e/backend/requirements.txt")
    assert traversal.status_code in {400, 404}


def test_publish_dry_run_rejects_unapproved_video(client: TestClient) -> None:
    project_id, shots = create_parsed_project(client)
    assert_status(client.post(f"/api/projects/{project_id}/script/approve", json={}), 200)
    character_id, version_id = create_approved_character(client)
    assert_status(client.post(f"/api/shots/{shots[0]['id']}/assign-character", json={"character_id": character_id, "character_version_id": version_id}), 200)
    assert_status(client.post(f"/api/shots/{shots[0]['id']}/approve", json={}), 200)
    video = assert_status(client.post(f"/api/shots/{shots[0]['id']}/generate", json={}), 202)["video_revision"]
    account = assert_status(client.post("/api/publishing/accounts", json={"platform": "kuaishou", "display_name": "占位账号"}), 201)
    rejected = client.post("/api/publishing/batches/dry-run", json={"items": [{"video_revision_id": video["id"], "account_id": account["id"]}]})
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "video_not_approved"


def test_shot_reject_and_soft_delete_preserve_records(client: TestClient) -> None:
    project_id, shots = create_parsed_project(client)
    assert_status(client.post(f"/api/projects/{project_id}/script/approve", json={}), 200)

    rejected = assert_status(
        client.post(
            f"/api/shots/{shots[0]['id']}/reject",
            json={"reason": "动作描述需要调整"},
        ),
        200,
    )
    assert rejected["status"] == "rejected"
    assert rejected["rejection_reason"] == "动作描述需要调整"

    deleted = assert_status(client.delete(f"/api/shots/{shots[1]['id']}"), 200)
    assert deleted == {
        "id": shots[1]["id"],
        "status": "deleted",
        "deleted": True,
        "history_preserved": True,
    }
    visible = assert_status(client.get(f"/api/projects/{project_id}/shots"), 200)
    assert {shot["id"] for shot in visible} == {shots[0]["id"], shots[2]["id"]}
    all_shots = assert_status(
        client.get(f"/api/projects/{project_id}/shots?include_deleted=true"), 200
    )
    assert len(all_shots) == 3


def test_publishing_account_soft_remove_clears_credential_reference(client: TestClient) -> None:
    account = assert_status(
        client.post(
            "/api/publishing/accounts",
            json={
                "platform": "xiaohongshu",
                "display_name": "待移除账号",
                "auth_status": "placeholder",
                "credential_reference": "env:XHS_TEST_TOKEN",
            },
        ),
        201,
    )
    removed = assert_status(
        client.delete(f"/api/publishing/accounts/{account['id']}"), 200
    )
    assert removed["removed"] is True
    assert removed["presets_preserved"] is True
    assert assert_status(client.get("/api/publishing/accounts"), 200) == []
    archived = assert_status(
        client.get("/api/publishing/accounts?include_removed=true"), 200
    )
    assert archived[0]["auth_status"] == "removed"
    assert "credential_reference" not in archived[0]

