# 邻控 · LanPilot
**Windows remote desktop automation via MCP from macOS.** Control multiple paired Windows PCs concurrently, verify screenshots, or run supported local batch jobs over LAN or WAN.

从 Mac 后台观察和操作已配对的 Windows 电脑。每台电脑有独立的连接与任务状态，多台可以同时工作。

- 屏幕操作：先取当前画面，在该设备、该画面上执行动作，再检查回执和实际变化。
- 批量作业：把受支持的任务发给 Windows 本机执行，收回结果文件，并按需检查屏幕。断线后先查询原任务状态，避免重复执行。
- 连接方式：支持局域网，以及配置好的跨网路由。配对凭据、设备配置和原始屏幕仅保存在部署环境。
- 开发验证：在项目根目录运行 `python3 -m unittest discover -s tests`。Windows 原生测试在 Windows 上运行。

构建 Mac 应用：在具备系统 Python、Swift 编译器和签名工具的 Mac 上运行 `python3 lanpilot/build_mac.py /tmp/邻控.app`。命令行入口为应用中的 `Contents/Resources/lanpilot`；可用 `--help` 查看子命令。[本地文件作业](docs/local-jobs.md)列出了可执行任务和结果验证方法。

添加新的 Windows 电脑：在 Mac 应用点「添加电脑」，为设备取名，把生成的安装邀请通过私密渠道交给目标电脑。Windows 用户双击并确认接入。邀请默认 15 分钟有效，仅能绑定一台设备。接入前需由管理员为控制端配置一次可达的邀请路由；后续设备共用该入口，不必逐台填写 IP。安装邀请内含一次性凭据，请勿提交到 Git。

| 你要完成的任务 | 从哪里开始 |
| --- | --- |
| AI 后台查看 Windows 屏幕、点击控件，不占用 Mac 前台 | MCP：`list_devices` → `observe` → `act`；[操作指南](skill/lanpilot/SKILL.md) |
| 多台电脑并发处理文件，弱网下减少鼠标往返 | `stage_inputs` → `run_job`；[作业清单](docs/local-jobs.md) · [远控与本机执行对照](docs/route-comparison.md) |
| 从不同 Wi-Fi 接入新的 Windows 电脑 | [一次性邀请与跨网配置](docs/invitations.md) |

[MCP 接入与设备选择](AGENTS.md)
