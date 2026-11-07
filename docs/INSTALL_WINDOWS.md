# Windows 安装教程

应用通过 Docker Desktop 运行网页、后台、SQLite 和 FFmpeg。默认从当前源码构建；本地模型与 NVIDIA/CUDA 推理环境在宿主机单独安装，不在应用镜像中。

## 1. 准备 Docker Desktop

按 [Docker 官方 Windows 安装说明](https://docs.docker.com/desktop/setup/install/windows-install/) 检查系统版本、虚拟化和 WSL 2 要求，安装 Docker Desktop，使用 **WSL 2 后端与 Linux 容器**。需要安装 WSL 时按 [Microsoft 官方步骤](https://learn.microsoft.com/windows/wsl/install) 操作。

打开 Docker Desktop，等待引擎就绪，再打开普通 PowerShell：

```powershell
docker version
docker compose version
docker info --format '{{.OSType}}/{{.Architecture}}'
```

应显示 Linux 和主机对应的架构。本教程的本机 H3 路线面向 Windows x86-64＋NVIDIA；其他硬件不能只凭应用镜像能构建就认为模型可运行。

## 2. 下载当前项目

从 [GitHub 仓库](https://github.com/willzhang35929924-ui/AI_Video_Creation) **Code → Download ZIP** 下载并完整解压，例如到 `D:\video-factory`。在能看到 `compose.yaml` 的文件夹地址栏输入 `powershell`。

也可用 Git：

```powershell
git clone https://github.com/willzhang35929924-ui/AI_Video_Creation.git video-factory
cd video-factory
```

不要覆盖已有业务安装；默认项目名 `video-factory` 会复用相同 Docker 卷，另一份源码目录并不自动隔离数据。

## 3. 一条命令启动应用

```powershell
docker compose up -d
docker compose ps
Start-Process 'http://127.0.0.1:8080'
```

首次会下载基础镜像与锁定依赖并构建当前源码。`pull_policy: build` 会自动构建，不需要加 `--build`，也不需要先复制 `.env.example`。应用主机不用另装 Python、Node.js、数据库或 FFmpeg。首次构建耗时取决于网络、CPU 和磁盘；失败时查看实际下载或构建错误。

可选辅助脚本为 `./scripts/start.ps1`；若执行策略阻止它，直接使用上面的 Docker 命令。8080 被占用时可复制 `.env.example` 为 `.env`，只调整 `WEB_PORT` 后重新启动。不要向公网开放默认管理网页。

旧 GHCR 发行镜像可用 `docker compose -f compose.prebuilt.yaml up -d`，但旧镜像可能没有本次 Context-IR 更新；不要将其直接用于已升级的数据库。开发隔离环境使用 `compose.dev.yaml`，默认项目 `video-factory-dev`、端口 `18083`。

## 4. 安装本地模型节点

按 [本地推理节点教程](local-inference.md) 安装/复用 ComfyUI、Qwen、H3、4step LoRA 和所需节点，完成来源校验与本机检查，再启动节点代理。项目提供安装工具与来源清单，不包含权重，也不会将已有模型重复下载。

三档 Context-IR 使用节点代理的 **8788** 入口。它负责素材理解、编译、显存交接与真实 ComfyUI 任务；仅打开普通 ComfyUI 8188/8189 不等于此入口已准备好。NVIDIA 驱动、模型许可证、内存/显存和对应 Python 运行环境仍需满足模型实际要求。

应用容器不能运行 Windows `.exe` 或直接使用 `E:\Models` 路径。模型目录应填写在**主机节点配置**；网页填写节点的 HTTP 接口。

## 5. 在网页保存服务并制作小样

1. 打开 **系统设置 → 添加服务**。用途选 **视频生成**，提供商选 **本地 / 私有 ComfyUI**。
2. 内置工作流选 **Context-IR 三档：Fast 4 / Quality 20 / Quality 30**，地址填 `http://host.docker.internal:8788`；勾选 **确认这是我管理的本地模型服务**。
3. 保存并 **测试连接**。无需为这个档案编辑 `.env`。如果节点在另一台自有 GPU 主机，改填真实私网地址；检查监听地址与防火墙，只允许受控网络访问。
4. 在项目中选择该视频档案，保存制作画质：Fast＝4 步、标准＝20 步、高质量＝30 步。三档均使用 Context-IR，四步档需匹配的 LoRA 和采样节点。
5. 下载当前 [剧本模板.docx](templates/剧本模板.docx)，导入一条短镜头，核对解析。按需添加人物、产品与声音参考，再开始制作。
6. 等待真实任务结束，播放审核分镜，批准后合成 MP4，再下载检查。通过连接测试不能代替真实生成与人工审核。

容器中的 `localhost` 指向容器自身，不是 Windows。只有明确确认的本地/私有服务可以使用 HTTP；云端地址仍需 HTTPS 与已实现的 API 协议。语言、图像和视频模型不能互相冒用；语音/口型 CLI 也不自动成为通用远程 API。

完整操作见 [使用教程](USER_GUIDE_ZH.md)，云端密钥只在设置页输入。

## 6. 维护与更换硬盘

```powershell
docker compose logs --tail=100 backend frontend
docker compose exec backend python -m app.container_entrypoint doctor
docker compose stop
docker compose start
```

升级前运行 `./scripts/backup-docker.ps1`，将完整归档和校验文件另存到其他磁盘；更新源码后再运行 `docker compose up -d`。仅复制源码目录不能迁移 Docker 命名卷，也不能备份模型目录。

数据库、媒体和加密主密钥必须一起恢复；模型节点另按自身配置迁移。见 [备份恢复教程](DOCKER_RELEASE_GUIDE_ZH.md)。不要使用 `docker compose down -v` 或全局卷清理来更新。网页突然出现空历史时先核对项目名、Docker context 与数据卷，保留旧卷。

平台与实际测试边界见 [发布验收记录](RELEASE_VALIDATION.md)；问题诊断见 [故障排查](TROUBLESHOOTING_ZH.md)。
