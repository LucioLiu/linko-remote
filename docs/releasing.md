# 统一预览版构建与发布

公开产物只含程序、运行环境、第三方许可和公开中继 URL / TLS 指纹。运行配置、设备身份、配对和截图均在安装后的用户数据目录生成，不能复制进发布目录。

## 构建

1. 准备仅含 `url`、`pin` 两个字段的 `peer-endpoint.json`，以及校验过的官方 CPython Windows 嵌入式运行时。
2. 使用全新的输出目录运行 `python3 -m lanpilot.build_unified windows OUTPUT --endpoint ENDPOINT --runtime PYTHON_ZIP --runtime-sha256 SHA256`。
3. macOS 使用 PyInstaller 冻结 `linko_desktop.py`。先用 `swiftc -target "$(uname -m)-apple-macosx14.0" -parse-as-library lanpilot/mac/PeerDesktop.swift -o PATH/LinkoDesktop -framework Cocoa -framework ScreenCaptureKit -framework ApplicationServices` 编译辅助程序。冻结时加入共享 `ui`、该辅助程序到 `native/LinkoDesktop`，以及公开 `peer-endpoint.json`。在冻结输出的 `ThirdParty` 中加入 CPython 和 PyInstaller 的许可文本。
4. 运行 `python3 -m lanpilot.build_unified macos OUTPUT_APP --endpoint ENDPOINT --engine FROZEN_ENGINE`。构建脚本生成原生外壳、签名应用，并只复制 `CLIENT_FILES` 白名单中的源码。
5. 用 Python `zipfile` 归档，保证文件名按 UTF-8 编码；为下载包生成 `SHA256SUMS.txt`。

不要从已安装应用或用户数据目录直接打包。重用输出目录时，代码目录出现白名单以外的文件会中止构建。

## 发布前检查

- 运行完整 Python 测试，并解析 Windows PowerShell 脚本。
- 用 `vtool -show-build` 核查 macOS 外壳、资源目录及冻结引擎中的两个 `LinkoDesktop` 副本，并核对系统最低版本与 `Info.plist` 一致。
- 使用新的临时用户数据目录启动产物，确认随机身份、通用名称、空设备列表、空 SSH 列表和默认关闭的接收开关。
- 验证同一中继中的已有设备和配对不会出现在新身份的列表中。
- 使用 `python3 -m lanpilot.release_audit ARTIFACTS --deny-file PRIVATE_LIST --report REPORT` 扫描最终归档和嵌套 ZIP。`PRIVATE_LIST` 是仅保存在本地的 JSON 字符串数组，用于排除开发设备名称、设备号和本地路径，不能上传。
- 在有 PyInstaller 的构建 Python 下，加 `--frozen ENGINE_BINARY` 检查压缩的冻结模块、代码文件名和字符串常量，避免只扫可执行文件表面字符串。
- 检查归档中没有用户配置、数据库、捕获画面、凭据或日志。公开中继配置只能包含 URL 和证书指纹。
- 上传前保持 Release 为草稿，上传后下载并核对实际字节的 SHA-256，再发布为预览版本。

历史提交的作者信息仍由 GitHub 展示。本流程清理产品内容和分发产物，不重写公开 Git 历史。


## 更新发布约定

版本只在 `lanpilot/version.py` 修改，支持 `major.minor.patch` 和 `-preview.N`、`-beta.N`、`-rc.N`。公开 Release 标签为 `v` 加版本号。Windows 资产名为 `LinkoRemote-Windows-VERSION.zip`，Mac Apple Silicon 为 `LinkoRemote-macOS-arm64-VERSION.zip`，GitHub 资产元数据须有 `sha256:` digest。两个平台产物均上传完成后再发布，避免客户端看见不完整版本。

更新器读取 Releases 集合，预览客户端可检测后续预览版；正式版客户端跳过预览版本。更旧或相同版本不会提示安装。不要修改已发布版本的资产字节，应提高版本号重新发布。
