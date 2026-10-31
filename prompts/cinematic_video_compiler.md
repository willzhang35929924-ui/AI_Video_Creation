<!-- GENERATED from schemas/cinematic-production-rules.yaml; source-sha256: 85ab4955a7aaa4232639c1869179be17879fa53ad1b6506da533c4c018e05f8f -->
# 实际视频工作流编译

- production_rules_version: `cinematic-1.0.1`
- screenplay_schema_version: `screenplay-ir-2.0.0`
- template_version: `cinematic-word-2.0.0`
- parser_version: `source-preserving-parser-2.0.0`
- video_prompt_compiler_version: `cinematic-compiler-1.0.1`

仅编译当前镜头相关人物、场景版本、服装状态、道具状态、动作、摄影、对白和声音，并保留全剧共同约束。

标准脸、场景图与关键画面按用途标注；参考图不冒充精确首帧，未支持的尾帧、口型或相机控制不得伪装实现。

冻结规则版本、模型、工作流哈希、实际参数、参考ID及哈希；重新生成需明确采用哪个版本。

A believable person with the referenced facial geometry, natural asymmetry and source-appropriate skin texture. Preserve the supplied identity and any stated age across costume and emotional changes. Do not invent age facts, beautify, remodel facial proportions, invent markings, or smooth all skin texture.

Specify garment layers, weave, weight, seams, fit, wear, reflectance and current wetness. Costume state is separate from facial identity. Preserve approved outfit features and explicitly track changes.

A physically coherent usable location with stable doors, windows, furnishings and prop positions. Surfaces retain plausible texture, wear, roughness, reflections and scale. Lighting is motivated by specified visible or plausible sources.

Show grounded body weight, stable foot contact and deliberate hand contact. Separate each performer's initial state, one primary action, contact target and final state. Preserve who holds each prop and which hand is used.

Describe the visible framing, screen direction, focus and camera movement needed for this shot. Camera terminology is visual intent unless the workflow exposes an actual control.

Preserve each original spoken line and its speaker identity. Distinguish model instructions from guaranteed audio processing. Never certify cloned voices or lip sync without a verified workflow and actual review.

Review identities, skin, hands, contact, costumes, props, space, motion and sound. A valid decodable file is technical success only, not film quality approval. Unverified visual metrics remain unavailable.
