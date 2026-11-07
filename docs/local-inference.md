# 用户自装本地 Context-IR / 4step 推理节点

Docker 只运行网页、API、SQLite、FFmpeg 和业务任务。模型权重、ComfyUI、CUDA、Qwen 推理不在应用镜像中。设置页配置的是下面这个**原生推理节点的 8788 地址**，不是把容器内的 `localhost` 当成 Windows 主机。

当前这套精确 H3 工作流要求 NVIDIA CUDA。Windows 是来源已经核对的基线；Linux 的源版本、内存交接和实际出片尚未验收，工具会明确阻止启动。Mac 可以运行 Docker 应用并连接自己的 NVIDIA Windows 节点；这套 H3 / NVFP4 / SMD 配置没有经过 Metal 验证，不能承诺 Mac 本机直接推理。普通本地 OpenAI-compatible 服务也不能替代本包要求的 Qwen 健康、图片、结构化输出与显存释放协议。

## 1. 来源和版本

完整机器可读清单在 [`runtime-manifests/context-ir-native-v1.json`](../runtime-manifests/context-ir-native-v1.json)。工具不下载任何权重，不替换 ComfyUI 核心或第三方节点，也不接管用户已有的 Qwen 进程。

| 组件 | 本次核对的来源与版本 | 许可证 |
|---|---|---|
| ComfyUI | `Comfy-Org/ComfyUI`，提交 `8010b48d2c1be986613f8e8c761527d205bd756f`；本机版本显示 `0.39.0` | GPL-3.0 |
| ComfyUI-OpenH3-IR | `ruashots/ComfyUI-OpenH3-IR`，提交 `8660988b033d427f346e72fdbcf2d45ede48edbe` | Apache-2.0，保留 LICENSE / NOTICE |
| 编译器 | `open-h3-ir==0.4.1`，32 个 Python 源文件另有 SHA256 校验 | Apache-2.0 |
| H3 主模型、编码器和 VAE | `Comfy-Org/MiniMax-H3`，提交 `e5eb578a89295337b8ff433a035929ce0279e0b6` | MiniMax H3 社区许可证，**不是本项目 Apache 许可证** |
| SMD 4step LoRA | `zjwfufu/SMD-MiniMax-H3-Ref2VA-4step-ComfyUI`，提交 `9544cef6ca00bcedb34a7139032634c4558ed4f8` | 模型卡标明 MiniMax H3 社区许可证 |
| Qwen 独立视觉模型 | `Qwen/Qwen3-VL-4B-Instruct`，本包核对已有权重与处理器文件的精确哈希 | Apache-2.0 |

ComfyUI 的 15 个原生采样/量化源文件已与上述公开提交逐项匹配，证据在 [`upstream-source-verification.json`](../runtime-manifests/upstream-source-verification.json)。安装器仍会检查用户自己的实际文件，版本号相同也不会自动跳过 SHA 检查。OpenH3 节点包的 9 个文件已证实仅有 Windows CRLF 与公开 LF 的差异，清单明确采用仅 CRLF→LF 的 SHA 校验；其他代码字节必须完全一致。Comfy 核心保持原始字节校验，以下 clone 明确关闭 autocrlf。不要使用“最新 ComfyUI”替代该提交后宣称已经兼容。升级需要更新来源清单，并重新验收真实出片。

本包仅包含项目自有 guard/compiler wrapper/cache policy/writer format/SMD sampling audit 节点。`ComfyUI-MiniMaxH3Mod` 是本机发现的 MIT 可选组件，这两个网页生产图不使用它，故不需要额外安装。旧 `minimax_h3_ref2v_turbo_4step_v0.1...` 也不是这里使用的 SMD LoRA。

## 2. 复用或安装用户自己的模型

先复用已有文件。不要因为下面列出来源就再次下载同名模型。目录可自行选择，下面以 `C:\AI\Models` 为例：

```text
C:\AI\Models\
  diffusion_models\
    minimax_h3_ref2va_pruned_w6a8.safetensors
    minimax_h3_ref2va_pruned_int8_convrot.safetensors
    minimax_h3_fl2va_pruned_int8_convrot.safetensors
  text_encoders\qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors
  vae\minimax_h3_video_vae_int8_convrot.safetensors
  vae\minimax_h3_audio_vae_fp32.safetensors
  loras\SMD-MiniMax-H3-Ref2VA-4step.safetensors
  Qwen3-VL-4B-Instruct\
    config.json / tokenizer.json / tokenizer_config.json / ...
    model.safetensors.index.json
    model-00001-of-00002.safetensors
    model-00002-of-00002.safetensors
```

`qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` 是 H3 专用编码器，不能用作独立的 Qwen 视觉对话服务。独立 Qwen 目录需要完整的配置、tokenizer、processor、索引和两片权重；13 项必需文件、逐文件下载地址与哈希均在清单 `qwen_files` 中。本基线对应 ModelScope revision `302de2efb1dd6de1ee943eb1277dc6875550563d`，请使用清单中固定的 `source` 地址。默认 main 的配置或 tokenizer 后续可能变化，不能与固定清单混用。清单保留了本机原有模型验证元数据；未下载或改动权重。

主模型文件官方下载页：

- [H3 文件清单](https://huggingface.co/Comfy-Org/MiniMax-H3/tree/e5eb578a89295337b8ff433a035929ce0279e0b6)
- [SMD LoRA 文件清单](https://huggingface.co/zjwfufu/SMD-MiniMax-H3-Ref2VA-4step-ComfyUI/tree/9544cef6ca00bcedb34a7139032634c4558ed4f8)
- [Qwen 完整视觉模型](https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct)、[ModelScope 同名模型](https://modelscope.cn/models/Qwen/Qwen3-VL-4B-Instruct/files)
- [H3 模型许可证原文](https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/LICENSE)

两条生产路线同时安装约需 88 GB 权重空间，另留 Python 环境、缓存、参考视频和输出空间。运行内存和显存需求随分辨率、时长和引用素材增加；工具不会将“文件齐全”解释成“硬件必然能完成每个任务”。Windows 提交量低于 2 GiB 余量时会保护性拦截/中断当前精确任务，不修改分页文件、不重启电脑。

## 3. Windows 安装第三方源和两个 Python 环境

需要 Python 3.12（Comfy）与 Python 3.10（本次实测的独立 Qwen）、Git、NVIDIA 驱动，以及可从 PATH 调用的 FFmpeg / FFprobe。请在 PowerShell 中执行，以下路径按实际修改。项目目录也可以有中文或空格。

```powershell
git -c core.autocrlf=false clone https://github.com/Comfy-Org/ComfyUI.git C:\AI\ComfyUI
git -c core.autocrlf=false -C C:\AI\ComfyUI checkout 8010b48d2c1be986613f8e8c761527d205bd756f
git -c core.autocrlf=false clone https://github.com/ruashots/ComfyUI-OpenH3-IR.git C:\AI\ComfyUI\custom_nodes\ComfyUI-OpenH3-IR
git -c core.autocrlf=false -C C:\AI\ComfyUI\custom_nodes\ComfyUI-OpenH3-IR checkout 8660988b033d427f346e72fdbcf2d45ede48edbe

py -3.12 -m venv C:\AI\envs\comfy
py -3.10 -m venv C:\AI\envs\qwen
& C:\AI\envs\comfy\Scripts\python.exe -m pip install torch==2.11.0 torchvision==0.26.0 torchaudio==2.11.0 --index-url https://download.pytorch.org/whl/cu128
& C:\AI\envs\qwen\Scripts\python.exe -m pip install torch==2.8.0 torchvision==0.23.0 --index-url https://download.pytorch.org/whl/cu128
```

在下载的本项目根目录继续：

```powershell
& C:\AI\envs\comfy\Scripts\python.exe -m pip install -r C:\AI\ComfyUI\requirements.txt -r .\scripts\local_inference\requirements-comfy-baseline.txt
& C:\AI\envs\qwen\Scripts\python.exe -m pip install -r .\scripts\local_inference\requirements-qwen-baseline.txt
```

这两份文件是观测到的直接依赖版本基线，并非全部传递依赖的跨平台锁。`check` 会检查实际编译器和采样器源文件，缺失包、CUDA 或源版本差异均会报出；不要跳过检查强行声明支持。模型权重从未被上述 pip 命令安装。若已有两个兼容环境，可以直接使用它们的 Python，无需重建环境。

## 4. 创建配置、安装自有节点和校验

在项目根目录执行。`RuntimeRoot` 必须是独立的可写目录；它保存输入、输出、缓存、Qwen 锁、日志和真实任务回执。已有项目和数据库无需移动。

```powershell
& C:\AI\envs\comfy\Scripts\python.exe -B .\scripts\local_inference\videofactory_native.py init --comfy-root C:\AI\ComfyUI --model-root C:\AI\Models --runtime-root C:\AI\VideoFactoryRuntime --comfy-python C:\AI\envs\comfy\Scripts\python.exe --qwen-python C:\AI\envs\qwen\Scripts\python.exe
```

工具按精确文件名在 `ModelRoot` 内查找，发现重名会阻止自动选择；不存在的文件会写出预期位置供用户配置。打开生成的 `C:\AI\VideoFactoryRuntime\native-runtime.json`，可修改 `model_paths` 中每个文件的实际绝对路径和 `qwen_model`。FFmpeg 不在 PATH 时，填写 `ffmpeg_path` / `ffprobe_path` 的实际可执行文件路径；工具会实际执行 `-version` 检查，并只为新启动的 Comfy/Qwen 子进程补充其 bin 目录，不修改系统 PATH。不会移动、改名或重复下载模型。

```powershell
& C:\AI\envs\comfy\Scripts\python.exe -B .\scripts\local_inference\videofactory_native.py install-nodes --config C:\AI\VideoFactoryRuntime\native-runtime.json
& C:\AI\envs\comfy\Scripts\python.exe -B .\scripts\local_inference\videofactory_native.py check --config C:\AI\VideoFactoryRuntime\native-runtime.json --hash-models
```

`install-nodes` 仅安装四个自有节点包、额外模型路径 YAML、Qwen 校验元数据；已有不同内容会拒绝覆盖，正在执行的 Comfy 队列会拒绝安装。它不重启 Comfy，也不修改用户已有的模型路径 YAML。首次完整哈希需要顺序读取全部模型，会花一些时间；之后检查复用带文件大小、修改时间和文件身份的校验记录，文件变更后必须重新哈希。

结果写入 `RuntimeRoot\installation-check.json`。只有检查项全部通过才允许启动该运行配置；具体缺少哪项在 `checks` 内。`ready=true` 只代表安装基线通过，仍不是视频质量验收。

`check` 会分别用配置的 Comfy 和 Qwen Python 启动短子进程，实际导入 PyTorch、查询 NVIDIA CUDA 驱动及可见设备，不加载任何模型。`comfy_python_hardware` / `qwen_python_hardware` 记录 GPU 名称、总显存、compute capability、PyTorch 的 CUDA 构建版本、当前可用系统 RAM，以及 Windows 提交量总额、上限和余量。CPU 版 PyTorch、没有可用 NVIDIA 设备、驱动不可用或查询异常均有明确诊断，不能仅凭安装了带 CUDA 名称的包通过检查。这些指标用于排障，不保证某个显存或 RAM 数字能完成所有分辨率、时长和素材组合；实际制作能力仍需短镜头出片验收。

## 5. 启动 Comfy 和真实内存调度代理

```powershell
& C:\AI\envs\comfy\Scripts\python.exe -B .\scripts\local_inference\videofactory_native.py start-comfy --config C:\AI\VideoFactoryRuntime\native-runtime.json
& C:\AI\envs\comfy\Scripts\python.exe -B .\scripts\local_inference\videofactory_native.py serve --config C:\AI\VideoFactoryRuntime\native-runtime.json
```

`start-comfy` 使用额外模型路径和独立输入/输出路径，关闭异步 offload、pinned memory、Comfy compiler 和跨任务缓存；检测到原端口已经响应则不重启、不重复启动。需要等 Comfy 初始化完成后再运行 `serve`。后者保持终端运行，启动自己拥有的离线 Qwen 服务，并在 8788 代理真实 Comfy API。建议用系统服务管理器保持该终端服务长期运行。

如果 8772 已有用户此前启动的 Qwen，本包会明确拒绝接管。先等待原任务完成，再由原来的管理入口停止该服务，然后运行本包 `serve`。它不会为了上线新代理强行终止来源不明的进程。关闭代理也不会强杀模型服务。

查看状态：

```powershell
Invoke-RestMethod http://127.0.0.1:8788/video_factory/runtime
```

`ready` 结合真实队列、节点注册、CUDA、工作目录、模型验证、Qwen 身份和 supervisor 心跳；`busy` 表示当前有实际任务或队列不空。只有确实运行了本包监管器才会返回 `memory_policy=runtime_owned`。这些字段不会使用假进度。

每次 `/prompt`：

1. 记录冻结请求、提交意图和编译器旧 scope；向 Comfy **仅发送一次**。
2. 保存实际 `prompt_id`，监管原始图、client_id 和队列；未知提交结果不重试。
3. 检查真实 `source=written`、引用绑定、素材路径和 GPU 释放回执后，停止自己拥有且已空闲的 Qwen 进程，归还其 Windows RAM。
4. Windows 提交量不足时只对两次确认的唯一同一 prompt 发送带 ID 的 interrupt；绝不发送全局中断。
5. 原图和 client_id 的真实终态确认后再恢复自己的 Qwen，为后续镜头服务。

原图素材、历史、数据库、其他 Comfy 实例均不清理。意外中断留下的未知任务会阻止新监管器启动，要求先核查已保存的原任务，防止重复计费或重复生成。

## 6. Docker / Mac 应用怎样连接

代理默认只监听本机 `127.0.0.1`，`allowed_clients` 默认只有本机 loopback。Docker Desktop 是否能访问宿主 loopback 因网络实现而异；若不可达，在 `native-runtime.json` 将 `listen` 改为该主机私网 IP，并把实际使用的 Docker/私网客户端**精确 IP**加入 `allowed_clients`，再重启**代理自身**。例如 Windows 为 `192.168.1.20`、自己的 Mac 为 `192.168.1.30`：

```json
{
  "listen": "192.168.1.20",
  "allowed_clients": ["127.0.0.1", "::1", "192.168.1.30"]
}
```

非 loopback 监听如果没有显式白名单会拒绝启动；不接受 CIDR、`0.0.0.0/0`、公开 IP 或 link-local 客户端，也不会自动扫描全网。Docker Desktop 显示 403 时，在本机代理终端的 JSON 日志中查看 `client_ip`，核对确实属于自己的 Docker 客户端后逐个加入；不要为排错开放整个网段。任何带浏览器 `Origin` 的请求都会拒绝，网页通过后端访问节点。

Windows 防火墙也应只允许实际使用的客户端。Comfy 8188 和 Qwen 8772 继续只监听 loopback。此代理不提供公网认证网关，不要公开端口或通过公网反向代理转发。

设置页选择本地 ComfyUI / H3 服务，实际地址填写：

- 同机 Docker Desktop：`http://host.docker.internal:8788`
- Mac 的 Docker 应用连接自己的 Windows 节点：`http://你的Windows私网IP:8788`

在设置页勾选明确的本地服务确认并保存这个**精确 origin**，通过连接测试后使用。`.env` 的 `TRUSTED_LOCAL_SERVICE_ORIGINS` 仅用于旧部署兼容，可选，不是新用户必须执行的步骤。不要填 `/v1`，也不要把外部云端地址伪装成本地节点。运行节点内部的 Qwen 地址固定为 `http://127.0.0.1:8772/v1`；应用通过 8788 的真实状态回执获取健康，不要求额外暴露 Qwen 端口。

网页制作档位对应：标准 Quality 20 步、高质量 Quality 30 步、快速 SMD 4 步。快速档同时要求正确的 INT8 Ref2VA 基础模型、SMD LoRA、强度 1、精确 sigma 序列和真实采样应用回执，不是简单将另一个图的步数改成 4。

## 7. 首次业务验收、日志和升级

从网页导入仓库 Word 模板，使用自己的普通人物/产品素材，保存本地视频档案，测试连接。先制作一个短镜头，检查实际 Context-IR 编译、参考素材绑定、真实 MP4 完整解码和人工画面审核，再测试 SMD 快速档与成片导出。没有完成这些步骤，不能把健康检查当作端到端出片通过。

原生侧日志：`RuntimeRoot\logs`。每个真实任务：`RuntimeRoot\jobs\job_...`，包括提交、原始响应、Windows 内存记录、编译 scope、Qwen 停止/恢复和终态 history。Comfy 编译和 SMD 回执在 `RuntimeRoot\output`。Docker 查看 `docker compose logs -f backend`。

`/video_factory/evidence?path=...` 只返回本节点输出下已限定审计目录的普通 JSON 文件，包含原始 Base64 字节、SHA256 和解析内容，供应用验证后保存。应用不需要读取宿主机盘符。

升级时先备份 RuntimeRoot 和 Docker named volumes，等待所有真实队列结束。更新源代码、核对版本、重新运行 `check`；检查失败先读差异，不清空数据、不删 Comfy history、不执行 `docker compose down -v`。第三方源 SHA 发生变化后必须重新验收，不能删除保护逻辑继续运行。

## 当前交付边界

本包提供了可执行安装检查、额外模型路径生成、原生服务启动、真实代理、Qwen 所有权检查与 Windows 内存监管。已安装本机的既有 H3 测试结果不等于新便携包已在另一台机器完成出片。仓库不包含用户素材、模型权重、CUDA 运行时或已经生成的视频。Linux 原生推理包和 Mac Metal H3 路线仍未完成实机验收；Docker 应用跨平台运行与 GPU 模型跨平台运行是不同的验收项。
