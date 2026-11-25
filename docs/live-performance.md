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
