<!-- GENERATED from schemas/cinematic-production-rules.yaml; source-sha256: 85ab4955a7aaa4232639c1869179be17879fa53ad1b6506da533c4c018e05f8f -->
# 人物身份与参考素材

- production_rules_version: `cinematic-1.0.1`
- screenplay_schema_version: `screenplay-ir-2.0.0`
- template_version: `cinematic-word-2.0.0`
- parser_version: `source-preserving-parser-2.0.0`
- video_prompt_compiler_version: `cinematic-compiler-1.0.1`

身份用稳定人物ID和明确版本；参考图须经过审核，不能靠姓名或种子维持身份。

记录面部轮廓、比例、眼眉鼻唇下颌、肤色与纹理、头发、肩部体态、明显特征和声音；未提供者标记未知。

候选标准脸由用户选定；角度、表情、全身与服装从该身份派生并逐项审核。

身份、服装、发妆、情绪、湿润或受伤等物理状态分开记录，不改写已锁定身份。

A believable person with the referenced facial geometry, natural asymmetry and source-appropriate skin texture. Preserve the supplied identity and any stated age across costume and emotional changes. Do not invent age facts, beautify, remodel facial proportions, invent markings, or smooth all skin texture.

Specify garment layers, weave, weight, seams, fit, wear, reflectance and current wetness. Costume state is separate from facial identity. Preserve approved outfit features and explicitly track changes.

A physically coherent usable location with stable doors, windows, furnishings and prop positions. Surfaces retain plausible texture, wear, roughness, reflections and scale. Lighting is motivated by specified visible or plausible sources.

Show grounded body weight, stable foot contact and deliberate hand contact. Separate each performer's initial state, one primary action, contact target and final state. Preserve who holds each prop and which hand is used.

Describe the visible framing, screen direction, focus and camera movement needed for this shot. Camera terminology is visual intent unless the workflow exposes an actual control.

Preserve each original spoken line and its speaker identity. Distinguish model instructions from guaranteed audio processing. Never certify cloned voices or lip sync without a verified workflow and actual review.

Review identities, skin, hands, contact, costumes, props, space, motion and sound. A valid decodable file is technical success only, not film quality approval. Unverified visual metrics remain unavailable.
