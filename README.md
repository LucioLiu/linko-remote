# 邻控 · Linko Remote

**致力于打造一个 AI 易用的「UU 远程」。**

每台电脑使用同一套界面，都可以连接另一台电脑，也可以在本机确认后接受连接。支持临时连接码、电脑改名、逐次取屏和键鼠操作，以及 SSH 连接管理。

## 下载统一预览版

[Windows 10/11 x64](https://github.com/LucioLiu/linko-remote/releases/download/v0.8.2-preview.7/LinkoRemote-Windows-0.8.2-preview.7.zip) · [macOS 14+ Apple Silicon](https://github.com/LucioLiu/linko-remote/releases/download/v0.8.2-preview.7/LinkoRemote-macOS-arm64-0.8.2-preview.7.zip) · [版本说明与校验文件](https://github.com/LucioLiu/linko-remote/releases/tag/v0.8.2-preview.7)

两端均自带运行环境。Windows 解压后双击「打开邻控.cmd」；Mac 将应用移入「应用程序」后打开。本预览版未做 Apple 开发者公证，macOS 可能提示来源限制。

1. 双方安装同一统一预览版。
2. 接收连接的电脑开启「允许其他电脑连接本机」，生成 10 分钟有效的连接码。
3. 另一台电脑输入连接码；接收电脑核对名称和设备号，允许本次连接。
4. 点击「打开桌面」，先取新画面，再进行操作。反向连接需要另行批准。

**当前可用范围：** 两台 Windows 已通过公网中继完成双向取屏、点击和文字输入实测；Mac 可主动连接 Windows，Mac 被控尚未完成实机验收。当前采用逐次截图和操作确认，尚非连续视频桌面。测试中使用公网入口，未验证机器分处两个实际网络的场景。测试中继依赖临时公网隧道，可能延迟或暂时不可达，不承诺生产可用性。

## 检查与安装更新

启动后自动检查 GitHub 新版本，每 4 小时再检查一次。也可在「设置与帮助 → 软件更新」手动检查；发现新版后显示提示和更新说明，点击下载、校验完成后选择「安装并重启」。自动检查可关闭，不会静默替换正在使用的软件。

更新前需结束远程连接、确认未完成操作并关闭允许连接。更新保留电脑身份、名称、配对和 SSH 设置；下载或校验失败不会安装，替换失败会恢复或保留上一版。0.8.0 及更早版本没有更新入口，需要先手动升级一次到 0.8.1。

## 干净安装与隐私

- 安装包不包含开发者的设备列表、旧配对、连接验证码、私钥、截图或运行日志。
- 首次启动生成独立随机身份和通用电脑名称，不读取系统主机名。设备列表初始为空；只显示经连接并批准的电脑。
- 公共连接服务地址与 TLS 证书指纹是公开服务配置，不是电脑连接凭据。仅知道地址或设备号不能取得桌面权限。
- 允许连接默认关闭。新版连接服务支持对已批准的设备长期授权。关闭接收会暂停远控，重新启用后可继续连接，也可在设备列表撤销授权。反向控制单独批准。
- 预览中继会处理设备名称、临时连接申请及桌面回执。建议只在测试电脑使用；源码支持自建服务。详见[统一预览说明](docs/unified-preview.md)。

统一版使用独立配置目录：Windows `%LOCALAPPDATA%\LinkoRemote`，macOS `~/Library/Application Support/LinkoRemote`。旧 0.7 安装和配对保留在旧客户端，尚未自动迁移；连接双方均需使用统一版。

## 开发与验收

运行 `python3 -m unittest discover -s tests`。Windows 原生检查需在 Windows 上运行。[构建与发布检查](docs/releasing.md)说明安装包的文件白名单、校验值和隐私扫描。

SSH 入口使用系统 OpenSSH 和已有配置，保存主机、用户名和端口，不代存密码，也不提供 SSH 自动穿透。

旧 0.7 的 Mac → Windows 后台 MCP、受限文件作业与邀请流程仍保留兼容：[MCP 操作](skill/lanpilot/SKILL.md) · [文件作业](docs/local-jobs.md) · [旧版接入](docs/invitations.md)。统一版的图形会话仅授予取屏与键鼠能力，不授予文件作业权限。

[产品定位](docs/branding.md)。项目与网易 UU 远程没有隶属或集成关系。
