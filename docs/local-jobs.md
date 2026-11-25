# Windows 本地文件作业

在 Mac 使用邻控 CLI 或 MCP 可将明确选定的 UTF-8 CSV 暂存到指定 Windows，再进行对账。Windows 登录账户需要已配对且已启用“允许 AI 键鼠和限定文件作业”。屏幕查看和点击继续使用 observe、act。

```sh
lanpilot stage DEVICE november-01 \
  --input expected-01.csv "/path/to/工资核对.csv" \
  --input actual-01.csv "/path/to/平台导出.csv"
lanpilot job DEVICE manifest.json
lanpilot recover DEVICE REQUEST_ID
```

`stage_inputs` MCP 接受 `device`、`input_scope` 和 `files`。每项可以是 Mac 文件路径，也可以是 `{"path":"/path/本月.csv","name":"expected-01.csv"}`。每次 1–48 个文件，总计最多 2 MiB，每个最高 512 KiB。暂存先校验每个文件的 SHA-256，再一次性发布整组文件；同名范围已有不同内容时请选择新范围。已发布输入保存在当前 Windows 账户的 `%LOCALAPPDATA%\CodexRemoteDesktop\job-inbox\<input_scope>`。

在 `run_job` 的 `manifest` 填入以下内容。SHA-256 取自暂存回执。一次可指定 1–24 组，各组使用 `expected-XX.csv` 与 `actual-XX.csv`，表头是 `id,amount`。同一 ID 的金额先按分相加，CSV 包含双方金额不同和单边缺失的行，以批次、ID 排序；差额为实际减预期。

```json
{"version":1,"operation":"reconcile-ledgers","input_scope":"november-01","batches":[
  {"batch":"01","expected_sha256":"<expected-01.csv 的 SHA-256>","actual_sha256":"<actual-01.csv 的 SHA-256>"}
]}
```

结果含各批双方行数、差异数、带正负号的分值合计和 `result.csv` 的 SHA-256。明细列为 `batch,id,expected_cents,actual_cents,status`。缺失一侧的金额字段留空；合计差额时按零处理。Windows 写入并重新读取 `jobs\<request_id>\result.csv`，Mac 再次校验并保存 CSV。通过 `result.artifact.local_path` 打开实际返回文件。指定额外的 `receipt_scope` 会在对应范围写入 `receipt.json` 供本机界面核对。要核验另外一个已知文件，可提交 `{"version":1,"operation":"read-artifact","input_scope":"另一范围","file":"result.csv","sha256":"<已知 SHA-256>"}`；它只接受该范围内这个文件，最大 2 MiB。

短作业默认为前台执行，这段时间该设备的屏幕请求要等作业返回。需要继续看屏时，在对账清单加 `"execution":"background"`。启动回执返回 `job_id`；用 `{"version":1,"operation":"job-status","job_id":"..."}` 查状态，用同样字段和 `"operation":"job-result"` 取完成结果。每台设备最多一个后台作业线程，仅执行指定清单，持续由请求 ID 标识。宿主进程异常退出而未写回结果会显示未确认；重新运行原任务会产生新的操作。转输超时时，通过 `recover` 查询原请求，后台作业则用原 `job_id` 继续查询。回执包含开始、运行、完成或失败状态，没有逐行进度。每个输入最多 2 万行；对账按批次检查 30 秒时间预算。输出、输入快照和异常回执保留在该 Windows 用户的专用目录，直到用户清理。

要增加本地 CLI 处理器，给 `lanpilot/jobs.py` 增加明确的操作名、字段、权限和输入上限，在 `windows/LocalJob.ps1` 注册固定处理函数或固定的已部署程序；参数来自经过校验的清单，工作目录由请求 ID 创建。新增处理器要验证输出内容和摘要，并通过安装包清单分发对应代码。涉及业务平台的结果应另外读回平台记录，必要时通过 `observe` 查看真实画面。现有处理器支持 CSV 暂存、对账及结果读取。

重新连接后可以直接使用启动回执里的 `job_id`：

```sh
lanpilot job-status DEVICE JOB_ID
lanpilot job-result DEVICE JOB_ID
```

第一条查询原作业进度，第二条在作业成功后取回并校验差异文件。两条都可重复查询。若启动回执丢失，先用 `lanpilot recover DEVICE REQUEST_ID` 取回原启动回执。

### 在 Mac 面板查询

0.7 Mac 面板选中执行对账的 Windows 设备，点击「查询后台作业…」，输入启动回执中的 `job_id`。面板记住每台设备最近查询的编号。完成时显示差异数和校验后 CSV 的本机路径，执行失败时显示远端错误。查询回执丢失时可按原编号再次查询。

0.8 统一版开发分支支持双向传文件、SSH 快捷连接和截图。统一版中继的远程操作入口目前未接入上述 Windows 后台对账协议；此面板入口属于 0.7。

单组对账可运行 `lanpilot reconcile DEVICE 预期.csv 实际.csv`。两份 UTF-8 文件使用 `id,amount` 表头。命令依次暂存两份文件、核对远端回执并启动一次后台对账。启动后用返回的 `result.job_id` 查询或取件。若返回 `unconfirmed`，按回执的 `request_id` 查询原请求；`step` 表明待确认的是暂存还是启动。暂存回执未确认时不会启动对账。
