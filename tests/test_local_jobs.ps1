param([string]$repo=(Split-Path $PSScriptRoot -Parent))
$ErrorActionPreference='Stop'
. "$repo/windows/LocalJob.ps1"
Initialize-JobEngine
$env:LOCALAPPDATA=Join-Path ([IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString('N'));$tmp=Join-Path $env:LOCALAPPDATA 'CodexRemoteDesktop';[void][IO.Directory]::CreateDirectory($tmp)
try{
 $hashes=@{};$files=@()
 foreach($v in @('expected','actual')){
  $name="$v-01.csv"
  $rows=if($v -eq 'expected'){"id,amount`r`nA,12.30`r`nA,0.20`r`nZ,1.00`r`n"}else{"id,amount`r`nA,11.50`r`nY,-2.00`r`n"}
  $b=[Text.Encoding]::UTF8.GetBytes($rows);$hashes[$v]=[LedgerReconcile]::Sha($b)
  $files+=@{name=$name;content_base64=[Convert]::ToBase64String($b);sha256=$hashes[$v]}
 }
 function wire($o){return ($o|ConvertTo-Json -Depth 8 -Compress|ConvertFrom-Json)}
 $stage=wire @{version=1;operation='stage-inputs';input_scope='example';files=$files}
 $staged=Invoke-LocalJob $tmp ('a'*32) $stage
 if($staged.files.count -ne 2){throw 'stage'}
 $same=Invoke-LocalJob $tmp ('b'*32) $stage
 $spec=wire @{version=1;operation='reconcile-ledgers';input_scope='example';receipt_scope='example-b';batches=@(@{batch='01';expected_sha256=$hashes.expected;actual_sha256=$hashes.actual})}
 $result=Invoke-LocalJob $tmp ('c'*32) $spec
 if($result.report.discrepancy_count -ne 3 -or $result.report.total_delta_cents -ne -400){throw ($result|ConvertTo-Json -Depth 8)}
 $read=wire @{version=1;operation='read-artifact';input_scope='example';file='result.csv';sha256='0'*64}
 try{[void](Invoke-LocalJob $tmp ('d'*32) $read);throw 'unexpected success'}catch{if($_.Exception.Message -eq 'unexpected success'){throw}}
 if(!(Test-Path "$tmp/job-inbox/example-b/receipt.json")){throw 'receipt'}
 $spec|Add-Member -NotePropertyName execution -NotePropertyValue background
 $start=Invoke-JobRequest $tmp ('e'*32) $spec
 if($start.job_state -ne 'started'){throw 'async launch'}
 $status=wire @{version=1;operation='job-status';job_id='e'*32}
 $terminal=$null
 for($i=0;$i -lt 50;$i++){
  $terminal=Invoke-JobRequest $tmp ('f'*32) $status
  if($terminal.job_state -in @('succeeded','failed','unconfirmed')){break}
  Start-Sleep -Milliseconds 150
 }
 if($terminal.job_state -ne 'succeeded'){throw ($terminal|ConvertTo-Json)}
 if($terminal.ContainsKey('artifact')){throw 'status should omit artifact'}
 $status.operation='job-result'
 $saved=Invoke-JobRequest $tmp ('f'*32) $status
 if($saved.artifact.sha256 -ne $result.artifact.sha256){throw 'async output mismatch'}
 'PASS asynchronous job finished same artifact without a second execution'
 'PASS native stage/idempotent retry/6-field report/readback/cross-scope receipt'
 # No user-controlled name may escape the dedicated inbox.
 $stage.input_scope='../escape'
 try{[void](Invoke-JobRequest $tmp ('1'*32) $stage);throw 'accepted traversal'}catch{if($_.Exception.Message -eq 'accepted traversal'){throw}}
 $stage.input_scope='example';$stage.files[0].sha256='f'*64
 try{[void](Invoke-JobRequest $tmp ('2'*32) $stage);throw 'accepted digest mismatch'}catch{if($_.Exception.Message -eq 'accepted digest mismatch'){throw}}
}finally{[IO.Directory]::Delete($env:LOCALAPPDATA,$true)}
