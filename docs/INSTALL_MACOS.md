# macOS 安装教程

Mac 通过 Docker Desktop 运行网页、后台、SQLite、任务编排与 FFmpeg。本地 H3＋Context-IR＋4step LoRA 连接用户自己的 NVIDIA 推理主机；Windows CUDA 环境不能复制到 Mac 后直接运行。也可配置应用已支持的云端 API。

当前没有 Mac 物理设备上的完整验收，也没有 Linux GPU 节点真实生成验收。双架构构建、模拟 ARM64 运行和 Windows 实测均不能替代这些测试。实际范围见 [发布验收记录](RELEASE_VALIDATION.md)。

## 1. 安装 Docker Desktop

苹果菜单 → 关于本机，确认 Apple Silicon 或 Intel；也可运行 `uname -m`，注意 Rosetta 终端可能影响显示。按 [Docker 官方 Mac 安装说明](https://docs.docker.com/desktop/setup/install/mac-install/) 安装适合自己芯片及系统版本的 Docker Desktop。

启动 Docker Desktop 并等待引擎运行，在终端检查：

```bash
docker version
docker compose version
docker info --format '{{.OSType}}/{{.Architecture}}'
```

Apple Silicon 默认构建 Linux ARM64，Intel 默认 Linux AMD64。不要把强制 AMD64 模拟运行当成原生适配或真实 Mac 验收。

## 2. 下载并启动网页

在 [项目仓库](https://github.com/willzhang35929924-ui/AI_Video_Creation) **Code → Download ZIP** 后完整解压，终端进入含 `compose.yaml` 的目录。使用 Git 时：

```bash
git clone https://github.com/willzhang35929924-ui/AI_Video_Creation.git video-factory
cd video-factory
docker compose up -d
docker compose ps
open http://127.0.0.1:8080
```

默认构建当前下载的源码，首次需要网络、CPU 与磁盘空间。`pull_policy: build` 自动构建，无需额外加 `--build`。应用主机无需另装 Python、Node.js、数据库或 FFmpeg，也不需要先创建 `.env`。

可选脚本为 `bash ./scripts/start.sh`。8080 被占用时运行 `cp .env.example .env`，修改 `WEB_PORT` 后重新启动；`.env` 不上传 GitHub。

默认项目名 `video-factory` 固定了命名卷前缀。下载第二份源码不会自动得到独立业务数据；开发测试使用独立 `compose.dev.yaml`（项目 `video-factory-dev`、端口 `18083`）。可选 `compose.prebuilt.yaml` 拉取 GHCR 发行镜像，但旧镜像可能缺少本次更新，不应覆盖新版业务库。

## 3. 连接自己的 NVIDIA 推理节点

先在 Windows NVIDIA 主机按 [本地推理节点教程](local-inference.md) 安装模型、运行环境和项目节点，启动 **8788** 节点入口。当前原生安装器将 Linux 标为 `BLOCKED_UNVERIFIED_RUNTIME` 并阻止启动，Linux GPU 路线尚未完成独立验收。

在 Mac 的网页中：

1. **系统设置 → 添加服务 → 视频生成 → 本地 / 私有 ComfyUI**。
2. 内置工作流选 **Context-IR 三档：Fast 4 / Quality 20 / Quality 30**。
3. 地址填写自有 GPU 主机的私网地址，例如 `http://192.168.1.20:8788`；勾选 **确认这是我管理的本地模型服务**，保存并测试连接。
4. 项目选用该视频档案；制作画质选 Fast 4 步、标准 20 步或高质量 30 步，保存后启动新任务。

这条设置路径无需编辑 `.env`。连接当前 Mac 自己已支持的宿主服务时可用 `host.docker.internal`；它不能代表另一台 NVIDIA 主机。在 GPU 节点的 `allowed_clients` 中加入已核对的 Mac/Docker 客户端精确私网 IP；若出现 403，在节点日志中核对 `client_ip`，按本地教程配置白名单。节点保持私有，不开放到公共互联网。

Mac Docker 不会获得 Windows CUDA、宿主 Apple Metal/MPS 或原生 Windows 启动器。CosyVoice、IndexTTS、SeedVR2 等也必须使用本项目实际支持的运行方式；任意 HTTP 地址不等于已有对应远程适配器。

## 4. 云端 API 与第一条小样

使用云端服务时，在服务档案按用途选择已实现的 API 协议、地址和模型，填写自己的密钥并测试。云端地址使用 HTTPS；语言模型的 `/v1/models` 或聊天接口不能替代视频生成接口。三档 Context-IR 路线属于本地 H3，不能仅换一个云端域名沿用。

下载 [剧本模板.docx](templates/剧本模板.docx)，填写一条短镜头后在项目工作台导入并核对。人物与产品参考按需上传；声音要求使用实际支持的策略与授权素材。开始制作后等待真实返回，播放审核分镜，批准并合成 MP4，下载检查画面、声音和时长。

基础健康、连接测试、真实生成与人工审核分别判断；不要用网页能打开证明视频链路已经通过。详见 [完整使用教程](USER_GUIDE_ZH.md)。

## 5. 更新、备份与更换磁盘

```bash
docker compose logs --tail=100 backend frontend
docker compose exec backend python -m app.container_entrypoint doctor
bash ./scripts/backup-docker.sh
docker compose stop
docker compose start
```

升级前备份，更新源码后执行 `docker compose up -d`。归档包含 SQLite、媒体和解密主密钥，是私人数据；另存到可靠磁盘，不上传仓库。Docker 数据卷由 Docker Desktop 管理，复制源码文件夹不能备份卷，也不能迁移 NVIDIA 主机的模型。

更换硬盘或电脑按 [恢复教程](DOCKER_RELEASE_GUIDE_ZH.md) 恢复到新的空卷，重新核对服务主机与模型能力。保留原卷，不使用 `docker compose down -v` 作为更新方式。遇到问题见 [故障排查](TROUBLESHOOTING_ZH.md)。
