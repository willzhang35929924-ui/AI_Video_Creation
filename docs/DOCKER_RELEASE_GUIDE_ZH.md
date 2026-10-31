# Docker 部署、升级、备份与发布

普通用户只需要 Docker Desktop 和公开项目文件。开发者才需要源码构建与 GitHub 发布流程。本项目的目标仓库与镜像命名已确认，公开发布、匿名拉取和最终镜像部署仍待验收；在这些步骤完成前，可使用 `compose.dev.yaml` 验证当前源码，不能把目标地址当成镜像已经发布的证明。

## 1. 镜像与依赖来源

| 内容 | 来源 |
| --- | --- |
| 后端应用镜像 | `ghcr.io/willzhang35929924-ui/ai_video_creation-backend:latest` |
| 前端应用镜像 | `ghcr.io/willzhang35929924-ui/ai_video_creation-frontend:latest` |
| Python 构建基础 | Docker Hub `python:3.12-slim-bookworm`，以当前 Dockerfile 为准 |
| 前端构建基础 | Docker Hub `node:24.22.1-alpine3.23`，以当前 Dockerfile 为准 |
| 前端运行基础 | Docker Hub `nginx/nginx-unprivileged:1.30.6-alpine`，以当前 Dockerfile 为准 |
| Python、npm 依赖 | 仓库锁定清单及对应软件包仓库；前端以锁文件安装 |
| FFmpeg、中文字体 | 后端镜像使用的 Debian 软件包源；实际组件与许可单独记录 |
| 模型、ComfyUI、GPU 服务 | 用户独立部署或购买的云端服务，不随应用镜像提供 |

应用镜像应提供 `linux/amd64`、`linux/arm64` 多架构清单。默认由 Docker 选择本机架构，不把 ARM Mac 强制固定成 AMD64。基础镜像支持两种架构，不能代替应用自己的多架构构建与运行验收。

## 2. 普通部署与开发部署

普通部署：

```bash
docker compose up -d
docker compose ps
```

`compose.yaml` 只有 `image`，使用 `pull_policy: always`，不要求用户在自己电脑编译。默认 `127.0.0.1:8080`、三个项目命名卷以及非 root 运行；可选 `.env` 配置参考 `.env.example`。API Key 在网页设置页由后端保存，不放在部署示例或前端环境变量中。

开发部署是独立 Compose 文件：

```bash
docker compose -f compose.dev.yaml config
docker compose -f compose.dev.yaml up -d --build
```

需要隔离测试时，使用不同的 `COMPOSE_PROJECT_NAME` 和 `WEB_PORT`，避免操作正在使用的生产卷。PowerShell 示例：

```powershell
$env:COMPOSE_PROJECT_NAME='video-factory-dev-test'
$env:WEB_PORT='8081'
docker compose -f compose.dev.yaml up -d --build
```

macOS 终端：

```bash
export COMPOSE_PROJECT_NAME=video-factory-dev-test
export WEB_PORT=8081
docker compose -f compose.dev.yaml up -d --build
```

这些环境变量影响当前终端后续命令。回到原安装时使用原终端，或明确恢复原项目名和端口。不要在不清楚项目名时执行停止或恢复命令。

辅助脚本：Windows `./scripts/start.ps1`，开发模式 `./scripts/start.ps1 -Development`；Mac `bash ./scripts/start.sh`，开发模式 `bash ./scripts/start.sh --dev`。脚本仍执行真实 Docker 命令，不会安装模型或绕过服务检查。

## 3. 启动与健康诊断

```bash
docker compose config
docker compose ps
docker compose logs --tail=100 backend frontend
docker compose exec backend python -m app.container_entrypoint doctor
```

在浏览器访问 `/api/health/runtime` 查看详细状态。应用就绪与外部服务就绪分别判断：`application_ready: true` 可与外部生成依赖缺失的 `degraded` 同时出现。连接测试和真正生成必须另做。

不要对外发布完整 `docker compose config` 输出或 `docker inspect` 全量信息，它们可能含自己的部署地址与环境变量。先脱敏再反馈。

## 4. 数据初始化与迁移

