# Loaded inside the existing private desktop worker. An expired lease pauses
# capture and releases input. Only an idle session can accept a fresh lease.
$script:liveId='';$script:liveSeq=0;$script:liveAck=0;$script:liveDeadline=0
$script:liveEnded=New-Object 'System.Collections.Generic.HashSet[string]'
$script:liveTimer=[Diagnostics.Stopwatch]::StartNew()
$script:liveControlPath=Join-Path $root 'live-control.json'

function Write-LiveJson($Name,$Value){
 $path=Join-Path $root $Name
 $Value|ConvertTo-Json -Depth 8 -Compress|Set-Content -LiteralPath ($path+'.tmp') -Encoding UTF8
 Move-Item -LiteralPath ($path+'.tmp') -Destination $path -Force
}

function Get-LiveControl {
 # Let the agent atomically replace its mailbox while the worker reads it.
 try{
  $stream=[IO.File]::Open($script:liveControlPath,'Open','Read',([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
 }catch [IO.FileNotFoundException]{return}
 try{
  $reader=[IO.StreamReader]::new($stream)
  try{$control=$reader.ReadToEnd()|ConvertFrom-Json}finally{$reader.Dispose()}
 }finally{$stream.Dispose()}
 if($control.id -isnot [string] -or $control.id -cnotmatch '^[a-f0-9]{32}$' -or
    ($control.expires -isnot [double] -and $control.expires -isnot [int] -and $control.expires -isnot [long]) -or
    $control.expires -lt 0 -or -not ($control.expires -le ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()/1000.0+4))){
  throw '无效连续桌面租约'
 }
 return $control
}

function Stop-LiveSession([string]$Reason,[string]$Id=$script:liveId){
 [RemoteDesktop]::LiveDeadlineUnixMs=0
 [RemoteDesktop]::LiveRelease()
 $script:liveId=''
 $script:liveDeadline=0
 if($Id -and $script:liveEnded.Add($Id)){
  # Retain a per-session end marker across worker restarts in this workspace.
  [IO.File]::WriteAllText((Join-Path $root ($Id+'.live-ended')),'')
  Write-LiveJson 'live-ended.json' @{id=$Id;error=$Reason}
 }
}

function Stop-LiveDesktopWorker {
 $id=$script:liveId
 if(-not $id){
  try{$id=(Get-LiveControl).id}catch{}
 }
 Stop-LiveSession '连续桌面工作进程已停止，按键已释放' $id
}

function Test-LivePendingInput {
 foreach($file in Get-ChildItem -LiteralPath $root -Filter ($script:liveId+'-*.live-*')){
  if($file.Name -match '\.live-input\.json(\.executing)?$'){return $true}
  # The agent creates this marker before publishing the batch. A gap here
  # means delivery may have started even if the batch file is not visible.
  if($file.Name -match '-([0-9]+)\.live-accepted$' -and [long]$Matches[1] -gt $script:liveAck){return $true}
 }
 return $false
}

function Suspend-LiveSession {
 if([RemoteDesktop]::LiveHasHeldInput){
  Stop-LiveSession '连续桌面租约已过期，按键已释放'
  return
 }
 [RemoteDesktop]::LiveDeadlineUnixMs=0
 [RemoteDesktop]::LiveRelease()
 $script:liveDeadline=0
 if(Test-LivePendingInput){Stop-LiveSession '连续桌面租约已过期，键鼠回执未确认'}
}

function Get-CurrentLiveDeadline {
 if(Test-WorkerStop){throw '连续桌面工作进程已停止'}
 $control=Get-LiveControl
 if(-not $control -or $control.id -ne $script:liveId -or $control.expires -eq 0){
  throw '连续桌面授权已结束'
 }
 # A renewal never lengthens an operation already using a previous lease.
 return [Math]::Min($script:liveDeadline,[long][Math]::Floor($control.expires*1000))
}

function Assert-LiveInputLease {
 $deadline=Get-CurrentLiveDeadline
 $deadline=[Math]::Min($deadline,[RemoteDesktop]::LiveDeadlineUnixMs)
 if([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() -ge $deadline){throw '键鼠会话已过期'}
}

function Test-LiveCaptureLease {
 $deadline=Get-CurrentLiveDeadline
 if([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() -ge $deadline){
  Suspend-LiveSession
  return $false
 }
 return $true
}

function Invoke-LiveDesktop {
 try{
  if(Test-WorkerStop){Stop-LiveDesktopWorker;return}
  $control=Get-LiveControl
  if(-not $control){
   if($script:liveId){Stop-LiveSession '连续桌面授权已结束'}
   return
  }
  if($script:liveId -and $script:liveId -ne $control.id){Stop-LiveSession '已切换会话'}
  if($control.expires -eq 0){
   Stop-LiveSession '连续桌面授权已结束，按键已释放' $control.id
   return
  }
  if($script:liveEnded.Contains($control.id) -or
     (Test-Path -LiteralPath (Join-Path $root ($control.id+'.live-ended')))){return}

  $deadline=[long][Math]::Floor($control.expires*1000)
  $now=[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
  if(-not $script:liveId){
   $script:liveId=$control.id;$script:liveSeq=0;$script:liveAck=0
  }elseif($script:liveDeadline -eq 0){
   if($now -ge $deadline){return}
   # Recheck after a fresh lease arrives to catch input posted during the gap.
   if(Test-LivePendingInput){Stop-LiveSession '键鼠回执未确认，请重新连接';return}
  }elseif($now -ge $script:liveDeadline){
   # A renewal may have replaced the expired file before this polling pass.
   Suspend-LiveSession
   return
  }
  if($now -ge $deadline){Suspend-LiveSession;return}
  $script:liveDeadline=$deadline

  [RemoteDesktop]::Check()
  $geometry=[RemoteDesktop]::Bounds() -join ','
  $pattern=$script:liveId+'-*.live-input.json'
  foreach($file in @(Get-ChildItem -LiteralPath $root -Filter $pattern|Sort-Object Name)){
   $claimed=$file.FullName+'.executing'
   [IO.File]::Move($file.FullName,$claimed)
   $batch=Get-Content -LiteralPath $claimed -Raw -Encoding UTF8|ConvertFrom-Json
   if($batch.id -ne $script:liveId -or $batch.seq -ne ($script:liveAck+1) -or $batch.geometry -ne $geometry -or $batch.expires -le 0){
    throw '键鼠事件已过期或屏幕尺寸变化，请重新连接'
   }
   if(@($batch.events).Count -gt 64){throw '键鼠事件过多'}
   [RemoteDesktop]::LiveDeadlineUnixMs=[Math]::Min($deadline,[long][Math]::Floor($batch.expires*1000))
   [RemoteDesktop]::LiveInputGuard=[Action]{Assert-LiveInputLease}
   foreach($event in $batch.events){
    Assert-LiveInputLease
    switch($event.type){
     'move' {[RemoteDesktop]::LivePointer([int]$event.x,[int]$event.y,0,0)}
     'down' {[RemoteDesktop]::LivePointer([int]$event.x,[int]$event.y,[int]$event.button,1)}
     'up' {[RemoteDesktop]::LivePointer([int]$event.x,[int]$event.y,[int]$event.button,2)}
     'keydown' {[RemoteDesktop]::LiveKey([uint16]$event.vk,$true)}
     'keyup' {[RemoteDesktop]::LiveKey([uint16]$event.vk,$false)}
     'wheel' {[RemoteDesktop]::Scroll([int]$event.amount)}
     'text' {[RemoteDesktop]::Text([string]$event.text)}
     'release' {[RemoteDesktop]::LiveRelease()}
     default {throw '无效键鼠事件'}
    }
   }
   [RemoteDesktop]::LiveInputGuard=$null
   [RemoteDesktop]::LiveDeadlineUnixMs=0
   Write-LiveJson 'live-receipt.json' @{id=$script:liveId;seq=[int]$batch.seq}
   $script:liveAck=[int]$batch.seq
   Remove-Item -LiteralPath $claimed
  }
  if((Test-LiveCaptureLease) -and $script:liveTimer.ElapsedMilliseconds -ge 120){
   # Pace from capture start. Discard any frame completed after lease loss.
   $script:liveTimer.Restart()
   $jpeg=[RemoteDesktop]::LiveCapture()
   if(-not (Test-LiveCaptureLease)){return}
   $script:liveSeq++
   Write-LiveJson 'live-frame.json' @{id=$script:liveId;seq=$script:liveSeq;geometry=$geometry;created=([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()/1000.0);jpeg_base64=[Convert]::ToBase64String($jpeg)}
  }
 }catch{Stop-LiveSession $_.Exception.Message}
 finally{
  [RemoteDesktop]::LiveInputGuard=$null
  [RemoteDesktop]::LiveDeadlineUnixMs=0
 }
}
