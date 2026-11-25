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
