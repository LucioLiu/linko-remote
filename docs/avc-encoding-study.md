# H.264 弱下行编码对照

基线为 `1ebc331`。这一版已经缩短 AVC 下行响应并能跳过积压画面。本次固定该版传输、授权与 Viewer，只改变同一份 1280×720 滚动文字片源的编码参数。随后在新版中单独验证自动降档和恢复。

## 方法

发送端用合成 Windows mailbox 走产品 Python 代理和持续 AVC 上传。中继、证书指纹校验、授权、输入、客户端服务和离屏 WKWebView 均走实际产品路径。限速代理在回环 TCP 上双向各加 300 ms，只有中继到 Viewer 的方向限速。每个条件串行运行，试验进程以 `nice -n 15` 启动。机器当时也在跑公网和多会话实验，原始记录保留了每轮的系统 load average。实际 Windows 捕获、公网拥塞窗口、随机丢包和显示器刷新均属于待实测变量。

片源是同一组连续滚动的 14 px 小字、交替底色和变化标记。所有档位为 libx264 Baseline、20 fps、最长 20 帧 GOP、无 B 帧。缩小画面之前使用相同的源像素，帧头的物理桌面仍为 1280×720。实际输入走授权控制层，合成 worker 核对原桌面几何、坐标和严格递增的输入序号，然后通过产品路径回传回执。浏览器绘制率以完整试验窗口为分母，从发起连接开始。帧龄从同机合成采集计到真实 Canvas 绘制。回执时间从 Viewer 排队计到 Viewer 看到已执行序号。

从实际 Canvas 每约 2.6 秒采样一个固定大小的文字区域。分辨率不同的档位使用相同的物理桌面区域和输出尺寸，以 macOS Vision 严格匹配行号、四位引用号和文案。OCR 在计时窗口结束后运行。它是密集移动小字的辨识代理，没有做人眼识读试验。

| 档位 | 编码画面 | 目标码率 | VBV | 用于比较 |
| --- | --- | ---: | ---: | --- |
| 原档 | 1280×720 | 3,500 kbps | 400 kbit | 同链路基线 |
| 只降码率 | 1280×720 | 750 kbps | 400 kbit | 单独比较目标码率 |
| 小关键帧 | 1280×720 | 750 kbps | 100 kbit | 在同码率下缩小 VBV 与突发 IDR |
| 540p | 960×540 | 750 kbps | 100 kbit | 同目标码率和 VBV 下缩小画面 |
| 360p | 640×360 | 750 kbps | 100 kbit | 更激进的画面缩小 |

## 结果

回环代理实测请求 RTT 中位数为 605–610 ms。原档和小关键帧各在两个带宽条件下重测一轮，复测反转了档位与带宽顺序。机器为 8 核 Apple M1 Pro，各轮开始前一分钟 load average 为 16.3–28.6。表内双轮值是两轮各自统计量的范围。

| 下行 | 档位 | 轮次 | 实绘 FPS | 帧间隔 P95 ms | 帧龄 P95 ms | 送达回执 / 入队 | OCR 全对 / 样本短语 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 Mbps | 原档 | 2 | 1.77–1.84 | 1034–1113 | 984–1022 | 3 / 7 | 0 / 38 |
| 1 Mbps | 只降码率 | 1 | 2.84 | 1025 | 852 | 1 / 5 | 0 / 26 |
| 1 Mbps | 小关键帧 | 2 | 3.92–5.38 | 802–816 | 529–619 | 7 / 12 | 0 / 58 |
| 1 Mbps | 540p | 1 | 4.93 | 769 | 667 | 3 / 6 | 0 / 28 |
| 1 Mbps | 360p | 1 | 5.46 | 722 | 650 | 3 / 7 | 0 / 26 |
| 1.6 Mbps | 原档 | 2 | 1.69–1.92 | 993–1006 | 675–706 | 4 / 11 | 0 / 50 |
| 1.6 Mbps | 只降码率 | 1 | 3.31 | 967 | 584 | 3 / 5 | 0 / 24 |
| 1.6 Mbps | 小关键帧 | 2 | 3.46–3.61 | 830–844 | 480–499 | 6 / 12 | 0 / 48 |
| 1.6 Mbps | 540p | 1 | 3.15 | 873 | 489 | 3 / 5 | 0 / 26 |
| 1.6 Mbps | 360p | 1 | 3.15 | 847 | 492 | 3 / 6 | 0 / 24 |

