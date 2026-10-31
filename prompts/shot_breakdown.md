<!-- GENERATED from schemas/cinematic-production-rules.yaml; source-sha256: 85ab4955a7aaa4232639c1869179be17879fa53ad1b6506da533c4c018e05f8f -->
# 动作与镜头拆解

- production_rules_version: `cinematic-1.0.1`
- screenplay_schema_version: `screenplay-ir-2.0.0`
- template_version: `cinematic-word-2.0.0`
- parser_version: `source-preserving-parser-2.0.0`
- video_prompt_compiler_version: `cinematic-compiler-1.0.1`

每个人分别记录初始状态、主动作、次动作、接触对象、重心方向、视线与结束状态。

动作按可执行时间段排序；复杂接触或目标时长无法容纳的动作提出拆镜建议，未经确认不改核心剧情。

服装、道具和动作结束状态应与下一镜初始状态衔接；明确保持左/右手与持有人。

A believable person with the referenced facial geometry, natural asymmetry and source-appropriate skin texture. Preserve the supplied identity and any stated age across costume and emotional changes. Do not invent age facts, beautify, remodel facial proportions, invent markings, or smooth all skin texture.

Specify garment layers, weave, weight, seams, fit, wear, reflectance and current wetness. Costume state is separate from facial identity. Preserve approved outfit features and explicitly track changes.

A physically coherent usable location with stable doors, windows, furnishings and prop positions. Surfaces retain plausible texture, wear, roughness, reflections and scale. Lighting is motivated by specified visible or plausible sources.

Show grounded body weight, stable foot contact and deliberate hand contact. Separate each performer's initial state, one primary action, contact target and final state. Preserve who holds each prop and which hand is used.

Describe the visible framing, screen direction, focus and camera movement needed for this shot. Camera terminology is visual intent unless the workflow exposes an actual control.

Preserve each original spoken line and its speaker identity. Distinguish model instructions from guaranteed audio processing. Never certify cloned voices or lip sync without a verified workflow and actual review.

Review identities, skin, hands, contact, costumes, props, space, motion and sound. A valid decodable file is technical success only, not film quality approval. Unverified visual metrics remain unavailable.
