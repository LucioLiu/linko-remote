# Loaded inside the existing private desktop worker. Network loss expires this
# lease; no queued input is replayed after a crash or new session.
$script:liveId='';$script:liveSeq=0;$script:liveAck=0
$script:liveEnded=New-Object 'System.Collections.Generic.HashSet[string]'
$script:liveTimer=[Diagnostics.Stopwatch]::StartNew()
function Write-LiveJson($Name,$Value){
 $path=Join-Path $root $Name
 $Value|ConvertTo-Json -Depth 8 -Compress|Set-Content -LiteralPath ($path+'.tmp') -Encoding UTF8
 Move-Item -LiteralPath ($path+'.tmp') -Destination $path -Force
}
function Stop-LiveSession([string]$Reason){
 [RemoteDesktop]::LiveDeadlineUnixMs=0
 [RemoteDesktop]::LiveRelease()
 if($script:liveId){
  [void]$script:liveEnded.Add($script:liveId)
  Write-LiveJson 'live-ended.json' @{id=$script:liveId;error=$Reason}
 }
 $script:liveId=''
}
function Invoke-LiveDesktop {
 $controlPath=Join-Path $root 'live-control.json'
 if(-not (Test-Path -LiteralPath $controlPath)){return}
 try{
  $control=Get-Content -LiteralPath $controlPath -Raw -Encoding UTF8|ConvertFrom-Json
  $now=[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()/1000.0
  if($control.id -notmatch '^[a-f0-9]{32}$' -or $control.expires -lt $now -or $control.expires -gt ($now+4) -or (Test-WorkerStop)){
   if($script:liveId){Stop-LiveSession '连续桌面已断开，按键已释放'};return
  }
  if($script:liveEnded.Contains([string]$control.id)){return}
  if($script:liveId -ne $control.id){
   if($script:liveId){Stop-LiveSession '已切换会话'}
   $script:liveId=[string]$control.id;$script:liveSeq=0;$script:liveAck=0
  }
  [RemoteDesktop]::Check()
  $geometry=[RemoteDesktop]::Bounds() -join ','
  $pattern=$script:liveId+'-*.live-input.json'
  foreach($file in @(Get-ChildItem -LiteralPath $root -Filter $pattern|Sort-Object Name)){
   $claimed=$file.FullName+'.executing'
   [IO.File]::Move($file.FullName,$claimed)
   $batch=Get-Content -LiteralPath $claimed -Raw -Encoding UTF8|ConvertFrom-Json
   if($batch.id -ne $script:liveId -or $batch.seq -ne ($script:liveAck+1) -or $batch.geometry -ne $geometry -or $batch.expires -lt ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()/1000.0)){throw '键鼠事件已过期或屏幕尺寸变化，请重新连接'}
   if(@($batch.events).Count -gt 64){throw '键鼠事件过多'}
   foreach($event in $batch.events){
    $now=[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()/1000.0
    if($now -gt $control.expires -or $now -gt $batch.expires -or (Test-WorkerStop)){throw '键鼠会话已过期'}
    [RemoteDesktop]::LiveDeadlineUnixMs=[long]([Math]::Min($control.expires,$batch.expires)*1000)
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
   [RemoteDesktop]::LiveDeadlineUnixMs=0
   $script:liveAck=[int]$batch.seq
   Write-LiveJson 'live-receipt.json' @{id=$script:liveId;seq=$script:liveAck}
   Remove-Item -LiteralPath $claimed
  }
  if($script:liveTimer.ElapsedMilliseconds -ge 120){
   $jpeg=[RemoteDesktop]::LiveCapture();$script:liveSeq++
   Write-LiveJson 'live-frame.json' @{id=$script:liveId;seq=$script:liveSeq;geometry=$geometry;created=([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()/1000.0);jpeg_base64=[Convert]::ToBase64String($jpeg)}
   $script:liveTimer.Restart()
  }
 }catch{Stop-LiveSession $_.Exception.Message}
}
