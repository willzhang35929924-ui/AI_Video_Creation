# 用 VS Code 从零维护 GitHub 项目

这份教程供发布者学习首次上传和后续维护。普通安装用户只需下载项目、安装 Docker Desktop 并启动，**不必注册 GitHub 或学习 Git 才能使用应用**。

本项目第一方代码采用 MIT，公开仓库为 `https://github.com/willzhang35929924-ui/AI_Video_Creation`，Actions 已实际发布两架构镜像。后续修改仍须使用纯净发行目录，保留 MIT 与适用的第三方许可，确认代码与素材的公开权，不提交个人任务、模型或数据库。具体构建与验证范围见 [发布验收记录](RELEASE_VALIDATION.md)。

下面的首次初始化和 Publish 教学只适用于尚未建立远程仓库的新项目。本项目已经公开，日后在现有仓库中正常 Commit、Push；不要重新初始化或通过 amend、强制推送改写已公开的历史。

## 1. 这些名称是什么意思

| 名称 | 可以怎样理解 |
| --- | --- |
| Git | 记录本地文件版本的工具，能查看每次改了什么 |
| GitHub | 保存远程 Git 仓库并协作的网站；不是你本机的 Git 程序 |
| Repository / 仓库 | 一份项目文件及其版本历史 |
| Stage / 暂存 | 为下一次提交挑选文件或改动，暂时还没有形成版本 |
| Commit / 提交 | 把已暂存的改动保存成一次本地版本；还没有上传网络 |
| Push / 推送 | 把本地提交上传到有权限的远程仓库 |
| Pull / 拉取 | 取得远程提交并合入当前分支，需要处理可能出现的冲突 |
| Branch / 分支 | 项目的另一条修改路线，例如 `main` 或功能分支 |
| Remote / 远程 | 本地仓库记住的远程地址；常用名称是 `origin` |
| `.gitignore` | 告诉 Git 哪些未跟踪文件不要纳入版本，例如 `.env` 和缓存 |
| GitHub Actions | 按仓库中的工作流运行检查、构建等任务的服务 |
| GHCR | GitHub Container Registry，存放 Docker 镜像的包仓库，与源码仓库不同 |

一次正常更新是：修改文件 → 查看差异 → Stage → Commit → Push → 查看 Actions → 验证 GHCR。以后不需要重新建仓库。

## 2. 安装与登录

