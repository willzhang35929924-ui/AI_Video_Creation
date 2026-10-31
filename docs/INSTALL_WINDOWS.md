# Windows 安装教程

本教程运行网页、后端和媒体处理工具。生成模型使用你配置的云端 API 或独立本地服务。无需先安装 Python、Node.js、SQLite、FFmpeg 或模型权重。

> 发布验证进行中：仓库、MIT 许可和镜像命名已确认；在镜像公开、匿名拉取和发布记录通过前，生产部署尚未验收。源码构建使用 `compose.dev.yaml`。

## 1. 安装 Docker Desktop

先按 `Win+R`，输入 `winver` 并回车，查看 Windows 版本与构建。按 [Docker 官方 Windows 安装说明](https://docs.docker.com/desktop/setup/install/windows-install/) 检查当前受支持系统和硬件条件，下载并安装 Docker Desktop。使用 WSL 2 后端和 Linux 容器，不能选成 Windows 容器。

安装器提示启用虚拟化、WSL 2 或重启时按提示完成。在任务管理器的“性能 → CPU”可以查看虚拟化状态。若系统未安装 WSL，可在管理员 PowerShell 按 [Microsoft 的 WSL 安装教程](https://learn.microsoft.com/windows/wsl/install) 安装，按要求重启后再继续。

打开 Docker Desktop，等引擎进入运行状态。在普通 PowerShell 执行：

```powershell
docker version
docker compose version
docker info --format '{{.OSType}}/{{.Architecture}}'
```

最后一项应报告 Linux 和与你主机对应的架构。本文的 Windows 路线面向 x86-64 / AMD64，`x86_64` 也是该架构常见显示名。若提示无法连接 Docker 引擎，先处理 Docker Desktop 自身的启动状态。

## 2. 下载公开项目

打开 [GitHub 仓库](https://github.com/willzhang35929924-ui/AI_Video_Creation)，点击 **Code → Download ZIP**。将 ZIP 解压到你自己的目录，例如 `D:\video-factory`。不要覆盖已有安装文件夹或把旧数据库复制进去。

在资源管理器进入能看见 `compose.yaml` 的文件夹，在地址栏输入 `powershell` 后按回车。确认当前目录：

```powershell
Get-Location
Get-Item compose.yaml
```

会用 Git 的用户也可执行：

```powershell
git clone https://github.com/willzhang35929924-ui/AI_Video_Creation video-factory
cd video-factory
```

普通用户不需要 GitHub 账号，也不需要安装 Git 才能下载 ZIP 并运行公开镜像。

## 3. 一条命令启动

```powershell
docker compose up -d
```

首次会下载匹配 AMD64 的前后端镜像、创建网络和持久化数据卷，然后启动应用。这里不用加 `--build`，也不用先复制 `.env.example`。若还未公开镜像，不能把拉取失败解释为你电脑上的模型问题。

查看状态并打开网页：

```powershell
docker compose ps
Start-Process 'http://127.0.0.1:8080'
```

可选辅助启动脚本为 `./scripts/start.ps1`。若 PowerShell 执行策略阻止脚本，直接使用上面的 Docker 命令即可，不必修改整台机器的执行策略。

如果 8080 被占用，复制 `.env.example` 为 `.env`，只修改 `WEB_PORT=8081`，再运行 `docker compose up -d`，随后访问 `http://127.0.0.1:8081`。默认绑定 `127.0.0.1`，只允许本机访问；不要随意改成对公网开放。

## 4. 首次配置与功能测试

1. 打开系统设置的服务档案。按用途配置真实的语言、图像、视频或语音服务，保存并测试连接。密钥只在自己的设置页输入。
2. 在项目工作台下载 Word 模板，先导入一份简单、非敏感的单人物单镜头内容；检查人物、分镜、对白、时长和画幅是否解析正确。
3. 按需要上传人物参考、声音和固定产品图片，检查项目制作画质与服务路由，然后启动制作。
4. 查看任务从提交到结束的真实状态，审核关键画面和视频；批准需要合成的版本，再渲染最终 MP4，下载并实际播放。

外部服务缺失时网页仍应能运行。请区分“基础健康”“连接测试”“真正生成”和“人工审核通过”，不要把容器健康当成视频生成成功。

完整操作见 [使用教程](USER_GUIDE_ZH.md)。

## 5. 连接 Windows 上已有的模型

通用镜像不包含 ComfyUI、H3、CosyVoice、IndexTTS、SeedVR2、CUDA 或模型权重。保留原生模型安装，通过应用实际支持的服务协议连接。

- Docker 容器内的 `127.0.0.1:8189` 不是 Windows 上的 ComfyUI。Docker Desktop 的宿主地址通常是 `http://host.docker.internal:8189`。
- 在 `.env` 的 `TRUSTED_LOCAL_SERVICE_ORIGINS` 中添加你拥有且控制的精确 origin，例如 `http://host.docker.internal:8189`；变更后重建应用容器。不要填带密钥、路径或查询参数的 URL。
- 服务必须允许容器实际访问；检查监听地址和 Windows 防火墙，只向受控网络开放。不要为了连通而把没有认证的推理端口公开到互联网。
- 文件在 Windows 上存在，不等于容器能访问该路径。基于 Windows 可执行文件和磁盘目录启动模型的助手不能在 Linux 容器中运行；遇到这种诊断，使用已支持的远端服务或继续在原生 Windows 应用中运行对应能力。

Windows NVIDIA 容器推理还涉及驱动、WSL 2 和 GPU 透传，参见 [Docker GPU 官方说明](https://docs.docker.com/desktop/features/gpu/)。本项目默认 Compose 没有替你安装或验证 GPU 推理环境。

## 6. 停止、更新与迁移磁盘

停止：`docker compose stop`。再次启动或更新：`docker compose up -d`。移除容器但保留数据：`docker compose down`。

升级、更换硬盘或重装 Docker Desktop 前，运行 `./scripts/backup-docker.ps1` 并把生成的归档复制到另一块磁盘。仅复制源码文件夹并不能备份 Docker 命名卷。恢复必须同时恢复数据库、媒体和主密钥，见 [备份恢复教程](DOCKER_RELEASE_GUIDE_ZH.md)。

**不要运行 `docker compose down -v` 或带 volumes 的全局清理命令来更新。** 更换 Compose 项目名、Docker context 或 Docker 数据盘也可能使网页看起来像新安装；先确认旧卷仍在，避免误建新数据或覆盖旧数据。

遇到启动、镜像下载、404、模型或文件问题，参见 [故障排查](TROUBLESHOOTING_ZH.md)。
