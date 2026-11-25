# 连续 JPEG 差分原型

发送端按 128×128 像素检查原始 RGB 的变化，将同一行相邻的变动块合并成一条 JPEG。接收端把这些条带叠到上一张已解码的画面。比较器对每个像素做精确比较，跳过未变化的块时不会把小字的变化量化掉。首帧、尺寸变化、主动恢复、定期刷新和变动块超过 60% 时发整张 JPEG。重复画面只发送 13 字节的帧头。

包头含序号、依赖的上一帧序号和画面尺寸。差分要求上一个序号已经应用。漏帧后接收端保留最后一张完整画面并返回 `MissingBase`，调用方可用 `force_full=True` 重建基准。验证条带边界并完成解码后才更新接收状态。基准在内存中传递二进制帧，自动化测试另用本地 socket 传过整帧和连续差分，覆盖像素一致性和丢帧恢复。接入只保留最新帧的中继时，需要跟踪每个接收端的基准序号，并为新加入或落后的接收端发整帧。

## 复现

用 Python 3.12 在独立虚拟环境安装本目录的依赖。PyAV wheel 自带 FFmpeg 库，脚本直接使用其软件 MJPEG 编解码。系统无需安装 FFmpeg CLI，也无需显卡、真实显示器、录屏权限或已配对设备。

```sh
python3.12 -m venv /tmp/lanpilot-video-bench
/tmp/lanpilot-video-bench/bin/python -m pip install -r tests/video_stream/requirements.txt
/tmp/lanpilot-video-bench/bin/python -m unittest discover -v -s tests/video_stream
/tmp/lanpilot-video-bench/bin/python tests/video_stream/benchmark.py
/tmp/lanpilot-video-bench/bin/python tests/video_stream/benchmark.py --width 1920 --height 1080 --fps 30
```

Windows x64 使用 PowerShell。

```powershell
$videoEnv = Join-Path $env:TEMP 'lanpilot-video-bench'
py -3.12 -m venv $videoEnv
& "$videoEnv\Scripts\python.exe" -m pip install -r tests/video_stream/requirements.txt
& "$videoEnv\Scripts\python.exe" -m unittest discover -v -s tests/video_stream
& "$videoEnv\Scripts\python.exe" tests/video_stream/benchmark.py
```

已检查固定版本在 Windows x64、Intel macOS、Linux x64 和 arm64（manylinux 2.28）有兼容的 Python 3.12 wheels。运行测试和基准的实测平台为 macOS 26.7、Apple M1 Pro。

## 测量口径

- 局部滚动和全屏滚动都以每帧 7 像素滚动办公小字，每 9 帧切换一次状态条底色。每场景各跑 3 轮、每轮 120 帧，轮换两条路径的测试顺序。两组采用同一张输入图、同一 FFmpeg MJPEG 4:4:4 软件编解码、单线程、量化参数 5。每帧以整帧 JPEG 解码结果为对照，RGB 有一处不同就报错。
- 字节数包含二进制帧头和条带头。表中码率按目标 20 或 30 fps 乘以实测平均帧体积计算。耗时只覆盖从 RGB 到包再到重建 RGB，包括检测、封包及贴回。RGB 生成、质量比较、操作系统采集、网络、JSON/Base64、排队与渲染均在计时之外。两条路径的尺寸和像素格式用于本实验等画质比较，与当前产品原生 JPEG 设置独立。
- 每 2 秒的整帧刷新计入测试。在全屏运动时所有帧都走整帧，带宽相同，候选只多出变化检测和保留原始帧的成本。高变化覆盖率场景若要减少带宽，需另测帧间视频编码。

## Apple M1 Pro 实测

共比较 1,440 帧，两条路径解码后的 RGB 逐帧逐像素相同。6 项自动化测试通过。