第一次使用空卷会自动创建 SQLite 结构并运行 Alembic 迁移，建立必要系统默认值。不会复制维护者的数据库、模型档案、密钥或历史文件。

后续升级后端会自动按当前代码执行迁移。已存在的 SQLite 在结构迁移前会生成数据库快照；它不包含全部媒体与运行凭据，不能代替完整业务备份。若已有表却缺少迁移版本，程序会明确拒绝自动清库；保留原库，按日志核对旧版本与迁移路径。

## 5. 升级与查看版本

先让制作任务结束并完成备份，再执行：

```bash
docker compose up -d
docker compose ps
docker compose images
docker compose logs --tail=100 backend
```

`up -d` 按 `always` 策略查询并拉取镜像，内容变化时重建容器，卷保持不变。`restart` 只重启现有容器，不能用它代替拉取新版本。

镜像标签含义：

- `latest`：发布流程更新的最新公开应用镜像，适合一般用户。
- `vX.Y.Z`：固定正式版本，用于可重复部署与升级规划。
- `sha-…`：某个源码提交的构建，便于定位问题。

用 `docker compose images` 看正在使用的镜像引用和 ID；GitHub Releases/变更说明查看该版本更新内容。检查 OCI 标签可运行：

```bash
docker image inspect ghcr.io/willzhang35929924-ui/ai_video_creation-backend:latest --format '{{json .Config.Labels}}'
```

`org.opencontainers.image.revision` 等标签对应构建提交；以实际发布镜像为准。前后端应使用同一发布版本，不混合任意提交。

如需固定版本，在自己的 `.env` 设置 `BACKEND_IMAGE`、`FRONTEND_IMAGE` 为实际 GHCR 的同一 `vX.Y.Z` 引用，或使用发布方提供的版本化 Compose。修改后仍运行 `docker compose up -d`。不要复制未经发布的镜像地址。

## 6. 完整私人备份

Docker 命名卷不在项目源码文件夹中。更换硬盘、迁移电脑、重装 Docker Desktop、更新镜像或更改数据目录前都应备份。

先等待制作任务处于可安全备份状态。备份脚本发现排队或执行中的任务会拒绝操作，避免中断真实生成。脚本短暂停止网页与空闲后端，保存数据库快照及三个数据卷内容，包括媒体和加密主密钥，再恢复原先运行状态；不自动拉新镜像或替换运行版本。

Windows PowerShell：

```powershell
./scripts/backup-docker.ps1
# 可选：指定备份文件夹
./scripts/backup-docker.ps1 -Destination 'E:\factory-backups'
# 源码开发部署使用相应 Compose 文件
./scripts/backup-docker.ps1 -Destination 'E:\factory-backups' -ComposeFile compose.dev.yaml
```

Mac 终端：

```bash
bash ./scripts/backup-docker.sh
# 位置参数：备份文件夹、Compose 文件
bash ./scripts/backup-docker.sh "$HOME/factory-backups" compose.yaml
```

### 备份提示旧镜像不存在时

运行中的后端容器可能仍记录一个已经不在本机镜像库中的旧 `.Image`。此时新的 `docker run` 无法用它启动备份帮助进程，可能报 `No such image` 或 exit 125；这不能单独说明业务数据损坏。

备份脚本默认仍使用当前容器的镜像，不会自动下载或改用其他版本。它先检查帮助镜像在本地存在、Linux 平台适用，并在禁网、非 root、只读根文件系统且不挂业务卷的临时容器中探测归档 CLI。预检成功后才创建备份目录、暂停服务和挂载业务卷。后续步骤固定使用同一个不可变镜像 ID，并保持 `--pull=never` 和禁网。

默认镜像确实缺失时，可显式选择**已经安装并经核对与本项目数据库结构、归档 CLI 兼容的本项目后端镜像**。以下 `video-factory-backend:dev` 仅为项目本地构建标签示例，先用 `docker image inspect` 确认它确实存在；不要直接套用其他应用镜像。

PowerShell：

```powershell
docker image inspect video-factory-backend:dev --format '{{.Id}} {{.Os}}/{{.Architecture}}'
./scripts/backup-docker.ps1 -Destination 'E:\factory-backups' -HelperImage 'video-factory-backend:dev'
```

