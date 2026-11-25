# 邻控连续桌面与跨网文件传输

首个优化场景是 Mac 操作 Windows 的当前桌面。验收看滚动、拖窗、点击反馈和表格小字，兼顾同一连接下的大文件传输。逐步取屏、动作编号、回执和撤销授权继续用于需要确认结果的操作。

## 产品和技术对照

| 对照 | 可借鉴的机制 |
| --- | --- |
| KDE Connect、KDE Plasma KRdp | 设备配对后可使用独立的文件分享、SFTP 浏览和虚拟触控板。Plasma 的图形远控由 KRdp 提供。邻控现有 SSH 入口可继续用于终端，已配置 OpenSSH 的主机可试用 SFTP。 [KDE Connect](https://userbase.kde.org/KDEConnect#Remote_File_System_Browser) · [KRdp](https://github.com/KDE/krdp) · [OpenSSH SFTP](https://man.openbsd.org/sftp) |
| Chrome Remote Desktop、Tailscale | 按实际连通性选直连或中继，显示目前路径。Chrome Remote Desktop 使用 ICE，优先 UDP 并可回退 TCP。 [Chrome 网络指南](https://support.google.com/chrome/a/answer/16364503?hl=en) · [Tailscale 连接类型](https://tailscale.com/docs/reference/connection-types) |
| RustDesk | 参考其 Windows 采集与编码协商，以及检测排队延时后调低码率和帧率的做法。 [视频服务](https://github.com/rustdesk/rustdesk/blob/master/src/server/video_service.rs) · [视频 QoS](https://github.com/rustdesk/rustdesk/blob/master/src/server/video_qos.rs) |
| Sunshine、Moonlight、Parsec | 参考 GPU 取屏和编解码、低延迟视频流，以及网络、丢帧、解码、渲染的分项统计。 [Sunshine 配置](https://docs.lizardbyte.dev/projects/sunshine/latest/md_docs_2configuration.html) · [Moonlight 统计说明](https://github.com/moonlight-stream/moonlight-docs/wiki/Frequently-Asked-Questions#what-do-the-numbers-mean-in-the-on-screen-connection-statistics) · [Parsec 技术说明](https://parsec.app/technology) |
| Microsoft RDP、网易 UU 远程 | 办公场景单独测小字和表格滚动，文件传输提供单独入口和进度。 [RDP 带宽说明](https://learn.microsoft.com/en-us/azure/virtual-desktop/rdp-bandwidth) · [UU 远程官方功能](https://adl.netease.com/d/g/uuremote/c/brand_broadcast1) |

## 第一轮代码

原有 Windows 取屏使用 GDI 全屏捕获、缩放和 JPEG，写盘完成后再等 120 毫秒。改为从取帧开始计时，仍限制每轮一次，键鼠先于取屏处理。客户端上传 JPEG 的 60 毫秒节奏计入请求时间，继续保留最新帧。

查看端和被控端请求可在中继等待最多 250 毫秒。新帧、输入或回执到达即唤醒对应请求。等待释放中继锁和 SQLite 事务，返回前重新校验权限及输入期限。首个被控端交换立即领取租约，后续再启用等待。连接旧中继时，客户端按原频率轮询。两端和中继一起升级后可获得完整收益。

## 复测

基准分两层。回环 TLS 基准用 120 毫秒合成帧、实际中继和客户端逻辑，在两段各加人工网络延时。它记录首帧、到达帧率、采集到缓存、从输入到后续画面进入缓存以及空闲请求数。它不运行 Windows GDI、原生键鼠、JPEG 解码或屏幕渲染。带宽选项按单帧模拟串行化，实网拥塞与丢包仍需单独测量。

```sh
PYTHONPATH="$PWD" python3 tests/live_network_benchmark.py --rtt-ms 100
PYTHONPATH="$PWD" python3 tests/live_network_benchmark.py --rtt-ms 100 --jitter-ms 25 --mbps 5
PYTHONPATH="$PWD" python3 tests/live_network_benchmark.py --rtt-ms 100 --jitter-ms 25 --mbps 5 --jpeg-kib 48
```

对照基线使用 `6c8aa55`。在候选工作树运行以下命令可以复测基线，附加参数与候选保持一致。

```sh
baseline_dir=$(mktemp -d)
git archive 6c8aa55 | tar -x -C "$baseline_dir"
PYTHONPATH="$baseline_dir" python3 tests/live_network_benchmark.py --rtt-ms 100
```

每次采集 10 秒活跃数据，包含 15 次模拟输入。RTT 指每一端到中继的模拟往返时间。

| 模拟条件 | 到达帧率 | 输入到对应画面进入缓存 P95 | 采集到缓存 P95 |
| --- | ---: | ---: | ---: |
| 30 ms，96 KiB 帧，基线 → 候选 | 8.1 → 8.4 fps | 373 → 285 ms | 199 → 122 ms |
| 100 ms，96 KiB 帧，基线 → 候选 | 4.9 → 7.2 fps | 807 → 618 ms | 427 → 326 ms |
| 200 ms，96 KiB 帧，基线 → 候选 | 3.0 → 4.2 fps | 1153 → 979 ms | 592 → 510 ms |
| 100 ms，±25 ms，5 Mbps，96 KiB 帧，基线 → 候选 | 2.5 → 2.8 fps | 1169 → 1132 ms | 713 → 792 ms |
| 同一限带宽条件，候选 96 → 48 KiB 帧 | 2.8 → 4.1 fps | 1132 → 875 ms | 792 → 557 ms |

限带宽时，96 KiB 原始帧的 Base64 负载约 128 KiB，按 5 Mbps 计算，两段合计约 420 ms 的串行化时间。帧体积实验只改变模拟负载，用于决定优先验证视频编码和码率控制。图像清晰度和实机体感仍需实测。

实机复测固定两端设备、分辨率、版本和测试窗口。依次跑已验证的同一局域网、不同网络的直连、不同网络的中继、限带宽和丢包。记录首帧、活跃帧率、最长帧间隔、输入到本机显示的 P50 和 P95、传输速率和两端 CPU/GPU。用测试窗口响应点击后变色，由 Mac 一端计时，区分键鼠回执与肉眼看到的反馈。对照表格滚动、拖窗、输入中文、网络断开再连、撤销授权和按键释放。用同一条路线复测并发大文件传输。

## 后续迭代

1. **Windows 原生视频。** 做 DXGI Desktop Duplication 到低延迟 H.264 的独立试验，与 GDI/JPEG 在同一台机器对照。优先验证办公小字、显卡编解码是否可用、锁屏后恢复和显示器切换。Mac 用 VideoToolbox 比较接收成本。原始帧排队保留最新，发生编码帧依赖中断时请求关键帧。先验证 720p 20 帧/秒和 1080p 30 帧/秒两个档位，再据实确定默认值。 [Windows DXGI](https://learn.microsoft.com/en-us/windows/win32/direct3ddxgi/desktop-dup-api) · [Media Foundation 低延迟](https://learn.microsoft.com/en-us/windows/win32/medfound/codecapi-avlowlatencymode) · [VideoToolbox](https://developer.apple.com/documentation/videotoolbox/decompression-properties)
2. **跨网直连。** 固定编码器比较现有 TLS 路线和 WebRTC ICE 直连／TURN。配对和授权提供会话身份，直连失败时仍可走中继。视频拥塞时降低画质或帧率，点击和键鼠边沿保持可靠、有序和可确认。 [WebRTC 统计定义](https://www.w3.org/TR/webrtc-stats/) · [Chrome Remote Desktop 网络指南](https://support.google.com/chrome/a/answer/16364503?hl=en)
3. **跨网文件。** 先用已配置的 OpenSSH 测 SFTP 收发体验，通用设备传输单独授权和选择接收目录。候选实现提供分块、断点续传、完成校验和限速。在传输暂停、续传和目标同名文件场景验证，并记录并发时的键鼠 P95。逻辑通道共享拥塞窗口时仍需限制传输速率。 [Syncthing 块交换协议](https://docs.syncthing.net/specs/bep-v1.html) · [WebRTC 数据通道标准](https://www.rfc-editor.org/rfc/rfc8831.html)

## 2026-11-25 家庭 Mac 到公司 Windows 公网实测

两端位于不同网络。用户确认 UU 的「联想」与邻控的「华为（宁朋芝交接）」为同一台机器。邻控统一版的这台 Windows 在线，Yoga 在邻控与 UU 的设备列表都显示离线。各次连续桌面会话依次运行，关闭后再开始下一次。采样只记录单调时钟、帧序号、负载大小及计数，不写入屏幕图像。

| Mac 侧接收端 | 采样时长 | 有效新帧 | 从请求连接到第一帧 | 相邻新帧间隔中位数 |
| --- | ---: | ---: | ---: | ---: |
| 已安装的 0.8.2-preview.9 | 30 秒 | 22 | 6.48 秒 | 1.013 秒 |
| 1e82039 接收端，现网中继和公司端 | 30 秒 | 24 | 5.23 秒 | 1.041 秒 |
| 1e82039 接收端，重复采样 | 30 秒 | 23 | 7.01 秒 | 1.018 秒 |

首帧和间隔统计到 Mac 接收缓存，未计屏幕显示。未安排持续运动场景。第一次额外检查鼠标回执时，12 秒内没有观察到缓存年龄小于 500 ms 的画面。另一次会话在缓存年龄小于 1000 ms 时分别发起五次只移动鼠标的操作。Mac 从提交到看到 Windows 执行回执分别为 3755、1450、1819、3408、2748 ms，五次中位数 2748 ms。未测到按键发出至屏幕像素变化。

重复采样中，有图请求耗时中位数 603 ms，空请求 299 ms；一次图像 Base64 负载中位数 82636 字节。现网中继未返回长轮询等待字段。取消旧中继轮询的额外 60 ms 间隔，单轮帧间隔中位数为 1065 ms，P95 为 1990 ms，未保留该调整。另一次 20 秒采样中，14 次带完整图像的返回有 13 次与上一张字节完全相同，完整图像负载中位数 82576 字节。

新候选中继和接收端在采集序号更新且像素编码与几何信息完全相同时，传递新序号并复用上一帧图像。画面过期仍按实际采集更新状态处理。客户端落后且期间内容有变化时，或旧版未声明支持复用时，返回完整 JPEG。此改动优化中继到 Mac 的一段，下行节省量取决于桌面内容；公司端上传、滚动视频编码和公网往返时间还需分别验证。

可使用以下命令对已配对、在线且允许访问的设备复测。输出的重复整帧计数只统计收到的完整图像。

```sh
PYTHONPATH=. python3 tests/live_wan_benchmark.py <已配对设备ID> --seconds 30
```

UU 4.39.1 在同一条路线两次接受连接请求，约 1.8 秒和 1.45 秒后活动连接均为空，未取得首帧或会话统计。不能据此给 UU 与邻控排出速度名次。公开参考中，Nate Randa 用相机同拍主从两屏，测得有线环境下 Parsec、Sunshine/Moonlight、RustDesk 和 AnyDesk 的平均画面时差依次为 13.5、32.6、45.9 和 67.7 ms；60 Hz 两屏约有一帧读数误差。该指标不同于上表的连续更新间隔。[测试原文和原始图表](https://www.nateranda.com/blog/desktop-server/)

固定重复图像、每端到中继 RTT 100 ms、每段限速 5 Mbps、96 KiB 原始帧的本地 TLS 对照中，以 1e82039 为基线，采集至 Mac 缓存 P50 从 645 ms 降到 419 ms，P95 从 758 ms 降到 556 ms。两版各采样 5 秒，均收到 14 帧（2.8 fps）。该项只验证重复画面的接收耗时。新实现还用每帧内容都变的负载完成了相同测试。

## 公网中继上线后的复测

2026-11-25，原有公网中继从 preview.6 切换为兼容旧客户端的性能版本；数据库、TLS 身份和配对沿用原配置，旧代码目录留存。先上线 32752e0，再上线 89fdad9 的 JPEG 字节差分。候选 Mac 代码直接作为接收端；公司的华为 Windows 仍使用原有发送端。本轮无 Yoga 的在线样本。

- 切换前的 20 秒目标窗口在第 16.25 秒断开，收到了 10 张完整 JPEG，9 张与上一张相同。切换后一次 30 秒窗口跑满，接收 1 张完整图（82,588 字节）和 24 次复用引用，采样看到 24 个有效序号，帧间隔 P50 1046 ms，P95 1862 ms。另一次窗口在 12.16 秒断开。接收序号含重新采集到的同一桌面，无法由此推算画面运动帧率。
- 30 秒测试期间，中继上大量接收图像的 TCP 连接约收到 2.33 MB，内核记录的平滑 RTT 中位数 187 ms；主要发送图像的连接约发出 104 KB，平滑 RTT 中位数 70 ms。按这两条连接估算，仅一次经中继到对端再返回就约 257 ms；实际的取帧轮询、输入执行和显示还需额外时间。
- 远端 Windows 在采集图像上绘制鼠标十字。首次测得移动鼠标到 Mac 解码后目标区域改变为 4033 ms，同次执行回执为 2739 ms。后续下行差分上线后，增强的探针要求目标附近变化、相邻对照区域基本不变；一次样本为 3165 ms，回执 2415 ms。该窗口内收到了 31,340 字节的 Base64 差分；另有仅收到回执及未确认即断开的样本。每组成功样本过少，不能把两次延迟差值归因于差分。
- Windows 候选包加入上传复用和空闲状态下短时网络抖动后的续接，Mac 候选包能重建局部变化的 JPEG。169 项 Python 测试、18 项 Windows 租约模拟场景及 6 项独立视频原型测试通过。Windows 原生实机安装和新版两端组合尚未验收。
- 软件 H.264 实验在 M1 Pro 上以全屏滚动文字对比同为 4:4:4 的全帧 JPEG。720p 和 1080p 的整帧 RGB PSNR 差距小于 0.014 dB；H.264 的文字边缘像素 PSNR 约低 1 dB。720p 平均载荷从 257.53 到 6.02 KiB/帧，1080p 从 579.45 到 8.34 KiB/帧。另把固定序列从公网中继主机经 SSH 实际传到家中 Mac，20 fps 的 80 帧 720p 画面分别跑两次。H.264 完整交付约 4.56 和 4.97 秒，JPEG 约 26.97 和 17.20 秒。此对照只含下行和解码，独立于公司电脑采集，也不表示产品已使用 H.264。