| 场景 | 目标帧率 | 平均 KiB/帧，整帧 → 差分 | 字节降幅 | 编解码 P50 / P95，整帧 → 差分 |
| --- | ---: | ---: | ---: | ---: |
| 720p 局部滚动 | 20 fps | 146.80 → 118.85 | 19.04% | 12.15 / 13.05 → 8.92 / 11.88 ms |
| 720p 全屏滚动 | 20 fps | 257.55 → 257.55 | 0% | 15.98 / 16.66 → 16.42 / 17.17 ms |
| 1080p 局部滚动 | 30 fps | 309.90 → 262.25 | 15.37% | 26.63 / 28.38 → 20.71 / 22.64 ms |
| 1080p 全屏滚动 | 30 fps | 579.46 → 579.46 | 0% | 35.83 / 37.82 → 36.82 / 38.82 ms |

原始输出见 [720p](m1pro-720p.json) 和 [1080p](m1pro-1080p.json)，包含发送、接收分项计时和整帧计数。1080p 全屏滚动的编解码 P50 已超过 30 fps 对应的 33.33 ms 帧周期。

### H.264 与实网下行

运行 `python h264_benchmark.py` 可复测等画质全屏文字滚动。固定 4:4:4 色度采样，按整帧 RGB PSNR 校准，同时报告文字边缘的独立指标。每帧输入都在当次编码并解码。

运行 `python wan_playback.py SSH主机别名` 可用自己管理的 SSH 服务器复测公网下行。合成测试帧预先放在远端临时目录，测试结束清除。客户端发出开始信号后，服务器按每秒 20 帧发送 80 帧，Mac 解码到 RGB 后计时，四轮采用 H.264、JPEG、JPEG、H.264 的顺序。首帧包含开始信号到第一帧解码的时间；批次尾部可能出现积压后突发到达，因此优先观察 4 秒内完成解码的帧数和全部 80 帧的交付时间。结果见 wan-playback-m1pro.jsonl。该测试测量服务器到 Mac 的路径，不包含 Windows 采集、公司上行或屏幕实际显示。

### Windows AVC 上传

`serial_avc_tls.py` 和 `serial_avc_tls_results.json` 保留逐帧等待 ACK 的原始吞吐基准。该基准在上一帧收到 ACK 后才释放下一帧。

```sh
/tmp/lanpilot-video-bench/bin/python tests/video_stream/batch_avc_tls.py \
  --seconds 15 --repeats 3 > batch_avc_tls_results.json
python3 -m unittest discover -s tests -p 'test_live_stream.py'
```

新对照使用同一份 720p、20fps AVC 片段和正常的 LiveAgent 上传及 TLS 中继。合成采集按固定时钟写入邮箱，两组分别使用旧的单文件和协商后的 8 槽邮箱。发送端到中继的 RTT 为 0、100 或 200ms，观看端直连本地中继。每轮在相同位置跳过两个采集序号，恢复时间从首个跳过的序号计到下一次有效到帧。片段每秒有一个 IDR。回归测试另验证提交未获 ACK、邮箱覆盖、输入回执及按需请求 IDR。到帧延迟测到观看端解析记录，Windows 实机采集和屏幕显示耗时需要另测。

## 有界 AVC 上传与点击场景

新 Windows 发送端和中继通过原控制交换协商上传流。TLS 握手与首轮控制交换并行，取得租约后才发送 HTTP 上传请求。已获得该会话发送权限的设备通过固定证书指纹 TLS 上传，每个 HTTP 块只含一个 AVC 帧。中继逐帧发布并重新核对授权、观看期限和发送端控制租约。原控制连接继续处理键鼠、回执和进度，上传本身不续租。

发送窗口最多 24 帧或 4 MiB 编码载荷，取帧时丢弃已等待 250 ms 的帧。丢帧后等待 IDR。进度卡住 1.25 秒、连接中断、协商不可用时，发送端关闭流、请求新 IDR 并回到原批传。中继在批传接手或控制端停止协商后封住旧流，旧中继照常协商批传或逐帧 AVC。现有 JPEG 回退仍适用。