Bash：

```bash
docker image inspect video-factory-backend:dev --format '{{.Id}} {{.Os}}/{{.Architecture}}'
bash ./scripts/backup-docker.sh --helper-image video-factory-backend:dev "$HOME/factory-backups" compose.yaml
# 或为这次 Bash 备份设置环境变量
BACKUP_HELPER_IMAGE=video-factory-backend:dev bash ./scripts/backup-docker.sh "$HOME/factory-backups" compose.yaml
```

也可将标签替换为经核对的本地完整 `sha256:` 镜像 ID。Bash 的显式选项优先于 `BACKUP_HELPER_IMAGE`；原有 Compose 文件、项目名和位置参数保持适用。预检通过只证明该帮助镜像能执行所需 CLI，完整归档和恢复仍需各自验证。上述帮助镜像选项属于备份入口，恢复使用下一节所列参数。

以脚本输出的真实归档路径与 SHA 校验文件为准。将两者复制到另一块磁盘，确认可读取且校验一致；恢复要求提供匹配的 SHA 校验文件，不能只复制归档。备份包含解密云端服务档案所需的主密钥，**是完整私人资料，不上传仓库、Issue、镜像或公开网盘**。

原生 Windows 的 `scripts/backup.ps1` 与 Docker 的 `backup-docker.ps1` 不是同一个工具。本教程的容器恢复不能直接导入任意原生应用数据库或 Windows 专用凭据。

## 7. 恢复到全新空卷

恢复工具不会覆盖已有业务文件。先保留原安装，在新 Compose 项目名下恢复；不要通过删除旧卷“制造空卷”。把备份与校验文件复制到当前电脑可访问的路径。

Windows：

```powershell
$env:COMPOSE_PROJECT_NAME='video-factory-restored'
$env:WEB_PORT='8081'
./scripts/restore-docker.ps1 -Archive 'E:\factory-backups\实际归档文件.tar.gz'
docker compose up -d
```

Mac：

```bash
export COMPOSE_PROJECT_NAME=video-factory-restored
export WEB_PORT=8081
bash ./scripts/restore-docker.sh "$HOME/factory-backups/实际归档文件.tar.gz"
docker compose up -d
```

脚本要求目标停止且卷为空，会验证归档和校验文件；必要时创建容器但不启动后端，先恢复数据再正常启动。若提示目标已有数据，核对项目名和卷，不要强制覆盖。需要显式指定项目名时，PowerShell 备份/恢复脚本支持 `-ProjectName`；shell 脚本在前两个位置参数后接受第三个项目名。随后启动也要使用同一个项目名，例如 `docker compose -p video-factory-restored up -d`。

访问 `http://127.0.0.1:8081`，检查项目、人物、素材、可播放媒体、服务档案解密及日志。模型主机地址、挂载目录、GPU 与本地服务在另一台机器上可能不同，要重新测试。确认恢复完整后再决定是否切换入口；原安装保留，不能同时绑定同一个宿主机端口。

## 8. 更新失败与回退

先保留错误日志、当前镜像 ID、数据库和卷。不要反复初始化或删卷。若是镜像拉取失败，原容器可能仍在运行；先用 `docker compose ps` 确认。

更早的应用代码不一定能读取升级后的数据库。仅在发布说明确认兼容时，才把前后端镜像同时固定到之前的版本。涉及数据库回退时，用升级前的完整备份恢复到另一个空卷，再使用与该备份匹配的镜像验证；不能默认自动执行 Alembic 降级，更不能先覆盖原库。

## 9. 停止与卸载

`docker compose stop` 暂停；`docker compose down` 移除本项目容器和网络，两者保留命名卷。需要重新使用时运行 `docker compose up -d`。

`docker compose down -v` 会删除命名卷；带 volumes 的全局清理也可能影响其他应用。它们不是升级或普通卸载步骤，数据删除属于单独决定，执行前必须确认已有可恢复的离机备份。

## 10. 维护者的 GitHub / GHCR 发布流程

先确认第一方许可与第三方分发权，完成纯净源码审计。公开目录不得含 `.env`、数据库、主密钥、个人素材、模型、日志、备份、私人路径和旧敏感 Git 历史。`.gitignore` 不会自动清除已经进入 Git 索引或提交历史的内容。

