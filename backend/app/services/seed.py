from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Character, CharacterVersion, Project, ScriptRevision, StoryboardShot
from app.services.parser import DeterministicScriptParser


DEMO_NAME = "人物一致性示例"

IDENTITY = {
    "character_id": "hero_001",
    "type": "原创 AI 人物",
    "fixed_identity": "28 岁左右的原创东亚女性角色；自然椭圆脸、深棕色眼睛、齐下巴深棕色短发、轻微侧分刘海；自然肤质，面部比例真实。",
    "hair": "齐下巴深棕色短发，轻微侧分刘海",
    "distinguishing_features": ["自然肤质", "深棕色眼睛"],
    "body": "真实自然的成年女性身材比例",
    "default_clothing": "中性色外套与上衣",
    "must_not_change": ["身份", "脸型", "眼睛颜色", "固定特征", "发型", "默认服装"],
    "allowed_changes": ["表情", "轻微头部转向", "手部动作", "镜头角度", "背景", "光照氛围"],
    "negative_prompt": "不要改变人物身份、脸型、眼睛颜色、发型和衣服；不要生成第二个相似但不同的人；不要突然变年轻或变老；不要无故添加眼镜、帽子、夸张妆容或不同服装。",
    "prompt_template_version": "hero-identity-v1",
    "reference_state": "empty_waiting_for_user_upload",
}


def seed_demo(session: Session, project_root: Path) -> None:
    character = session.scalar(select(Character).where(Character.slug == "hero_001"))
    if character is None:
        character = Character(
            id="hero_001",
            slug="hero_001",
            display_name="示例角色",
            description="虚构成年女性人物，非真实人物仿冒。",
            status="active",
        )
        session.add(character)
        session.flush()
        version = CharacterVersion(
            character_id=character.id,
            version=1,
            identity_json=IDENTITY,
            approval_state="approved",
        )
        session.add(version)
        session.flush()
        character.current_approved_version_id = version.id

    project = session.scalar(select(Project).where(Project.name == DEMO_NAME))
    fixture = project_root / "fixtures" / "demo_script.md"
    if project is None and fixture.exists():
        raw_text = fixture.read_text(encoding="utf-8")
        parsed = DeterministicScriptParser().parse(raw_text)
        project = Project(
            name=DEMO_NAME,
            source_type="fixture_markdown",
            source_uri="fixtures/demo_script.md",
            source_filename="demo_script.md",
            source_text=raw_text,
            status="script_parsed",
        )
        session.add(project)
        session.flush()
        script = ScriptRevision(
            project_id=project.id,
            revision=1,
            parsed_json=parsed,
            raw_text=raw_text,
            status="draft",
        )
        session.add(script)
        for item in parsed["shots"]:
            session.add(StoryboardShot(project_id=project.id, **item))
    session.commit()

