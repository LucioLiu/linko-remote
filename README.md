# 邻控 · Linko Remote
**致力于打造一个 AI 易用的「UU 远程」。**

让 AI 能可靠地使用另一台电脑。图形操作负责必须看屏、点击和输入的任务；SSH 入口服务已有 SSH 环境的电脑和服务器。

Linko Remote，中文名「邻控」，日常简称 Linko。现阶段支持 Mac 后台控制 Windows，并提供 SSH 连接管理和系统终端入口。[产品定位](docs/branding.md)

> 0.7.0 为预览版本。完整的人工实时桌面控制器、AI SSH 命令任务，以及通用公共设备目录仍待建设。当前新 Mac 需配置一次中继，Windows 公开下载包可粘贴 Mac 复制的完整连接码；专用包也可输入 12 位数字配对。旧 LanPilot 命令、配对与存储路径保留兼容。

- 屏幕操作：先取当前画面，在该设备、该画面上执行动作，再检查回执和实际变化。
- 批量作业：把受支持的任务发给 Windows 本机执行，收回结果文件，并按需检查屏幕。断线后先查询原任务状态，避免重复执行。
- 连接方式：支持局域网，以及配置好的跨网路由。配对凭据、设备配置和原始屏幕仅保存在部署环境。
- 开发验证：在项目根目录运行 `python3 -m unittest discover -s tests`。Windows 原生测试在 Windows 上运行。

## 下载预览版

[macOS 控制端](https://github.com/LucioLiu/linko-remote/releases/download/v0.7.0-preview.1/LinkoRemote-macOS-0.7.0-preview.zip) · [Windows 接入端](https://github.com/LucioLiu/linko-remote/releases/download/v0.7.0-preview.1/LinkoRemote-Windows-0.7.0-preview.zip) · [版本说明与校验文件](https://github.com/LucioLiu/linko-remote/releases/tag/v0.7.0-preview.1)

Windows 解压后双击「打开邻控.cmd」，粘贴 Mac 端「复制连接码」的内容。Mac 需有可用的系统 Python 3，并按[说明](docs/invitations.md)配置一次可达中继。当前优先支持新设备接入；旧设备请先用独立测试实例验收，保留原有工作连接。

## 0.7.0：验证码连接与设备管理

Mac 首页可生成 10 分钟有效的配对验证码；Windows 使用可复用客户端输入验证码接入，不再逐台传递邀请或公钥。首页支持电脑改名、搜索、画面快照和 SSH 连接管理。跨网使用配置好的中继入口。当前 Windows 客户端包含本控制端的公开网关配置；新控制端仍需一次性配置中继，并非通用的全局设备目录。详见[连接改进与验收边界](docs/2026-11-19-connection-upgrade.md)。

构建 Mac 应用：在具备系统 Python、Swift 编译器和签名工具的 Mac 上运行 `python3 lanpilot/build_mac.py /tmp/邻控.app`。命令行入口为应用中的 `Contents/Resources/lanpilot`；可用 `--help` 查看子命令。[本地文件作业](docs/local-jobs.md)列出了可执行任务和结果验证方法。

旧邀请文件方式仍可使用：在 Mac 应用点「更多接入」，为设备取名，把生成的安装邀请通过私密渠道交给目标电脑。Windows 用户双击并确认接入。邀请默认 15 分钟有效，仅能绑定一台设备。接入前需由管理员为控制端配置一次可达的邀请路由；后续设备共用该入口，不必逐台填写 IP。安装邀请内含一次性凭据，请勿提交到 Git。

| 你要完成的任务 | 从哪里开始 |
| --- | --- |
| AI 后台查看 Windows 屏幕、点击控件，不占用 Mac 前台 | MCP：`list_devices` → `observe` → `act`；[操作指南](skill/lanpilot/SKILL.md) |
| 多台电脑并发处理文件，弱网下减少鼠标往返 | `stage_inputs` → `run_job`；[作业清单](docs/local-jobs.md) · [远控与本机执行对照](docs/route-comparison.md) |
| 从不同 Wi-Fi 接入新的 Windows 电脑 | [一次性邀请与跨网配置](docs/invitations.md) |

[MCP 接入与设备选择](AGENTS.md)