只降码率已提高绘制率。同为 750 kbps 时，缩小缓冲把 IDR 中位数从 20.4 KiB 降到 6.8 KiB，IDR P95 从 21.6 KiB 降到 7.4 KiB。小关键帧档在两档链路都降低了帧龄和绘制间隔 P95。540p、360p 在 1 Mbps 的单轮里有较短的间隔，但在 1.6 Mbps 中的绘制率都低于保留 720p 的小关键帧档，也没有通过小字辨识。产品因此只采用同尺寸的小关键帧档。

无压缩原图的同区域校准是 36/36 条短语完全匹配。该密集滚动负载下，所有编码档的 Canvas 样本均无完整短语匹配。帧率上升可改善拖动时找画面的速度，14 px 的滚动小字仍需单独解决。输入探针一批一序号，部分事件在等待发送时被现有新鲜度保护清空，表内均计入入队。小关键帧档的完成回执 P95 每轮为 1.60–1.75 秒，样本每轮 3–4 条。

## 运行中降档与恢复

候选版本为 `cfd5a8c`。在从打开会话算起的第 6.29 秒，中继从高档切到低档，第 6.73 秒首次绘出了低档画面。第 10.92 秒把下行从 1 Mbps 提升到 12 Mbps，保持约 600 ms RTT。第 18.48 秒中继恢复高档，第 18.87 秒首绘高档。两次切换后的发送首帧分别是序号 93 和 337，均为 IDR。全程 8 条已发送输入均得到唯一合成 worker 和 Viewer 回执，另有 6 条在提交前被清空。恢复原档之后曾出现一次 2.59 秒的绘制间隔。

恒定 12 Mbps 对照始终选择高档，绘制率仍为 2.26 fps，帧间隔 P95 为 1.68 秒。有界下行响应在高 RTT 下的续订间隔仍限制连续绘制。以上档位切换用合成源代替 Windows MFT 输出，实际原生编码、小字识读和办公室链路没有实测。

## 产品接线

Viewer 只在双方协商有界 AVC 和编码档位后发送档位。中继只接受当前授权 Viewer 在媒体续订里请求的两个整数档位，且只有主动声明支持的发送端会收到档位。现有进程租约、输入序号、回执和几何检查照常执行。无协商的旧端沿原路径运行。

Viewer 从至少 8 KiB 的媒体样本估计交付速率，扣除到响应头的往返和中继生产间隔。连续三批估计至多 2.2 Mbps 且交付耗时明显时降档。连续五批估计达到 4 Mbps，且低档已维持至少 12 秒时恢复。恢复后至少维持 8 秒再降档。空批及很小的批次没有足够证据，极静止的画面可能要等后续内容变化才恢复。

Windows 高档仍使用 1280×720 上限、20 fps、3.5 Mbps。低档保持尺寸和帧率，设置 750 kbps CBR 与 12,500 字节 HRD 缓冲。变化时在同一原生助手内换编码器，从新 IDR 继续递增帧序，保留物理桌面几何与租约。配置低档失败时仍用当前编码器。HRD 的字节值来自等量设置换算，实际 Windows IDR 字节和质量受 MFT 实现影响。[Microsoft H.264 编码器文档](https://learn.microsoft.com/en-us/windows/win32/medfound/h-264-video-encoder)

## 复跑

[压缩原始数据](../tests/video_stream/avc_encoding_results.json.gz) 包含 14 轮固定档位、2 轮运行中档位测试、逐帧绘制与帧龄、OCR 输出与真值、回执和续订、负载、链路探针与源代码哈希。脚本自带合成 mailbox、WKWebView 和本地代理，无需相邻工作树或公网服务。

```sh
export PYTHONDONTWRITEBYTECODE=1
run_study() {
  nice -n 15 uv run --offline --no-project --python 3.12 \
    --with-requirements tests/video_stream/requirements.txt python tests/video_stream/downlink_tls.py "$@"
}

run_study --seconds 13 --repeats 1 --rates 1 1.6 --modes slow \
  --profiles reference bitrate small-idr 540p 360p --output /tmp/lanpilot-quality-matrix.json
run_study --adaptive --candidate-revision cfd5a8c --seconds 27 --repeats 1 --rates 1 12 \
  --recover-after 8 --recover-mbps 12 --modes slow --output /tmp/lanpilot-quality-adaptive.json
run_study --seconds 13 --repeats 1 --rates 1.6 1 --modes slow \
  --profiles small-idr reference --output /tmp/lanpilot-quality-repeat.json
```

集成工作树在 Python 3.12 上通过 210 项测试，另通过 3 项 WKWebView JavaScript 回归、25 项 Windows PowerShell 工作进程模拟、C# 编译、x64 与 ARM64 原生交叉编译和 C++ 协议 sanitizer 检查。7 项需运行 Windows 的原生测试在 macOS 跳过。未部署。
