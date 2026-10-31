# macOS 安装教程

Mac 通过 Docker Desktop 运行相同的网页、后端、SQLite、任务编排和 FFmpeg。视频生成优先配置云端 API；不能将 Windows 上的 NVIDIA/CUDA 模型目录复制进 Mac 就直接运行。

> 验证边界：Linux AMD64 / ARM64 镜像已实际构建、发布并通过 CI 匿名拉取。当前没有 Mac 实机，Mac 物理设备上的启动与完整业务验证为 **BLOCKED**；Linux 构建、模拟运行和其他主机上的安装结果不能替代 Mac 实测或 Metal/MPS 模型验证。
>
> 源码与 `latest` 镜像已公开，CI 已验证 Linux AMD64 / ARM64 匿名拉取。Mac 实机仍缺少设备验证，不能用这些结果替代 Mac 上的实际启动、模型推理和视频制作测试；详见 [发布验收记录](RELEASE_VALIDATION.md)。

## 1. 识别芯片与安装 Docker Desktop

点击苹果菜单 → 关于本机。显示“Apple M…”的是 Apple Silicon；显示 Intel 的是 Intel Mac。也可以打开“终端”运行：

```bash
uname -m
```

`arm64` 对应 Apple Silicon 的原生 ARM64 运行路线，`x86_64` 对应 Intel 的 AMD64 路线。若终端通过 Rosetta 运行，命令显示可能受到影响，优先根据“关于本机”确认芯片。

在 [Docker 官方 Mac 安装页](https://docs.docker.com/desktop/setup/install/mac-install/) 下载对应芯片的安装包，并按当前官方系统要求安装。启动 Docker Desktop，等待引擎就绪。终端验证：

```bash
docker version
docker compose version
docker info --format '{{.OSType}}/{{.Architecture}}'
```

默认不应手动强制 `platform: linux/amd64` 来绕过 ARM64 问题。生产多架构镜像应由 Docker 自动选择适用架构；没有适用清单时应报告发布问题。

## 2. 下载与启动

在 [项目仓库](https://github.com/willzhang35929924-ui/AI_Video_Creation) 点击 **Code → Download ZIP**，解压后在终端进入包含 `compose.yaml` 的目录，例如：

```bash
cd ~/Downloads/video-factory
docker compose up -d
```

目录名以你实际解压结果为准；含空格时用引号包住完整路径。也可安装 Git 后运行：

```bash
git clone https://github.com/willzhang35929924-ui/AI_Video_Creation video-factory
cd video-factory
docker compose up -d
```

生产部署不要求你先安装 Python、Node.js、数据库、FFmpeg，也不要求复制 `.env`。初次拉取完成后：

```bash
docker compose ps
open http://127.0.0.1:8080
```

可选辅助脚本：`bash ./scripts/start.sh`。直接用 `bash` 运行不依赖解压后是否保留可执行权限。

端口占用时，运行 `cp .env.example .env`，用文本编辑器修改 `WEB_PORT=8081`，再执行 `docker compose up -d`；访问地址相应改为 `http://127.0.0.1:8081`。`.env` 是自己的部署配置，不上传 GitHub。

## 3. 配置云端模型并做小样

1. 进入系统设置的服务档案，按用途选择应用已支持的 API 协议，填写真实的服务地址、模型和密钥。
2. 保存并测试连接。语言用途的聊天模型不能代替视频用途的视频模型；能读取模型列表不代表已经开通生成权限。
3. 在项目工作台下载模板，导入一个短镜头，核对解析；人物与产品参考是可选项，按剧本需要准备。
4. 启动制作，实际等待生成并审核结果；合成 MP4 后下载播放，核对声音、时长和画面。

Mac 的本地 Docker 健康检查与付费云端推理是不同测试。完整流程见 [使用教程](USER_GUIDE_ZH.md)。

## 4. 本地模型与远程节点

Docker Desktop 的 Linux 容器不能直接获得 Apple 的 Metal/MPS 运行环境，也不能执行项目的 Windows 专用模型启动脚本。若某模型官方支持 macOS 原生推理，需要你在宿主机独立安装，再确认本项目已有适用的服务协议、连接测试和真实生成结果；本项目没有因此自动保证该模型可用。

已部署在 Mac、Windows GPU 机或 Mac mini 上的服务，必须先在各自主机真实运行。连接当前 Mac 宿主机通常用 `host.docker.internal`；连接另一台主机则用受控局域网地址和认证。配置相应本地服务 origin 白名单并检查监听、防火墙和连通性。不要把容器 `localhost` 当作宿主机。

Windows 上的 H3、CosyVoice、IndexTTS、SeedVR2 原生运行环境与磁盘路径不能原封不动用于 Mac。它们的模型权重不在应用镜像内；不能把“功能页面还在”写成“本地推理已适配 Mac”。

## 5. 更新、备份与更换磁盘

```bash
# 更新镜像并按需重建，保留数据卷
docker compose up -d

# 备份私人业务数据
bash ./scripts/backup-docker.sh

# 停止；需要时再 up -d
docker compose stop
```

备份归档包含数据库、用户文件和加密主密钥，另存到可靠磁盘，不上传 GitHub。Docker 命名卷通常由 Docker Desktop 管理，单纯复制源码文件夹不能迁移业务数据。更换硬盘或从 Windows 迁移时应按 [备份恢复教程](DOCKER_RELEASE_GUIDE_ZH.md) 恢复到空卷，再重新检查模型地址和服务能力；Windows 原生凭据与模型安装不是 Docker 数据备份。

不要在升级或普通卸载中删除 volumes。故障处理见 [故障排查](TROUBLESHOOTING_ZH.md)。
