from app.api.routes import build_prompt
from app.models import CharacterVersion, StoryboardShot
from app.services.parser import DeterministicScriptParser


def test_structured_identity_does_not_inherit_lan_features():
    version = CharacterVersion(character_id="wenlan", version=1, identity_json={
        "fixed_identity": "Wen Lan, adult coastal acoustic engineer",
        "profile": {"hair": "silver-blond undercut", "identifying_marks": "birthmark behind right ear", "wardrobes": [{"id": "default", "description": "grey work shirt"}]},
    })
    shot = StoryboardShot(project_id="project", order=1, scene_title="Clock", visual_prompt="Looks at a clock", action="Raises eyes", negative_prompt="extra people")
    prompt, negative = build_prompt(shot, version, None)
    assert "silver-blond undercut" in prompt and "birthmark behind right ear" in prompt
    assert "grey work shirt" in prompt and "<Picture 1>" in prompt
    assert "sage green" not in prompt and "below her left eye" not in prompt
    assert negative == "extra people"


def test_legacy_identity_and_revision_still_preserved():
    version = CharacterVersion(character_id="hero_lan_001", version=1, identity_json={"fixed_identity": "林岚，左眼下小痣，黑色齐肩发", "default_clothing": "鼠尾草绿风衣", "negative_prompt": "wrong face"})
    shot = StoryboardShot(project_id="project", order=1, scene_title="Door", visual_prompt="Opens door", negative_prompt="extra people")
    prompt, negative = build_prompt(shot, version, "Keep the door still")
    assert "林岚，左眼下小痣，黑色齐肩发" in prompt and "鼠尾草绿风衣" in prompt
    assert "Keep the door still" in prompt and "wrong face" in negative


def test_metadata_before_explicit_headers_has_no_fallback_warning():
    result = DeterministicScriptParser().parse("项目标题：真实导入\n分镜 1：墙钟（6 秒）\n剧情：抬眼。\n分镜 2：开关（6 秒）\n剧情：按下开关。")
    assert len(result["shots"]) == 2
    assert result["warnings"] == []
