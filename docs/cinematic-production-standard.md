<!-- GENERATED from schemas/cinematic-production-rules.yaml; source-sha256: 85ab4955a7aaa4232639c1869179be17879fa53ad1b6506da533c4c018e05f8f -->
# 电影级视觉制作规范

本规范驱动 Word、兼容解析、中间数据、人物与制作设定、视觉与视频提示词、连续性预检及真实编排。电影质量须审核，文字规范不保证模型完全服从。

- production_rules_version: `cinematic-1.0.1`
- screenplay_schema_version: `screenplay-ir-2.0.0`
- template_version: `cinematic-word-2.0.0`
- parser_version: `source-preserving-parser-2.0.0`
- video_prompt_compiler_version: `cinematic-compiler-1.0.1`

## 人物身份与参考素材

身份用稳定人物ID和明确版本；参考图须经过审核，不能靠姓名或种子维持身份。

记录面部轮廓、比例、眼眉鼻唇下颌、肤色与纹理、头发、肩部体态、明显特征和声音；未提供者标记未知。

候选标准脸由用户选定；角度、表情、全身与服装从该身份派生并逐项审核。

身份、服装、发妆、情绪、湿润或受伤等物理状态分开记录，不改写已锁定身份。

## 场景与材质设计

场景使用独立ID和状态版本；记录时间天气、门窗家具布局、背景、主要材质与道具位置。

描述主光方向、色温和强弱关系、辅助光、阴影和反射来源；避免用电影感或8K代替具体信息。

场景参考图与分镜关键画面须审核；状态变化创建新版本，不能悄悄改历史参考。

## 动作与镜头拆解

每个人分别记录初始状态、主动作、次动作、接触对象、重心方向、视线与结束状态。

动作按可执行时间段排序；复杂接触或目标时长无法容纳的动作提出拆镜建议，未经确认不改核心剧情。

服装、道具和动作结束状态应与下一镜初始状态衔接；明确保持左/右手与持有人。

## 实际视频工作流编译

仅编译当前镜头相关人物、场景版本、服装状态、道具状态、动作、摄影、对白和声音，并保留全剧共同约束。

标准脸、场景图与关键画面按用途标注；参考图不冒充精确首帧，未支持的尾帧、口型或相机控制不得伪装实现。

冻结规则版本、模型、工作流哈希、实际参数、参考ID及哈希；重新生成需明确采用哪个版本。

## 自然语言导入

标准模板和未标识的自然语言文档进入同一中间结构，保留正文、表格、顺序和每项来源。

明确区分原文、建议、继承、用户确认、未知与冲突；置信度只表示规则提取依据，不是模型或画质评分。

重名、相似身份、动作主体不清、服装与场景矛盾进入待确认；未知段落不丢弃，也不自动变成确定剧情。

## 连续性与质量检查

生成前核对人物版本、造型状态、道具持有人/位置/开合/干湿、场景状态、站位与镜头起止状态。

显式状态变化可衔接；缺少变化原因时报告冲突，不替用户改写原文。

生成后实际解码、帧/尺寸/时长核对；身份漂移、手部、闪烁、动作与声画质量须保留人工审核门槛。

按身份、服装、动作、场景、道具、声画、技术错误分类修复；不合格不自动批准，不无限重试。

## identity 字段

description (`description`), facial_features (`facial_features`), eyes (`eyes`), eyebrows (`eyebrows`), nose (`nose`), lips (`lips`), jaw (`jaw`), skin (`skin`), hair (`hair`), hairline (`hairline`), body (`body`), shoulders (`shoulders`), hands (`hands`), marks (`marks`), movement_style (`movement_style`), voice_profile (`voice_profile`), immutable_traits (`immutable_traits`)

## wardrobe 字段

衣物层次 (`layers`), 材质 (`material`), 织纹 (`weave`), 重量感 (`weight`), 垂坠 (`drape`), 剪裁 (`cut`), 合身程度 (`fit`), 长度 (`length`), 领口 (`collar`), 袖口 (`cuffs`), 缝线 (`seams`), 色彩 (`colors`), 反光特性 (`reflectance`), 磨损 (`wear`), 配饰 (`accessories`), 干湿状态 (`wetness`), 破损状态 (`damage`), 沾污状态 (`dirt`), 固定特征 (`immutable_features`), 可变细节 (`allowed_variation`)

## prop 字段

材质 (`material`), 尺寸 (`dimensions`), 颜色 (`color`), 磨损 (`wear`), 持有人 (`holder`), 持有手 (`hand`), 位置 (`location`), 开合状态 (`open_state`), 干湿状态 (`wetness`), 破损状态 (`damage`), 接触关系 (`contact`)

## scene 字段

位置 (`location`), time (`time`), weather (`weather`), season (`season`), interior_exterior (`interior_exterior`), layout (`layout`), 门窗关系 (`doors_windows`), 背景 (`background`), 主要材质 (`materials`), 主光方向 (`key_light_direction`), 主光色温 (`key_light_temperature`), 辅助光 (`fill_light`), 阴影 (`shadows`), 反射 (`reflections`), atmosphere (`atmosphere`), palette (`palette`), 曝光意图 (`exposure`), continuity (`continuity`)

## action 字段

初始状态 (`start_state`), 主要动作 (`main_action`), 次级动作 (`secondary_action`), 接触对象 (`contact_target`), 速度力度 (`speed_force`), 重心方向 (`weight_direction`), 视线 (`eyeline`), 表情变化 (`expression_change`), 结束状态 (`end_state`), 动作衔接 (`next_cut`)

## camera 字段

shot_size (`shot_size`), camera_position (`camera_position`), height (`height`), angle (`angle`), composition (`composition`), camera_move (`camera_move`), move_speed (`move_speed`), focus (`focus`), depth (`depth`), foreground (`foreground`), 背景 (`background`), motion_blur (`motion_blur`), 曝光意图 (`exposure`), 初始状态 (`start_state`), 结束状态 (`end_state`), continuity (`continuity`), transition (`transition`)

## 来源与兼容

原文、建议、继承、已审核、未知与冲突分别记录。没有版本标识的 Word 进入通用兼容模式，未确定的语义和主体保留待审核；没有经过验证的语义模型时，不宣称任意文档已被完全理解。

历史人物、镜头与任务快照不会因当前规范变化自动重写。新生成冻结规则、模型、工作流和参考哈希。

## 实际控制边界

本项目 H3 工作流是参考生视频，最多9图，24fps和17n+5帧；宽高、时长、采样步数和种子有实际节点映射。关键画面和场景图属于参考，不能冒充精确首尾帧；摄影术语属于视觉意图。原生音频不等于精确口型或声音克隆。

场景候选使用所选本地图像模型的实际文生图路径，Klein用独立ReferenceLatent接入人物与场景图；多图效果须以真实候选审核结果为准。检测仅认证实际解码/规格，身份、手部、动作与电影观感保留人工审核。

## 同步构建

使用 bundled Python 运行 scripts/build_screenplay_templates.py；检查用 .venv/Scripts/python.exe scripts/build_cinematic_contracts.py --check。修改规范后必须重新构建和测试，运行时的同步检查会报告源、模板、Schema或消费者不一致。
