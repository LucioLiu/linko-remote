# All filenames are manifest-derived basenames below the current user's protected
# job directories. This handler never runs a caller-supplied command.
function Test-JobFields($Object,[string[]]$Required,[string[]]$Optional=@()) {
 if($null -eq $Object -or $Object -isnot [pscustomobject]){throw 'JOB_INVALID_OBJECT'}
 $names=@($Object.PSObject.Properties.Name)
 foreach($key in $Required){if($names -cnotcontains $key){throw 'JOB_MISSING_FIELD'}}
 foreach($key in $names){if(($Required+$Optional) -cnotcontains $key){throw 'JOB_UNKNOWN_FIELD'}}
}
function Test-JobName([string]$Name,[bool]$Csv=$false) {
 $pattern=if($Csv){'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}\.csv\z'}else{'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}\z'}
 if($Name -cnotmatch $pattern -or $Name.Split('.')[0] -match '^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$'){throw 'JOB_INVALID_NAME'}
}
function Test-PlainJobPath([string]$Path) {
 if(([IO.File]::GetAttributes($Path) -band [IO.FileAttributes]::ReparsePoint) -ne 0){throw 'JOB_REPARSE_PATH'}
}
function New-JobFolder([string]$Path) {
 [void][IO.Directory]::CreateDirectory($Path)
 Test-PlainJobPath $Path
}
function Read-JobBytes([string]$Directory,[string]$Name,[int]$Limit) {
 Test-JobName $Name $true
 Test-PlainJobPath $Directory
 $path=[IO.Path]::Combine($Directory,$Name)
 Test-PlainJobPath $path
 $stream=[IO.File]::Open($path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
 try{
  if($stream.Length -gt $Limit){throw 'JOB_FILE_TOO_LARGE'}
  $bytes=New-Object byte[] ([int]$stream.Length)
  $pos=0
  while($pos -lt $bytes.Length){
   $count=$stream.Read($bytes,$pos,$bytes.Length-$pos)
   if($count -eq 0){throw 'JOB_FILE_CHANGED'}
   $pos+=$count
  }
  return ,$bytes
 }finally{$stream.Dispose()}
}
function Test-InputDigest([byte[]]$Bytes,[string]$Expected) {
 if($Expected -cnotmatch '^[0-9a-f]{64}\z' -or [LedgerReconcile]::Sha($Bytes) -cne $Expected){throw 'JOB_DIGEST_MISMATCH'}
}
function Get-JobArtifact([string]$Directory,[string]$Hash) {
 [byte[]]$bytes=Read-JobBytes $Directory 'result.csv' 2097152
 Test-InputDigest $bytes $Hash
 return @{name='result.csv';sha256=$Hash;bytes=$bytes.Length;content_base64=[Convert]::ToBase64String($bytes)}
}
function Publish-JobReceipt([string]$Inbox,[string]$Scope,$Receipt) {
 Test-JobName $Scope
 $folder=[IO.Path]::Combine($Inbox,$Scope)
 New-JobFolder $folder
 $target=[IO.Path]::Combine($folder,'receipt.json')
 $tmp=$target+'.'+[guid]::NewGuid().ToString('N')+'.tmp'
 try{
  [IO.File]::WriteAllText($tmp,($Receipt|ConvertTo-Json -Depth 4 -Compress),(New-Object Text.UTF8Encoding($false)))
  if([IO.File]::Exists($target)){Test-PlainJobPath $target;[IO.File]::Replace($tmp,$target,[System.Management.Automation.Language.NullString]::Value)}
  else{[IO.File]::Move($tmp,$target)}
 }finally{if([IO.File]::Exists($tmp)){[IO.File]::Delete($tmp)}}
}
function Invoke-LocalJob([string]$Root,[string]$RequestId,$Manifest) {
 if($RequestId -cnotmatch '^[a-f0-9]{32}\z'){throw 'JOB_INVALID_REQUEST'}
 if($Manifest.version -isnot [ValueType] -or $Manifest.version -is [bool] -or $Manifest.version -ne 1){throw 'JOB_VERSION'}
 Test-JobName $Manifest.input_scope
 $jobs=[IO.Path]::Combine($Root,'jobs');$inbox=[IO.Path]::Combine($Root,'job-inbox')
 Test-PlainJobPath $Root
 New-JobFolder $jobs;New-JobFolder $inbox
 $scope=[IO.Path]::Combine($inbox,$Manifest.input_scope)
 $work=[IO.Path]::Combine($jobs,$RequestId)
 if([IO.Directory]::Exists($work)){throw 'JOB_ALREADY_STARTED_RECOVER_ORIGINAL_ID'}
 New-JobFolder $work
 $marker=[IO.File]::Open([IO.Path]::Combine($work,'started'),[IO.FileMode]::CreateNew);$marker.Dispose()
 $checks=@();$totalBytes=0;$clock=[Diagnostics.Stopwatch]::StartNew()
 $base=@('version','operation','input_scope')
 switch -CaseSensitive ($Manifest.operation){
  'stage-inputs' {
   Test-JobFields $Manifest ($base+@('files'))
   if($Manifest.files -isnot [Array] -or $Manifest.files.Length -lt 1 -or $Manifest.files.Length -gt 48){throw 'JOB_FILE_COUNT'}
   $staged=[IO.Path]::Combine($work,'staging');New-JobFolder $staged
   $names=New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
   $utf8=New-Object Text.UTF8Encoding($false,$true)
   foreach($file in $Manifest.files){
    Test-JobFields $file @('name','sha256','content_base64')
    Test-JobName $file.name $true
    if(-not $names.Add($file.name) -or $file.content_base64 -isnot [string] -or $file.content_base64.Length -gt 699052){throw 'JOB_STAGE_INPUT'}
    [byte[]]$bytes=[Convert]::FromBase64String($file.content_base64)
    $totalBytes+=$bytes.Length
    if($bytes.Length -gt 524288 -or $totalBytes -gt 2097152){throw 'JOB_INPUT_BUDGET'}
    Test-InputDigest $bytes $file.sha256
    [void]$utf8.GetString($bytes)
    [IO.File]::WriteAllBytes([IO.Path]::Combine($staged,$file.name),$bytes)
    Test-InputDigest (Read-JobBytes $staged $file.name 524288) $file.sha256
    $checks+=@{name=$file.name;sha256=$file.sha256;bytes=$bytes.Length}
   }
   if([IO.Directory]::Exists($scope)){
    Test-PlainJobPath $scope
    if([IO.Directory]::GetFileSystemEntries($scope).Length -ne $checks.Count){throw 'JOB_STAGE_SCOPE_CONFLICT'}
    foreach($f in $checks){Test-InputDigest (Read-JobBytes $scope $f.name 524288) $f.sha256}
    [IO.Directory]::Delete($staged,$true)
   }else{[IO.Directory]::Move($staged,$scope)}
   return @{operation=$Manifest.operation;input_scope=$Manifest.input_scope;files=$checks;elapsed_ms=$clock.ElapsedMilliseconds}
  }
  'reconcile-ledgers' {
   Test-JobFields $Manifest ($base+@('batches')) @('receipt_scope','execution')
   if($Manifest.PSObject.Properties['execution'] -and $Manifest.execution -cnotin @('foreground','background')){throw 'JOB_EXECUTION_MODE'}
   if($Manifest.batches -isnot [Array] -or $Manifest.batches.Length -lt 1 -or $Manifest.batches.Length -gt 24){throw 'JOB_BATCH_COUNT'}
   if($Manifest.PSObject.Properties['receipt_scope']){
    Test-JobName $Manifest.receipt_scope
    if($Manifest.receipt_scope -eq $Manifest.input_scope){throw 'JOB_SCOPE_COLLISION'}
   }
   $snapshot=[IO.Path]::Combine($work,'inputs');New-JobFolder $snapshot
   $batches=New-Object 'System.Collections.Generic.List[LedgerBatch]'
   $ids=New-Object 'System.Collections.Generic.HashSet[string]'
   foreach($spec in $Manifest.batches){
    Test-JobFields $spec @('batch','expected_sha256','actual_sha256')
    if(-not [LedgerReconcile]::ValidBatch($spec.batch) -or -not $ids.Add($spec.batch)){throw 'JOB_INVALID_BATCH'}
    foreach($side in @('expected','actual')){
     $name=$side+'-'+$spec.batch+'.csv';$digest=$spec.($side+'_sha256')
     [byte[]]$bytes=Read-JobBytes $scope $name 524288
     $totalBytes+=$bytes.Length
     if($totalBytes -gt 2097152){throw 'JOB_INPUT_BUDGET'}
     Test-InputDigest $bytes $digest
     [IO.File]::WriteAllBytes([IO.Path]::Combine($snapshot,$name),$bytes)
     $checks+=@{name=$name;sha256=$digest;bytes=$bytes.Length}
    }
    if($clock.Elapsed.TotalSeconds -gt 30){throw 'JOB_TIME_BUDGET'}
    $batches.Add([LedgerReconcile]::ProcessBatch($snapshot,$spec.batch))
   }
   $report=[LedgerReconcile]::Export($work,$batches.ToArray())|ConvertFrom-Json
   $artifact=Get-JobArtifact $work $report.csv_sha256
   if($Manifest.receipt_scope){
    Publish-JobReceipt $inbox $Manifest.receipt_scope @{request_id=$RequestId;csv_sha256=$report.csv_sha256;csv_bytes=$report.csv_bytes;discrepancy_count=$report.discrepancy_count;total_delta_cents=$report.total_delta_cents}
   }
   return @{operation=$Manifest.operation;input_scope=$Manifest.input_scope;inputs=$checks;report=$report;artifact=$artifact;elapsed_ms=$clock.ElapsedMilliseconds}
  }
  'read-artifact' {
   Test-JobFields $Manifest ($base+@('file','sha256'))
   if($Manifest.file -cne 'result.csv'){throw 'JOB_INVALID_ARTIFACT'}
   return @{operation=$Manifest.operation;input_scope=$Manifest.input_scope;artifact=(Get-JobArtifact $scope $Manifest.sha256);elapsed_ms=$clock.ElapsedMilliseconds}
  }
  default{throw 'JOB_UNSUPPORTED_OPERATION'}
 }
}

function Initialize-JobEngine {
 $loading=[string]::Intern('LanPilot.JobEngine.Loader')
 [Threading.Monitor]::Enter($loading)
 try{
 if('LedgerReconcile' -as [type]){return}
 if($PSVersionTable.PSEdition -eq 'Core'){
  $refs=@((Get-ChildItem (Join-Path $PSHOME 'ref/*.dll')).FullName)
 }else{
  Add-Type -AssemblyName Microsoft.VisualBasic,System.Runtime.Serialization,System.Xml
  $refs=@([Microsoft.VisualBasic.FileIO.TextFieldParser].Assembly.Location,[Runtime.Serialization.DataContractAttribute].Assembly.Location,[Xml.XmlDocument].Assembly.Location,'System.Core.dll')
 }
 Add-Type -ReferencedAssemblies $refs -Path (Join-Path $PSScriptRoot 'LedgerReconcile.cs')
 }finally{[Threading.Monitor]::Exit($loading)}
}
function Write-JobJson([string]$Path,$Object) {
 $tmp=$Path+'.'+[guid]::NewGuid().ToString('N')+'.tmp'
 try{
  [IO.File]::WriteAllText($tmp,($Object|ConvertTo-Json -Depth 10 -Compress),(New-Object Text.UTF8Encoding($false)))
  if([IO.File]::Exists($Path)){Test-PlainJobPath $Path;[IO.File]::Replace($tmp,$Path,[System.Management.Automation.Language.NullString]::Value)}
  else{[IO.File]::Move($tmp,$Path)}
 }finally{if([IO.File]::Exists($tmp)){[IO.File]::Delete($tmp)}}
}
function Close-LocalJobHandle([switch]$Wait) {
 $h=$script:localJobHandle
 if(-not $h -or (-not $Wait -and -not $h.Invocation.IsCompleted)){return}
 try{[void]$h.Pipeline.EndInvoke($h.Invocation)}finally{
  $h.Pipeline.Dispose();$h.Space.Dispose();$script:localJobHandle=$null
 }
}
function Get-BackgroundJob([string]$Queue,[string]$Id,[bool]$IncludeResult=$false) {
 if($Id -cnotmatch '^[a-f0-9]{32}\z'){throw 'JOB_INVALID_ID'}
 Test-PlainJobPath $Queue
 $folder=Join-Path $Queue $Id;Test-PlainJobPath $folder
 Close-LocalJobHandle
 $done=Join-Path $folder 'finished.json';$running=Join-Path $folder 'running.lock'
 $data=@{job_id=$Id;job_state='starting'}
 if(Test-Path -LiteralPath $done){
  Test-PlainJobPath $done
  $entry=[IO.File]::ReadAllText($done)|ConvertFrom-Json
  if($entry.ok){
   $data.job_state='succeeded'
   $data.report=$entry.data.report
   if($IncludeResult){$data.artifact=$entry.data.artifact}
  }else{$data.job_state='failed';$data.error=$entry.error}
  return $data
 }
 if([IO.File]::Exists($running)){
  try{$probe=[IO.File]::Open($running,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::None);$probe.Dispose();$data.job_state='unconfirmed'}
  catch [IO.IOException]{$data.job_state='running'}
 }elseif(([DateTime]::UtcNow-[IO.Directory]::GetCreationTimeUtc($folder)).TotalSeconds -gt 15){$data.job_state='unconfirmed'}
 return $data
}
function Start-BackgroundJob([string]$Root,[string]$Id,$Manifest) {
 if($Id -cnotmatch '^[a-f0-9]{32}\z' -or $Manifest.operation -cne 'reconcile-ledgers'){throw 'JOB_INVALID_START'}
 Test-PlainJobPath $Root
 $queue=Join-Path $Root 'job-queue';New-JobFolder $queue
 $active=Join-Path $queue 'active.json'
 if([IO.File]::Exists($active)){
  Test-PlainJobPath $active
  $last=[IO.File]::ReadAllText($active)|ConvertFrom-Json
  $previous=Get-BackgroundJob $queue $last.job_id
  if($previous.job_state -in @('starting','running')){throw 'JOB_BACKGROUND_SLOT_BUSY'}
 }
 $folder=Join-Path $queue $Id
 if([IO.Directory]::Exists($folder)){throw 'JOB_ALREADY_STARTED_RECOVER_ORIGINAL_ID'}
 New-JobFolder $folder
 Write-JobJson (Join-Path $folder 'manifest.json') $Manifest
 Write-JobJson $active @{job_id=$Id}
 $hostFile=Join-Path $PSScriptRoot 'LocalJobHost.ps1'
 Close-LocalJobHandle
 $space=[RunspaceFactory]::CreateRunspace()
 $pipeline=[PowerShell]::Create()
 try{
  $space.ApartmentState='MTA';$space.ThreadOptions='ReuseThread';$space.Open()
  $pipeline.Runspace=$space
  [void]$pipeline.AddCommand($hostFile).AddParameter('RequestId',$Id).AddParameter('Workspace',$Root)
  $invocation=$pipeline.BeginInvoke()
  $script:localJobHandle=@{Space=$space;Pipeline=$pipeline;Invocation=$invocation}
 }catch{
  $pipeline.Dispose();$space.Dispose()
  Write-JobJson (Join-Path $folder 'finished.json') @{ok=$false;error='JOB_START_FAILED'}
  throw
 }
 return @{job_id=$Id;job_state='started'}
}
function Invoke-JobRequest([string]$Root,[string]$Id,$Manifest) {
 if($Manifest.operation -cin @('job-status','job-result')){
  Test-JobFields $Manifest @('version','operation','job_id')
  if($Manifest.version -ne 1){throw 'JOB_VERSION'}
  return Get-BackgroundJob (Join-Path $Root 'job-queue') $Manifest.job_id ($Manifest.operation -ceq 'job-result')
 }
 if($Manifest.execution -ceq 'background'){return Start-BackgroundJob $Root $Id $Manifest}
 Initialize-JobEngine
 return Invoke-LocalJob $Root $Id $Manifest
}