1. 在 [GitHub 官方网站](https://github.com/signup) 注册并登录自己的账号，按要求完成验证。用于公开发布的账号应启用适当的账号保护。
2. 从 [Git 官方下载页](https://git-scm.com/downloads/) 安装 Git。Windows 安装后新开终端，Mac 按官方页面提供的方式安装。执行 `git --version`，能看到版本号表示终端已识别。
3. 从 [VS Code 官方下载页](https://code.visualstudio.com/download) 安装适用版本，重新启动 VS Code。
4. 点击 VS Code 左侧扩展图标，搜索并核对官方 **GitHub Pull Requests and Issues** 扩展；需要 PR 与 Issue 协作时安装它。基本 Git、克隆、提交和推送功能已经内置，不需要为了上传另装收费扩展。
5. 在发起 Publish/Push 或 GitHub 扩展登录时，按浏览器里的 GitHub 授权流程登录，完成后返回 VS Code。确认浏览器域名与授权应用，不把密码、Token 或设备授权码发给他人。没有自动回到编辑器时，按编辑器自己的官方提示完成回跳。
6. 选择 **File → Open Folder（文件 → 打开文件夹）**，只打开已经准备好的纯净发行目录。确认能看到 README、Compose、backend、frontend 和忽略规则。

菜单语言和位置可能随版本变化；可按 Windows `Ctrl+Shift+P` / Mac `Cmd+Shift+P` 打开命令面板搜索英文命令。对应功能的官方说明见 [VS Code 的 GitHub 教程](https://code.visualstudio.com/docs/sourcecontrol/github)。

### 网页登录与程序授权分别处理

GitHub 网页按该账号已配置的方式登录，例如 Google、Apple 或 passkey。账号若不支持密码登录，应使用原有可用方式；具体方式以账号设置和官方页面为准，不能从一次失败推断注册方式。HTTPS Git 推送不接受账户密码，VS Code 或 GitHub CLI 可通过各自的官方浏览器授权流程取得访问权限；网页登录与程序授权分别确认。登录验证与账号恢复由账号持有人在官方流程中操作，本教程不自动更改登录或恢复设置。参见 [GitHub 官方认证说明](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/about-authentication-to-github)。

网页已经登录，不表示 CLI 也已授权。若已安装 GitHub CLI，可用 `gh auth status` 检查其状态；若界面提示设备码过期，重新发起授权并使用新码。遇到失败先看页面或命令的具体错误，不猜测密码、账号或网络原因。密码、Token、设备码和验证码只在可信的官方流程中使用，不发送到聊天、Issue 或仓库。

维护者用 GitHub CLI 发布本项目时，登录命令同时请求 `workflow`，以便推送仓库内的 Actions 工作流文件：

```bash
gh auth login --hostname github.com --git-protocol https --web --scopes workflow
gh auth status --hostname github.com
```

已经登录、需要补充该权限时使用：

```bash
gh auth refresh --hostname github.com --scopes workflow
```

按官方浏览器页面确认所请求的权限；不要把 Token 发到聊天或写进文件来替代授权。这里的 OAuth `workflow` 与 Actions 任务中的 `GITHUB_TOKEN` 权限分别配置，不代表已取得 GHCR 发布权限或镜像已经公开。命令选项见官方 [`gh auth login`](https://cli.github.com/manual/gh_auth_login) 与 [`gh auth refresh`](https://cli.github.com/manual/gh_auth_refresh)。

## 3. 先确认是否已有仓库

点击左侧分支形状的 **Source Control（源代码管理）** 图标。若已经显示变更、分支和同步状态，通常已经有 Git 仓库，**不要再初始化**。

在 **Terminal → New Terminal（终端 → 新建终端）** 查看：

```bash
git status
git remote -v
git branch --show-current
```

如果 `git status` 说不是 Git 仓库，才进入首次初始化步骤。若已存在仓库但含私人历史，先由维护者生成经审计的独立发行目录，不能只删除当前文件就直接推送旧历史。

## 4. 首次初始化本地仓库

Source Control 中点击 **Initialize Repository（初始化仓库）**。正确结果是文件进入 Changes 列表，编辑器出现分支信息。没有按钮时，命令面板搜索 **Git: Initialize Repository**。

终端备用方式，仅在确认当前目录尚无仓库时执行：

```bash
git init -b main
```

第一次提交需要作者信息。在 GitHub 的邮件设置中找到自己的公开或 noreply 地址，再在此仓库中设置：

```bash
git config user.name "你的公开显示名"
git config user.email "你确认使用的提交邮箱或 GitHub noreply 地址"
```

作者信息会进入公开提交，不要填不愿公开的私人信息。登录 GitHub 与设置 Git 作者是两件事。

## 5. 检查将被上传的内容

在 Source Control 的 Changes 中逐个点文件，看中间编辑器的差异。README、源码、锁文件、迁移、Docker 文件和教程通常需要提交；个人素材、运行数据和密钥不应出现。

先检查 `.gitignore`、`.dockerignore` 是否覆盖 `.env`、数据库、模型、uploads、results、日志、备份、虚拟环境、`node_modules` 和缓存等实际目录。注意源码中可能也有合理的 migrations、templates 或 fixtures，不能只凭目录名一律删除。

本地核对命令：

```bash
git status --short
git status --short --ignored
git check-ignore -v .env
git ls-files
```

`git ls-files` 查看已经被 Git 跟踪的文件；忽略规则不会自动停止跟踪这些文件。若 `.env` 已进入暂存但从未公开，先在 Source Control 取消暂存，或按实际状态使用 `git rm --cached -- .env` 保留磁盘文件、移出索引。若密钥已经进入提交或远程，先撤销/轮换，再按 [安全说明](../SECURITY.md) 处理历史，不能只补一条忽略规则。

发现模型、视频、备份或私人 Word 时，停在本地检查，不继续 Publish。

## 6. Stage：选择下一次提交

把鼠标移到想提交的文件行，点击 **+**，文件会从 **Changes** 移到 **Staged Changes（暂存的更改）**。点暂存文件再看一次差异；误选时点 **−** 取消暂存，不会删除本地改动。

首次上传如果已经完整审查发行目录，才可以使用 Changes 旁的 **Stage All Changes**。平常更新建议逐文件选择，避免把新生成的数据带进去。

终端备用方式，明确列出你已检查的文件，例如：

```bash
git add README.md compose.yaml .gitignore .dockerignore
git diff --cached --stat
git diff --cached
```

这只是挑选四个文件的示例，不是完整首次发行清单。其余已审核源码、迁移、教程与工作流也需按真实清单暂存。

## 7. Commit：先保存本地版本

在 Source Control 顶部消息框写清这次变化，例如 `feat: prepare clean Docker release`，点击 **Commit（提交）**。正确结果是暂存列表清空，分支有新提交；这一步还没有上传。

终端备用方式：

```bash
git commit -m "feat: prepare clean Docker release"
git log -1 --oneline
```

提示没有作者时返回第 4 步；提示没有改动时检查是否 Stage、是否已经提交，不要重复初始化。

## 8. Publish：创建新的 GitHub 仓库

仅当尚未创建目标仓库且你有发布权时，点击 Source Control 中的 **Publish Branch（发布分支）**，或在命令面板搜索 **Publish to GitHub**。按提示登录、选择账号、填仓库名、核对文件范围与可见性。完成许可与纯净审查后，才选择 **Public**。

发布成功后应出现远程同步状态，网页可打开你的真实仓库。检查 `git remote -v`，确认 `origin` 指向正确账号与仓库。若已存在目标仓库，不要另建同名替代仓库，使用下一节连接它。

不同版本可能把发布入口放在 **… → Remote（远程）** 或命令面板；找不到按钮时使用下面的终端方式，不需要把 API Key 交给扩展。

## 9. 连接已有远程仓库并 Push

GitHub 网页创建目标空仓库时，不再自动添加第二份 README、LICENSE 或 `.gitignore`，因为发行目录已经有经审核的版本。复制仓库的 HTTPS 地址。

VS Code：Source Control 的 **… → Remote → Add Remote（添加远程）**，填实际地址，名称用 `origin`；再选择 **Push（推送）**。如果已有 `origin`，先核对它，不能盲目覆盖。

终端备用方式：

```bash
git remote add origin https://github.com/willzhang35929924-ui/AI_Video_Creation
git remote -v
git push -u origin main
```

只有地址确实错了才改：

```bash
git remote set-url origin https://github.com/willzhang35929924-ui/AI_Video_Creation
```

浏览器按提示完成 GitHub 登录；当前账号必须有该仓库写权限。GitHub HTTPS 推送不能用普通账号密码代替受支持的认证流程。不要将凭据写进 remote URL、命令历史或仓库文件。

如果远程已经有 README、许可或别的提交，初次 Push 可能被拒绝。先停止，把远程 clone 到新目录，再合并你需要发布的文件并检查差异；不要用 `--force` 覆盖未知历史。

## 10. 在 GitHub 网页验收

打开真实仓库，检查默认分支、最新提交、README、安装教程、Compose、源码、迁移和工作流文件都存在。确认没有数据库、模型、个人素材、日志、`.env` 或备份。

再点击 **Actions**，打开本次运行，查看检查、构建和发布任务的具体结果。源码 Push 成功不等于镜像构建成功。打开 GHCR Packages 核对镜像版本、公开可见性和仓库关联，按 [Docker 发布教程](DOCKER_RELEASE_GUIDE_ZH.md) 验证多架构清单、匿名拉取和空卷部署。

## 11. 后续修改与更新

已有仓库的日常流程：

1. 在 Source Control 检查本地未提交改动，先提交或用编辑器的 Stash 保存；不要丢弃有用文件。
2. 从 **… → Pull（拉取）** 取得远程更新。终端可用 `git pull --ff-only`；若存在分叉或冲突，先处理，不强行覆盖。
3. 开始修改，逐个查看 Changes，运行适合这次变化的检查。
4. Stage → 输入说明 → Commit → Push。**Sync Changes（同步更改）** 可能包含先拉取再推送，点击前看清同步数量与提示。
5. 查看 GitHub Actions 的真实结果和新镜像，再按文档更新自己的运行环境。

终端对应示例：

```bash
git status
git pull --ff-only
git diff
git add README.md
git diff --cached
git commit -m "docs: clarify installation steps"
git push
```

多人协作时从功能分支提交 PR，审核后合入发布分支。不要每次更新都 `git init`，也不要反复创建新 GitHub 仓库。

## 12. 在另一台电脑继续工作

VS Code 命令面板选择 **Git: Clone**，粘贴真实仓库地址，选保存位置，完成后点 **Open**。需要本地构建和终端工作时使用真实 clone，不将只在远端浏览文件的虚拟工作区当成本地源码目录。

终端方式：

```bash
git clone https://github.com/willzhang35929924-ui/AI_Video_Creation video-factory
cd video-factory
git status
```

Git 不保存你的 Docker 数据卷。另一台电脑要恢复私人业务数据，另按完整备份恢复步骤操作，不通过把数据库 Push 到 GitHub 来搬家。

## 13. 常见错误

| 现象 | 处理 |
| --- | --- |
| Git 未安装 / VS Code 找不到 Git | 验证 `git --version`；重开终端和 VS Code，确认安装器完成与 PATH 生效 |
| 无法登录或没有回跳 | 确认浏览器 GitHub 账号、授权域名、网络/代理和 VS Code 通知；重新走编辑器的官方认证，不到聊天里发送 Token |
| 没有 Push 权限 | 核对登录账号、仓库归属与协作者权限；贡献别人的项目先 fork 并发 PR |
| remote 已存在 | `git remote -v` 先看地址，确认是否应该复用；不要无条件删 remote |
| Push 被拒绝 / non-fast-forward | 远端有本地没有的提交，先获取并合并或在新 clone 中合并发行文件；不要直接强推 |
| 文件太大 | 检查是否误选模型、视频、数据库或备份；移出索引并补忽略规则。个人运行数据不应通过 LFS 公开发布 |
| 误加入 API Key | 立即撤销/轮换；撤回当前文件不足以清理历史，按安全说明处理，不在公开 Issue 重贴密钥 |
| Actions 构建失败 | 点失败步骤查看真实日志，区分依赖、测试、架构和权限问题；修好提交再运行，不把失败步骤删掉掩盖问题 |
| GHCR 无权限或用户拉不到 | 检查 `packages: write`、组织策略、包与仓库关联、包公开性。不能把个人 Token 硬编码进工作流 |
| ARM64 构建失败 | 检查依赖与基础镜像是否支持 ARM64，查看对应构建日志；AMD64 通过不能替代 ARM64，模拟构建不能替代 Mac 实机验证 |

Docker 管理与恢复见 [Docker 教程](DOCKER_RELEASE_GUIDE_ZH.md)。需要反馈前，再按 [故障排查](TROUBLESHOOTING_ZH.md) 脱敏诊断材料。
