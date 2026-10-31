from __future__ import annotations

import re
from dataclasses import asdict, dataclass


SHOT_HEADER = re.compile(
    r"^\s*(?:分镜|镜头)\s*(\d+)\s*[：:]\s*(.*?)(?:[（(]\s*(\d+(?:\.\d+)?)\s*秒\s*[)）])?\s*$"
)
FIELD_LINE = re.compile(r"^\s*([^：:]{1,16})[：:]\s*(.*)$")


@dataclass(slots=True)
class ParsedShot:
    order: int
    scene_title: str
    narration: str = ""
    dialogue: str = ""
    visual_prompt: str = ""
    negative_prompt: str = ""
    action: str = ""
    camera_move: str = ""
    location: str = ""
    lighting: str = ""
    audio_direction: str = ""
    duration_seconds: float = 5.0
    aspect_ratio: str = "9:16"


class ScriptParser:
    def parse(self, raw_text: str) -> dict:
        raise NotImplementedError


class DeterministicScriptParser(ScriptParser):
    """Rule-based parser that remains usable without a text model."""

    def parse(self, raw_text: str) -> dict:
        text = raw_text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not text:
            raise ValueError("脚本文本为空")
        lines = text.split("\n")
        metadata: dict[str, str] = {}
        blocks: list[tuple[re.Match[str], list[str]]] = []
        current: tuple[re.Match[str], list[str]] | None = None

        for line in lines:
            header = SHOT_HEADER.match(line)
            if header:
                if current:
                    blocks.append(current)
                current = (header, [])
                continue
            if current:
                current[1].append(line)
            else:
                field = FIELD_LINE.match(line)
                if field and field.group(1).strip() in {
                    "项目标题",
                    "内容类型",
                    "统一主角",
                    "统一画面风格",
                }:
                    metadata[field.group(1).strip()] = field.group(2).strip()
        if current:
            blocks.append(current)

        has_explicit_headers = bool(blocks)
        if not blocks:
            blocks = self._fallback_blocks(text)

        shots = [self._parse_block(header, block_lines, i + 1) for i, (header, block_lines) in enumerate(blocks)]
        return {
            "parser": "deterministic-v1",
            "title": metadata.get("项目标题", "未命名项目"),
            "metadata": metadata,
            "shots": [asdict(shot) for shot in shots],
            "warnings": [] if has_explicit_headers else ["未发现明确分镜标记，已按段落规则拆分"],
        }

    def _fallback_blocks(self, text: str) -> list[tuple[re.Match[str], list[str]]]:
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", text) if p.strip()]
        if not paragraphs:
            paragraphs = [text]
        blocks = []
        for index, paragraph in enumerate(paragraphs, start=1):
            synthetic = SHOT_HEADER.match(f"分镜 {index}：段落 {index}（5 秒）")
            assert synthetic is not None
            blocks.append((synthetic, [f"剧情：{paragraph}"] ))
        return blocks

    def _parse_block(self, header: re.Match[str], lines: list[str], fallback_order: int) -> ParsedShot:
        order = int(header.group(1) or fallback_order)
        title = (header.group(2) or f"分镜 {order}").strip()
        duration = float(header.group(3) or 5)
        fields: dict[str, list[str]] = {}
        active_key = "剧情"
        fields[active_key] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            match = FIELD_LINE.match(line)
            if match:
                active_key = match.group(1).strip()
                fields.setdefault(active_key, []).append(match.group(2).strip())
            else:
                fields.setdefault(active_key, []).append(line)

        def value(*keys: str) -> str:
            for key in keys:
                if fields.get(key):
                    return "\n".join(fields[key]).strip()
            return ""

        narration = value("剧情", "旁白", "叙述")
        visual = value("画面提示词", "画面", "提示词") or narration
        audio = value("声音提示", "声音", "音效", "音乐")
        dialogue = value("台词", "对话")
        negative = value("负面提示词", "禁止")
        camera = self._first_match(visual, ["缓慢推进", "平稳横移", "镜头推进", "推近", "固定镜头", "摇镜", "跟拍"])
        lighting = self._first_match(visual, ["暖冷混合照明", "暖色室内灯光", "写实电影灯光", "自然光", "柔光"])
        return ParsedShot(
            order=order,
            scene_title=title,
            narration=narration,
            dialogue=dialogue,
            visual_prompt=visual,
            negative_prompt=negative,
            action=narration,
            camera_move=camera,
            location=title,
            lighting=lighting,
            audio_direction=audio,
            duration_seconds=duration,
        )

    @staticmethod
    def _first_match(text: str, candidates: list[str]) -> str:
        return next((candidate for candidate in candidates if candidate in text), "")

