param([switch]$AllowInput, [string]$RelayConfig, [switch]$TargetGuard, [switch]$Persistent, [switch]$LanDiscovery, [switch]$WanOnly, [Threading.ManualResetEvent]$StopEvent, [ValidatePattern('^[a-f0-9]{32}$')][string]$Generation, [string]$Workspace, [string]$StopFile)
$ErrorActionPreference = 'Stop'
if($env:LANPILOT_ISOLATED -match '^[0-9a-f]{32}$' -and $env:LOCALAPPDATA -eq (Join-Path ([IO.Path]::GetTempPath()) ('LanPilot-Invite-QA-'+$env:LANPILOT_ISOLATED))){$AllowInput=$false}
$root = if($Workspace){[IO.Path]::GetFullPath($Workspace)}else{Join-Path $env:LOCALAPPDATA 'CodexRemoteDesktop'}
New-Item -ItemType Directory -Force -Path $root | Out-Null
# Protect mailbox contents (typed text and screenshots) from other local users.
$acl = [IO.Directory]::GetAccessControl($root,[Security.AccessControl.AccessControlSections]::Access)
$acl.SetAccessRuleProtection($true, $false)
foreach($existing in @($acl.Access)) { [void]$acl.RemoveAccessRuleSpecific($existing) }
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
foreach ($identity in @($sid, (New-Object System.Security.Principal.SecurityIdentifier('S-1-5-18')))) {
 $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($identity,'FullControl','ContainerInherit,ObjectInherit','None','Allow')
 $acl.AddAccessRule($rule)
}
[IO.Directory]::SetAccessControl($root,$acl)
$lock = [IO.File]::Open((Join-Path $root 'worker.lock'),'OpenOrCreate','ReadWrite','None')
try {
 Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes,WindowsBase,Accessibility
 if(-not ('RemoteDesktop' -as [type])){Add-Type -ReferencedAssemblies @('System.Drawing',[System.Windows.Automation.AutomationElement].Assembly.Location,[System.Windows.Automation.TextPatternIdentifiers].Assembly.Location,[System.Windows.Rect].Assembly.Location,[Accessibility.IAccessible].Assembly.Location) -Path (Join-Path $PSScriptRoot 'Desktop.cs')}
 if($RelayConfig){
  Add-Type -AssemblyName System.Security
  if(-not ('DesktopRelay' -as [type])){Add-Type -Path (Join-Path $PSScriptRoot 'Relay.cs')}
  if($LanDiscovery -and -not ('LanPilotDiscovery' -as [type])){Add-Type -ReferencedAssemblies System.Web.Extensions -Path (Join-Path $PSScriptRoot 'Discovery.cs')}
  [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12
  $rsa=New-Object Security.Cryptography.RSACryptoServiceProvider
  $rsa.PersistKeyInCsp=$false
  try {
   $blob=[Security.Cryptography.ProtectedData]::Unprotect([IO.File]::ReadAllBytes((Join-Path $root 'pair-key.dpapi')),$null,[Security.Cryptography.DataProtectionScope]::CurrentUser)
   $rsa.ImportCspBlob($blob)
   $relay=[Text.Encoding]::UTF8.GetString($rsa.Decrypt([IO.File]::ReadAllBytes($RelayConfig),$true)) | ConvertFrom-Json
  } finally {$rsa.Dispose()}
  if($relay.pin -notmatch '^[a-f0-9]{64}$'){throw 'INVALID_RELAY_CONFIG'}
  $pairedEndpoint=[DesktopRelay]::NormalizeEndpoint($relay.url)
  $routeFile=Join-Path $root 'relay-route.json'
  function Select-RelayEndpoint([string]$failed) {
   $route=$null;$local=$null
   if(Test-Path -LiteralPath $routeFile){
    try {
     $config=Get-Content -Raw -LiteralPath $routeFile -Encoding UTF8 | ConvertFrom-Json
     $route=[DesktopRelay]::NormalizeEndpoint($config.url)
    }catch{Write-Warning 'Ignoring invalid relay-route.json; using paired routes.'}
   }
   if($WanOnly){if(-not $route){throw 'WAN_TEST_ROUTE_REQUIRED'};return $route}
   if($LanDiscovery){
    try{$local=[LanPilotDiscovery]::Find($relay.token)}catch{Write-Verbose 'LAN discovery unavailable'}
   }
   return [DesktopRelay]::NextEndpoint($pairedEndpoint,$route,$local,$failed)
  }
  $relay.url=Select-RelayEndpoint ''
 }
 [RemoteDesktop]::Init()
 if($StopFile){[RemoteDesktop]::InputStopFile=$StopFile}
 if($Persistent){[void][RemoteDesktop]::KeepAwake($true)}
 [RemoteDesktop]::Check()
 $session = if($Generation){$Generation}else{[guid]::NewGuid().ToString('N')}
 function Test-WorkerStop {return ($StopEvent -and $StopEvent.WaitOne(0)) -or ($StopFile -and (Test-Path -LiteralPath $StopFile))}
 function Wait-Reconnect([int]$Seconds) {
  if($StopEvent){[void]$StopEvent.WaitOne($Seconds*1000)}else{Start-Sleep -Seconds $Seconds}
 }
 $frame = $null
 $seen = New-Object 'System.Collections.Generic.HashSet[string]'
 $pollFailures = 0
 $previousPostMs = $null
 $previousCleanupMs = $null
 $sha = [Security.Cryptography.SHA256]::Create()
 function Test-TransientRelayFailure($exception) {
  return [DesktopRelay]::IsTransient($exception)

 }
 # A fresh STA generation can rebuild a failed connection pool. Only a typed
 # network failure may request this; credential, policy and lock errors stop.
 function Request-TransportRestart([string]$Phase,[int]$Attempt) {
  if($Generation -and $Persistent -and $Attempt -ge 4){
   $path=Join-Path $root 'transport-restart.json'
   @{generation=$Generation;phase=$Phase;utc=[DateTime]::UtcNow.ToString('o')}|ConvertTo-Json -Compress|Set-Content -Encoding UTF8 ($path+'.tmp')
   Move-Item -Force ($path+'.tmp') $path
   throw 'TRANSIENT_TRANSPORT_RESTART'
  }
 }
 function Send-ResultReceipt([string]$body) {
  $attempt=0
  while($true){
   if(Test-WorkerStop){throw 'WORKER_STOPPED_WITH_DURABLE_RECEIPT'}
   try{[void][DesktopRelay]::Exchange(($relay.url+'/result'),$relay.token,$relay.pin,'POST',$body);return}
   catch{
    $transient=Test-TransientRelayFailure $_.Exception
    $attempt++
    if(-not $transient -or (-not $Persistent -and $attempt -ge 3)){throw 'RESULT_DELIVERY_UNCONFIRMED_WORKER_STOPPED'}
    Request-TransportRestart 'result' $attempt
    $relay.url=Select-RelayEndpoint $relay.url
    Wait-Reconnect ([Math]::Min(10,$attempt*2))
   }
  }
 }
 $outbox=Join-Path $root 'pending-receipt.json'
 if($RelayConfig -and (Test-Path -LiteralPath $outbox)){
  $pending=Get-Content -Raw -LiteralPath $outbox -Encoding UTF8|ConvertFrom-Json
  if($pending.pin -ne $relay.pin){throw 'PENDING_RECEIPT_IDENTITY_MISMATCH'}
  $pendingBody=$pending.body
  # Recover the initial 0.4 draft's PowerShell extended-string serialization.
  if($pendingBody -isnot [string] -and $pendingBody.value -is [string]){$pendingBody=$pendingBody.value}
  if($pendingBody -isnot [string]){throw 'PENDING_RECEIPT_INVALID_BODY'}
  $pendingValue=$pendingBody|ConvertFrom-Json
  if($pendingValue.request_id -notmatch '^[a-f0-9]{32}$'){throw 'PENDING_RECEIPT_INVALID_ID'}
  Send-ResultReceipt $pendingBody
  Remove-Item -LiteralPath $outbox
 }
 function Hash-Image($bytes) { [Convert]::ToBase64String($sha.ComputeHash($bytes)) }
 function Add-Screenshot($data,[byte[]]$png,[string]$format) {
  if($format -eq 'compact'){
   try{
    [byte[]]$transfer=[RemoteDesktop]::CompactScreenshot($png)
    if($transfer[0] -eq 255){$data.jpeg_base64=[Convert]::ToBase64String($transfer);return}
    $data.png_base64=[Convert]::ToBase64String($transfer);return
   }catch{
    # A codec failure must not lose the original screenshot or input receipt.
   }
  }
  $data.png_base64=[Convert]::ToBase64String($png)
 }
 function Observe([string]$format) {
   $before = [RemoteDesktop]::LastInput()
   $hwnd = [RemoteDesktop]::GetForegroundWindow().ToInt64()
   $focus = [RemoteDesktop]::FocusIdentity()
   $windowBounds = [RemoteDesktop]::ForegroundBounds()
   $automationFocus = [RemoteDesktop]::AutomationFocusIdentity()
   $editTarget = [RemoteDesktop]::FocusedEditTarget()
   $caret = [RemoteDesktop]::CaretBounds()
   $bytes = [RemoteDesktop]::Capture()
   $after = [RemoteDesktop]::LastInput()
   if ($before -ne $after -or $hwnd -ne [RemoteDesktop]::GetForegroundWindow().ToInt64()) {throw 'DESKTOP_CHANGED_DURING_CAPTURE'}
   if($focus -ne [RemoteDesktop]::FocusIdentity() -or ($windowBounds -join ',') -ne ([RemoteDesktop]::ForegroundBounds() -join ',')){throw 'FOCUS_OR_WINDOW_MOVED_DURING_CAPTURE'}
   if(($caret -join ',') -ne ([RemoteDesktop]::CaretBounds() -join ',')){throw 'CARET_MOVED_DURING_CAPTURE'}
   if($automationFocus -ne [RemoteDesktop]::AutomationFocusIdentity()){throw 'AUTOMATION_FOCUS_CHANGED_DURING_CAPTURE'}
   $editAfter=[RemoteDesktop]::FocusedEditTarget()
   if($editTarget.State -ne $editAfter.State){throw 'EDIT_TARGET_CHANGED_DURING_CAPTURE'}
   $script:frame = @{id=[guid]::NewGuid().ToString('N'); time=[Diagnostics.Stopwatch]::GetTimestamp(); hash=(Hash-Image $bytes); image=$bytes; input=$after; hwnd=$hwnd; focus=$focus; windowBounds=$windowBounds; caret=$caret; automationFocus=$automationFocus; editTarget=$editTarget}
   $data=@{frame_id=$script:frame.id; session=$session; bounds=[RemoteDesktop]::Bounds(); title=[RemoteDesktop]::Title(); foreground_bounds=$windowBounds; caret_bounds=$caret; input_enabled=[bool]$AllowInput; target_guard=[bool]$TargetGuard; focused_edit_available=($null -ne $editTarget)}
   Add-Screenshot $data $bytes $format
   return $data
 }
 function ObserveAfterInput([string]$format) {
   # Long text can still be rendering after SendInput. Retry ONLY observation,
   # never the input above it, and only for known transient capture changes.
   for($captureAttempt=1;$captureAttempt -le 12;$captureAttempt++){
    try {
     $snapshot=Observe $format
     $snapshot.capture_attempts=$captureAttempt
     $snapshot.application_effect="not_verified"
     return $snapshot
    }catch{
     if($_.Exception.Message -notin @('CARET_MOVED_DURING_CAPTURE','DESKTOP_CHANGED_DURING_CAPTURE','FOCUS_OR_WINDOW_MOVED_DURING_CAPTURE','AUTOMATION_FOCUS_CHANGED_DURING_CAPTURE','EDIT_TARGET_CHANGED_DURING_CAPTURE') -or $captureAttempt -eq 12){throw}
     Start-Sleep -Milliseconds 250
    }
   }
 }
 Write-Host 'Codex remote desktop worker running. Close this window / Ctrl+C to stop.'
 Write-Host "Input enabled: $([bool]$AllowInput). This controls THIS Windows desktop; do not use it concurrently."
 Write-Host 'No listening TCP port, no AI login, no automatic startup. Close this window to stop.'
 if(-not $RelayConfig){. (Join-Path $PSScriptRoot 'Live-Desktop.ps1')}
 while (-not (Test-WorkerStop)) {
  if(-not $RelayConfig){Invoke-LiveDesktop}
  if($RelayConfig){
   try {
    $pollTimer=[Diagnostics.Stopwatch]::StartNew()
    $incoming=[DesktopRelay]::Exchange(($relay.url+'/poll?session='+$session),$relay.token,$relay.pin,'GET',$null) | ConvertFrom-Json
    $pollElapsedMs=$pollTimer.Elapsed.TotalMilliseconds
    $pollReceivedTick=[Diagnostics.Stopwatch]::GetTimestamp()
    $pollFailures = 0
   } catch {
    if(-not (Test-TransientRelayFailure $_.Exception)){throw}
    $pollFailures++
    if(-not $Persistent -and $pollFailures -ge 12){throw 'RELAY_UNAVAILABLE_WORKER_STOPPED'}
    Request-TransportRestart 'poll' $pollFailures
    $relay.url=Select-RelayEndpoint $relay.url
    Wait-Reconnect ([Math]::Min(10,$pollFailures*2))
    continue
   }
   if($Persistent){
     $live=@{pid=$PID;session=$session;host=$(if($Generation){'panel'}else{'standalone'});input=[bool]$AllowInput;guard=[bool]$TargetGuard;endpoint=[string]$relay.url;wan_only=[bool]$WanOnly;last_seen=[DateTime]::UtcNow.ToString('o')}
     $live|ConvertTo-Json -Compress|Set-Content -Encoding UTF8 (Join-Path $root 'online.json.tmp')
     Move-Item -Force (Join-Path $root 'online.json.tmp') (Join-Path $root 'online.json')
    }
   if($null -eq $incoming){continue}
   $batch=@([pscustomobject]@{Name=($incoming.id+'.request.json'); Payload=$incoming.request})
  }else{$batch=@(Get-ChildItem -LiteralPath $root -Filter '*.request.json')}
  foreach ($file in $batch) {
   $id = $file.Name.Replace('.request.json','')
   if ($id -notmatch '^[a-f0-9]{32}$') {continue}
   $attempted = $false
   $guardEvidence = $null
   $workTimer = [Diagnostics.Stopwatch]::StartNew()
   $receivedUnixMs = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
   try {
    if($RelayConfig){$request=$file.Payload}else{
     $request = Get-Content -Raw -LiteralPath $file.FullName -Encoding UTF8 | ConvertFrom-Json
     Remove-Item -LiteralPath $file.FullName
    }
    if(Test-WorkerStop){throw 'WORKER_STOPPING_BEFORE_INPUT'}
    if (-not $seen.Add($id)) {throw 'DUPLICATE_REQUEST_REJECTED'}
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    if($RelayConfig){
     # Broker supplies remaining lifetime over pinned TLS. Subtracting the
     # full poll round trip is conservative, including server long-poll wait.
     # Never compare wall clocks belonging to different machines.
     $validFor=$incoming.valid_for_ms
     $sincePoll=1000.0*([Diagnostics.Stopwatch]::GetTimestamp()-$pollReceivedTick)/[Diagnostics.Stopwatch]::Frequency
     if($null -eq $validFor -or [double]$validFor -le 0 -or [double]$validFor -gt 45000 -or ([double]$validFor-$pollElapsedMs-$sincePoll) -le 0){throw 'REQUEST_EXPIRED_IN_TRANSIT'}
    }elseif ($null -eq $request.expires -or $request.expires -lt $now -or $request.expires -gt ($now+120)) {throw 'REQUEST_EXPIRED_OR_CLOCK_MISMATCH'}
    if ($request.action -eq 'observe') {$data = Observe $request.image_format}
    elseif ($request.action -eq 'job') {
     if(-not $AllowInput){throw 'JOB_DISABLED_IN_READ_ONLY_MODE'}
     . (Join-Path $PSScriptRoot 'LocalJob.ps1')
     $data=Invoke-JobRequest $root $id $request.manifest
    }
    elseif ($request.action -eq 'status') {$data = @{session=$session; input_enabled=[bool]$AllowInput; version='0.6.0'; local_jobs_version=1; platform='windows'; keep_awake=[bool]$Persistent; previous_post_ms=$previousPostMs; previous_cleanup_ms=$previousCleanupMs}}
    else {
     if (-not $AllowInput) {throw 'READ_ONLY_START_WITH_ALLOWINPUT_AFTER_USER_PERMISSION'}
     if ($null -eq $frame -or $request.frame_id -ne $frame.id -or (([Diagnostics.Stopwatch]::GetTimestamp()-$frame.time)/[Diagnostics.Stopwatch]::Frequency) -gt 60) {throw 'STALE_FRAME_OBSERVE_AGAIN'}
     [RemoteDesktop]::Check()
     if ([RemoteDesktop]::LastInput() -ne $frame.input -or [RemoteDesktop]::GetForegroundWindow().ToInt64() -ne $frame.hwnd) {throw 'USER_OR_WINDOW_CHANGED_OBSERVE_AGAIN'}
     if([RemoteDesktop]::FocusIdentity() -ne $frame.focus -or ($frame.windowBounds -join ',') -ne ([RemoteDesktop]::ForegroundBounds() -join ',')){throw 'FOCUS_OR_WINDOW_MOVED_OBSERVE_AGAIN'}
     if($frame.automationFocus -ne [RemoteDesktop]::AutomationFocusIdentity()){throw 'AUTOMATION_FOCUS_CHANGED_OBSERVE_AGAIN'}
     $editNow=[RemoteDesktop]::FocusedEditTarget()
     if($frame.editTarget.State -ne $editNow.State){throw 'EDIT_TARGET_CHANGED_OBSERVE_AGAIN'}
     $currentImage=[RemoteDesktop]::Capture()
     if(($frame.caret -join ',') -ne ([RemoteDesktop]::CaretBounds() -join ',')){throw 'CARET_MOVED_OBSERVE_AGAIN'}
     if($TargetGuard){
      $region=$frame.windowBounds
      # Only plain text in an OS-verified editable target is scoped. Enter,
      # shortcuts, scroll and unsupported controls retain the full-window guard.
      if($request.action -eq 'text' -and $request.text -notmatch "[\r\n]" -and $null -ne $frame.editTarget){$region=$frame.editTarget.Region}
      if($request.action -in @('click','doubleclick')){$region=[int[]]@(([int]$request.x-80),([int]$request.y-80),160,160)}
      if(-not [RemoteDesktop]::RegionUnchanged($frame.image,$currentImage,$region,$frame.caret)){
       # Diagnostic image is evidence only: it has no frame_id and cannot authorize input.
       $guardEvidence=@{guard_region=$region; caret_bounds=$frame.caret; title=[RemoteDesktop]::Title()}
       Add-Screenshot $guardEvidence $currentImage $request.image_format
       throw 'TARGET_REGION_CHANGED_OBSERVE_AGAIN'
      }
     }elseif ((Hash-Image $currentImage) -ne $frame.hash) {throw 'SCREEN_CHANGED_OBSERVE_AGAIN'}
     # Capture/comparison take time: recheck ownership immediately before input.
     if([RemoteDesktop]::LastInput() -ne $frame.input -or [RemoteDesktop]::GetForegroundWindow().ToInt64() -ne $frame.hwnd -or [RemoteDesktop]::FocusIdentity() -ne $frame.focus){throw 'INPUT_OWNER_CHANGED_DURING_GUARD'}
     if(($frame.windowBounds -join ',') -ne ([RemoteDesktop]::ForegroundBounds() -join ',')){throw 'WINDOW_MOVED_DURING_GUARD'}
     if($frame.automationFocus -ne [RemoteDesktop]::AutomationFocusIdentity()){throw 'AUTOMATION_FOCUS_CHANGED_DURING_GUARD'}
     $editFinal=[RemoteDesktop]::FocusedEditTarget()
     if($frame.editTarget.State -ne $editFinal.State){throw 'EDIT_TARGET_CHANGED_DURING_GUARD'}
     if(($frame.caret -join ',') -ne ([RemoteDesktop]::CaretBounds() -join ',')){throw 'CARET_MOVED_DURING_GUARD'}
     # Accessibility providers are cross-process calls. Recheck native ownership
     # AFTER those calls as well, immediately before consuming the frame.
     if([RemoteDesktop]::LastInput() -ne $frame.input -or [RemoteDesktop]::GetForegroundWindow().ToInt64() -ne $frame.hwnd -or [RemoteDesktop]::FocusIdentity() -ne $frame.focus){throw 'INPUT_OWNER_CHANGED_DURING_FINAL_GUARD'}
     if(($frame.windowBounds -join ',') -ne ([RemoteDesktop]::ForegroundBounds() -join ',')){throw 'WINDOW_MOVED_DURING_FINAL_GUARD'}
     [RemoteDesktop]::CheckInputAccess()
     if(Test-WorkerStop){throw 'WORKER_STOPPING_BEFORE_INPUT'}
     # Consume observation before input; failures are never retried automatically.
     $frame = $null
     $attempted = $true
     switch ($request.action) {
      'click' { if ($request.button -notin @('left','right')) {throw 'INVALID_BUTTON'}; [RemoteDesktop]::Click([int]$request.x,[int]$request.y,($request.button -eq 'right')) }
      'doubleclick' {if($request.button -ne 'left'){throw 'INVALID_BUTTON'};[RemoteDesktop]::DoubleClick([int]$request.x,[int]$request.y)}
      'text' {if ($null -eq $request.text -or $request.text.Length -gt 2000) {throw 'INVALID_TEXT'}; [RemoteDesktop]::Text([string]$request.text)}
      'key' {
       $map=@{CTRL=17;ALT=18;SHIFT=16;META=91;ENTER=13;TAB=9;ESC=27;BACKSPACE=8;DELETE=46;LEFT=37;UP=38;RIGHT=39;DOWN=40;HOME=36;END=35;PAGEUP=33;PAGEDOWN=34;SPACE=32;F4=115;F5=116}
       $keys=@();foreach($k in $request.keys){if($map.ContainsKey($k)){$keys += [uint16]$map[$k]} elseif($k -cmatch '^[A-Z0-9]$'){$keys += [uint16][char]$k} else {throw 'INVALID_KEY'}}
       if($keys.Count -lt 1 -or $keys.Count -gt 4){throw 'INVALID_KEY_COUNT'};[RemoteDesktop]::Keys([uint16[]]$keys)
      }
      'scroll' {if([int]$request.amount -eq 0 -or [Math]::Abs([int]$request.amount) -gt 10){throw 'INVALID_SCROLL'};[RemoteDesktop]::Scroll([int]$request.amount)}
      default {throw 'UNKNOWN_ACTION'}
     }
     Start-Sleep -Milliseconds 250
     $data = ObserveAfterInput $request.image_format
    }
    $result = @{ok=$true; request_id=$id; action=$request.action; input_attempted=$attempted; result=$data}
   } catch {
    $frame=$null
    $result = @{ok=$false; request_id=$id; input_attempted=$attempted; error=$_.Exception.Message; instruction='Observe before any new action; never replay an uncertain input.'}
    if($null -ne $guardEvidence){$result.result=$guardEvidence}
   }
   $result.worker_ms=$workTimer.Elapsed.TotalMilliseconds
   $result.received_unix_ms=$receivedUnixMs
   $result.completed_unix_ms=[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
   if($RelayConfig){$result.poll_ms=$pollElapsedMs}
   $target=Join-Path $root ($id+'.response.json')
   $result | ConvertTo-Json -Depth 8 -Compress | Set-Content -LiteralPath ($target+'.tmp') -Encoding UTF8
   Move-Item -LiteralPath ($target+'.tmp') -Destination $target -Force
   if($RelayConfig){
    # Sending the same RESULT again is safe; input itself is never retried.
    $body=[IO.File]::ReadAllText($target,[Text.Encoding]::UTF8)
    # Durable result outbox: reconnect/restart may resend this receipt only.
    @{pin=$relay.pin;body=$body}|ConvertTo-Json -Compress|Set-Content -LiteralPath ($outbox+'.tmp') -Encoding UTF8
    Move-Item -LiteralPath ($outbox+'.tmp') -Destination $outbox -Force
    $postTimer=[Diagnostics.Stopwatch]::StartNew()
    Send-ResultReceipt $body
    Remove-Item -LiteralPath $outbox
    $previousPostMs=$postTimer.Elapsed.TotalMilliseconds
   }
  }
  # Expire sensitive response artifacts after 10 minutes, without logging contents.
  $cleanupTimer=[Diagnostics.Stopwatch]::StartNew()
  Get-ChildItem -LiteralPath $root -Filter '*.response.json' | Where-Object {$_.LastWriteTimeUtc -lt [DateTime]::UtcNow.AddMinutes(-10)} | Remove-Item
  $previousCleanupMs=$cleanupTimer.Elapsed.TotalMilliseconds
  # Long polling already blocks when idle; only the filesystem mailbox needs a delay.
  if(-not $RelayConfig){Start-Sleep -Milliseconds 15}
 }
} finally {
 if('RemoteDesktop' -as [type]){[RemoteDesktop]::LiveRelease()}
 try{if(Get-Command Close-LocalJobHandle -CommandType Function -ErrorAction SilentlyContinue){Close-LocalJobHandle -Wait}}
 finally{if($Persistent -and ('RemoteDesktop' -as [type])){[void][RemoteDesktop]::KeepAwake($false)};$lock.Dispose()}
}
