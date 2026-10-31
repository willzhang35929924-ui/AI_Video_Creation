# 许可证与第三方分发审查

权利人已确认第一方代码采用 MIT，正式授权以仓库 `LICENSE` 为准。本说明记录第三方分发审查边界，不替代任何组件的许可证。源码与镜像已经公开，后续版本仍须保留适用的许可、版权与源码说明，按实际变更复核分发义务。

## 已确定的第一方许可

| 选项 | 主要区别 |
| --- | --- |
| MIT | 条文简短、允许广泛使用与修改分发，保留版权和许可声明；没有 Apache-2.0 那样的明确专利授权条款 |
| Apache-2.0 | 同为宽松许可证，包含明确专利授权与相关终止条件；分发时按条款保留许可、变更说明及适用的 NOTICE |

本项目使用 MIT；上表说明它与 Apache-2.0 的区别，不改变任何第三方组件原有条款。原文分别见 [OSI MIT](https://opensource.org/license/mit) 与 [Apache-2.0 官方文本](https://www.apache.org/licenses/LICENSE-2.0)。分发时仍保留正式 `LICENSE` 以及适用的第三方版权、许可和 NOTICE。

## 必须分别审查的对象

- 第一方源码、文档、模板与工作流是否确有公开分发权。
- Python、npm 与系统软件包的实际锁定版本、许可证和必要版权声明；依赖可能混合 MIT、BSD、ISC、Apache、MPL、CC 等，不统一改成第一方许可。
- Nginx、Python、Node 基础镜像及镜像中的运行软件、字体、编解码器。源码仓库许可与容器内组合软件的义务分开处理。
- 第三方复制代码、工作流和测试 fixture 的来源及许可。含 AGPL 或其他特殊条款的外部目录不能仅因位于同一磁盘就复制进公开发行版；来源不清的文件先核实。
- 示例图片、视频、音频、人物、品牌/产品照片和字体是否允许公开。私人输入或生成结果不作为发行样例。
- 模型、LoRA、Checkpoint、下载缓存及 ComfyUI 第三方节点的使用与再分发条款。应用仅连接模型不等于能把权重放进镜像；本通用镜像不分发模型权重。

## FFmpeg 与字体

FFmpeg 的许可取决于实际构建选项和链接组件，特别是启用 GPL 组件时的组合。维护者需针对镜像真实安装的 FFmpeg/编解码器版本记录软件包来源、许可与对应源代码取得方式，不能只写“FFmpeg 免费”或贴一个主页地址代替相关分发义务。官方说明：[FFmpeg License and Legal Considerations](https://ffmpeg.org/legal.html)。

检查实际构建：

```bash
docker compose exec backend ffmpeg -version
docker compose exec backend ffmpeg -buildconf
```

中文字体同样保留其版权和许可证，具体以镜像安装的软件包为准。必要的 `NOTICE`、第三方声明和许可证全文应随正式发行版提供，而不是把所有组件重新许可为 MIT 或 Apache-2.0。

本项目的后端镜像构建配置将匹配 Debian FFmpeg / x264 的源码归档、版权文件和版本元数据保存在 `/usr/share/video-factory/corresponding-source`。正式发布前维护者必须核对该目录实际存在、源码版本与安装二进制一致；配置中写了路径不等于已经验证分发合规。用户可从自己的后端容器复制随附文件：

```bash
docker compose cp backend:/usr/share/video-factory/corresponding-source ./corresponding-source
```

## 发布前需要留下的记录

维护者应保留已确认的 MIT 许可决定、第三方组件清单与版本、可疑来源文件的审查结论、镜像系统包和 FFmpeg 构建信息、保留的许可与对应源码说明，以及公开 fixture 的来源。未解决项按实际情况标为 `BLOCKED` 或 `NOT RUN`，不能写成全部已通过。

当前第一方许可：`MIT`。审查与发布实际状态见发布验收记录：[发布验收记录](RELEASE_VALIDATION.md)。