已确认的目标地址：仓库 `https://github.com/willzhang35929924-ui/AI_Video_Creation`；后端 `ghcr.io/willzhang35929924-ui/ai_video_creation-backend:latest`；前端 `ghcr.io/willzhang35929924-ui/ai_video_creation-frontend:latest`。仓库创建、发布权限、镜像公开和匿名拉取需要逐项验证；认证等外部条件尚未满足的步骤记为 `BLOCKED`，不能仅凭地址已确认就声称发布完成。

维护者使用 GitHub CLI 的浏览器授权路线时，执行 `gh auth login --hostname github.com --git-protocol https --web --scopes workflow`；已有授权需要补充工作流权限时，执行 `gh auth refresh --hostname github.com --scopes workflow`。这是为推送 `.github/workflows` 申请的权限，不等于下面的 Actions 包发布权限；登录细节见 [VS Code 与 GitHub 教程](VSCODE_GITHUB_GUIDE_ZH.md)。

按照仓库实际 Actions 配置，PR 执行检查与构建而不推送正式包；发布分支或版本标签通过检查后，用 `GITHUB_TOKEN` 最小权限构建并推送多架构镜像。代码读取需要 `contents: read`，发布包的任务需要 `packages: write`；不要将个人 Token 写进工作流。维护者需要在 GitHub Packages 检查两份镜像的仓库关联和公开可见性。参见 [GitHub 官方容器镜像发布教程](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images)。

正式版本标签示例，只有在版本号确定后执行：

```bash
git tag v1.0.0
git push origin v1.0.0
```

随后在 GitHub **Actions** 查看真实运行，不把“已提交工作流”当作“镜像发布成功”。检查发布镜像清单：

```bash
docker buildx imagetools inspect ghcr.io/willzhang35929924-ui/ai_video_creation-backend:latest
docker buildx imagetools inspect ghcr.io/willzhang35929924-ui/ai_video_creation-frontend:latest
```

两者应包含 AMD64 和 ARM64 对应 manifest。再从已发布地址拉取，在独立空卷中使用正式 `compose.yaml` 启动，检查新库、服务保存、真实核心业务、备份恢复、重建后持久化和匿名拉取。测试证据和日志进入维护者的发布记录；不把私人测试数据复制回源码或镜像。

## 11. 验收记录怎么读

| 状态 | 含义 |
| --- | --- |
| `PASS` | 已执行该项真实测试并有可核对证据 |
| `FAIL` | 已执行，但结果未满足要求 |
| `BLOCKED` | 因设备、授权、网络或其他明确外部条件无法完成 |
| `NOT RUN` | 尚未运行，不应描述为已通过 |

目前 Mac 物理设备验证为 `BLOCKED`，缺少 Mac 实机。多架构构建、模拟 ARM64 运行与 Mac 实测必须分开记。正式发布结果：[发布验收记录](RELEASE_VALIDATION.md)。

## 源码构建时的网络镜像（可选）

普通用户拉取 GHCR 镜像不执行 APT 或 pip。维护者源码构建默认使用 Debian 官方 HTTPS 源和 PyPI；生产依赖使用官方 PyPI SHA256 锁文件，两个 Linux 架构都只安装匹配的二进制 wheel。镜像切换不会跳过 TLS、Debian 签名或 wheel 哈希校验，也不会更改包版本。

如果当前网络无法稳定下载，可在本地 `.env` 设置公开镜像。中国大陆网络可按[清华 Debian 帮助](https://mirrors.tuna.tsinghua.edu.cn/help/debian/)及[PyPI 帮助](https://mirrors.tuna.tsinghua.edu.cn/help/pypi/)选择：

```dotenv
DEBIAN_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/debian
PYPI_INDEX_URL=https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple
```

`DEBIAN_SECURITY_MIRROR` 默认仍使用官方安全更新源；如需单独指定，先核对镜像同步状态。镜像可能有地域限制或同步延迟，不应将某一地域镜像设为所有用户的默认值。地址不得包含密码或 Token。保存后运行 `docker compose -f compose.dev.yaml up -d --build`。
