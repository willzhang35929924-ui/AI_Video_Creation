<!-- GENERATED from schemas/cinematic-production-rules.yaml; source-sha256: 85ab4955a7aaa4232639c1869179be17879fa53ad1b6506da533c4c018e05f8f -->
# 自然语言导入

- production_rules_version: `cinematic-1.0.1`
- screenplay_schema_version: `screenplay-ir-2.0.0`
- template_version: `cinematic-word-2.0.0`
- parser_version: `source-preserving-parser-2.0.0`
- video_prompt_compiler_version: `cinematic-compiler-1.0.1`

标准模板和未标识的自然语言文档进入同一中间结构，保留正文、表格、顺序和每项来源。

明确区分原文、建议、继承、用户确认、未知与冲突；置信度只表示规则提取依据，不是模型或画质评分。

重名、相似身份、动作主体不清、服装与场景矛盾进入待确认；未知段落不丢弃，也不自动变成确定剧情。

A believable person with the referenced facial geometry, natural asymmetry and source-appropriate skin texture. Preserve the supplied identity and any stated age across costume and emotional changes. Do not invent age facts, beautify, remodel facial proportions, invent markings, or smooth all skin texture.

Specify garment layers, weave, weight, seams, fit, wear, reflectance and current wetness. Costume state is separate from facial identity. Preserve approved outfit features and explicitly track changes.

A physically coherent usable location with stable doors, windows, furnishings and prop positions. Surfaces retain plausible texture, wear, roughness, reflections and scale. Lighting is motivated by specified visible or plausible sources.

Show grounded body weight, stable foot contact and deliberate hand contact. Separate each performer's initial state, one primary action, contact target and final state. Preserve who holds each prop and which hand is used.

Describe the visible framing, screen direction, focus and camera movement needed for this shot. Camera terminology is visual intent unless the workflow exposes an actual control.

Preserve each original spoken line and its speaker identity. Distinguish model instructions from guaranteed audio processing. Never certify cloned voices or lip sync without a verified workflow and actual review.

Review identities, skin, hands, contact, costumes, props, space, motion and sound. A valid decodable file is technical success only, not film quality approval. Unverified visual metrics remain unavailable.