Windows 原生队列仍为 8 帧或 4 MiB，编码器沿用单个采集位图和现有 Media Foundation 缓冲。磁盘及 Python 读取缓存最多各 8 个 mailbox，单槽最多一个约 2.67 MiB 的 Base64 记录。上传窗口只保留序号、计时和字节数，不另存帧。中继每会话 AVC 历史上限 64 帧或 4 MiB，每条连接按有界单帧大小读取，最多 12 条上传流并存。接收端沿用一个画布，WebCodecs 最多保留 8 个待解码帧的元数据，每个输出 VideoFrame 绘制后立即关闭。Mac 硬解互操作可用以下命令验证。实机显存峰值和长时内存曲线需在 Windows、Mac 实机采样。

复跑时用同一 Python 环境安装本目录的 `requirements.txt`，从候选工作树执行。

```sh
baseline_dir=$(mktemp -d)
git archive 0703f5e | tar -x -C "$baseline_dir"
cp tests/video_stream/batch_avc_tls.py tests/video_stream/input_avc_tls.py "$baseline_dir/tests/video_stream/"
python "$baseline_dir/tests/video_stream/batch_avc_tls.py" --rtts 100 200 --modes batch
python tests/video_stream/batch_avc_tls.py --rtts 100 200 --modes stream

python "$baseline_dir/tests/video_stream/batch_avc_tls.py" --input --repeats 1 --rtts 100 200 --modes batch
python tests/video_stream/batch_avc_tls.py --input --repeats 1 --rtts 100 200 --modes stream
python tests/video_stream/native_interop.py
```

- 传输对照使用原来的 720p 20 fps 滚动源、300 个预编码访问单元和 1 秒 IDR 间隔，两组都故意漏掉第 145、146 帧。每个 RTT 和模式各三轮，每轮 15 秒。延迟只加在 Windows 发送端与中继之间，查看端走本机回环 TLS。帧年龄从生成对应源帧到查看端收到该帧的回调，使用共用单调时钟。帧率分母为固定 15 秒窗口，另在排空后统计丢帧。
- 点击对照单独运行。测试用实际 `live-input`、`live-exchange` 和 Windows worker 文件信箱协议，把点击送到独立运行的合成桌面逻辑。合成桌面应用点击并写执行回执，随后把更新后的状态绘制进编码源帧的一组像素。以查看端发出点击前为起点，分别记录提交响应、桌面应用、查看端收到执行回执，以及收到 AVC 并解码出对应像素标记的时刻。最后一个边界止于解码结果，显示器绘制、刷新和物理键鼠需另行采样。丢帧恢复期间的点击也在样本内，逐条保留回执和像素结果。

| 发送端 RTT | 收帧率中位数 | 帧年龄 P95 的三轮中位数 | 点击到执行回执 P95 | 点击到解码对应像素 P95 |
| --- | ---: | ---: | ---: | ---: |
| 100 ms | 18.87 → 18.87 fps | 172 → 73 ms | 281 → 189 ms | 374 → 206 ms |
| 200 ms | 18.80 → 18.80 fps | 307 → 124 ms | 467 → 280 ms | 657 → 282 ms |

箭头从 `0703f5e` 实际代码重跑的批传指向候选上传流。原始滚动源与此前批传实验有相同的 AVC 载荷 SHA-256。收帧对照的十二轮均从第 161 帧恢复，每轮经排空确认缺失 16 帧，包含故意省略的两帧。点击对照在 100 ms RTT 匹配了批传 29 次、上传流 35 次，在 200 ms RTT 匹配了批传 20 次、上传流 28 次。每次均收到回执且解码得到对应像素。Mac 原生互操作使用 VideoToolbox 硬解，通过完整序列 65 帧及注入丢帧后的 44 帧像素哈希核对。

[逐轮结果与每次点击的计时](bounded_avc_tls_results.json) · [此前逐帧和批传的原始对照](batch_avc_tls_results.json)
