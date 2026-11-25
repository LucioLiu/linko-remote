$ErrorActionPreference='Stop'

# Exercise the Windows mailbox and session logic on any PowerShell host.
# The shim records the native boundary without accessing a desktop.
Add-Type -TypeDefinition @'
using System;
public static class RemoteDesktop {
 public static long LiveDeadlineUnixMs;
 public static Action LiveInputGuard, BeforeSend, DuringCapture;
 public static bool KeyHeld, ButtonHeld;
 public static bool LiveHasHeldInput {get {return KeyHeld || ButtonHeld;}}
 public static int Captures, Sends, Releases, Checks;
 public static void Check() {Checks++;}
 public static int[] Bounds() {return new[]{0,0,800,600};}
 static void Send() {
  if(BeforeSend!=null)BeforeSend();
  LiveInputGuard();
  Sends++;
 }
 public static void LivePointer(int x,int y,int button,int transition) {
  if(transition==1)ButtonHeld=true;
  Send();
  if(transition==2)ButtonHeld=false;
 }
 public static void LiveKey(ushort vk,bool down) {
  if(down)KeyHeld=true;
  Send();
  if(!down)KeyHeld=false;
 }
 public static void Scroll(int amount) {Send();}
 public static void Text(string text) {foreach(char c in text)Send();}
 public static void LiveRelease() {Releases++;KeyHeld=ButtonHeld=false;}
 public static byte[] LiveCapture() {
  Captures++;
  if(DuringCapture!=null)DuringCapture();
  return new byte[]{1,2,3};
 }
}
'@

$source=Join-Path $PSScriptRoot '../windows/Live-Desktop.ps1'
$base=Join-Path ([IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString('N'))
$script:workerStop=$false

function Test-WorkerStop {return $script:workerStop}
function Assert($Condition,[string]$Message){if(-not $Condition){throw $Message}}
function Now-Live {return [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()/1000.0}
function Write-Control($Expires){
 Write-LiveJson 'live-control.json' @{id=$id;expires=$Expires}
}
function Write-Batch([int]$Seq,$Events,[double]$Expires=((Now-Live)+1.5)){
 $prefix='{0}-{1:D10}' -f $id,$Seq
 [IO.File]::WriteAllText((Join-Path $root ($prefix+'.live-accepted')),'')
 Write-LiveJson ($prefix+'.live-input.json') @{id=$id;seq=$Seq;geometry='0,0,800,600';expires=$Expires;events=@($Events)}
}
function Invoke-Tick {
 # Force the capture interval to elapse without relying on test timing.
 Start-Sleep -Milliseconds 125
 Invoke-LiveDesktop
}
function Assert-Ended {
 $end=Get-Content -LiteralPath (Join-Path $root 'live-ended.json') -Raw|ConvertFrom-Json
 Assert ($end.id -eq $id -and -not $script:liveId) 'Expected terminal session'
 Assert (-not [RemoteDesktop]::LiveHasHeldInput) 'Input still held after end'
 $captures=[RemoteDesktop]::Captures
 $sends=[RemoteDesktop]::Sends
 Write-Control ((Now-Live)+2.5)
 Invoke-Tick
 Assert ([RemoteDesktop]::Captures -eq $captures -and [RemoteDesktop]::Sends -eq $sends) 'Ended session accepted a renewal'
}
function Run-Case([string]$Name,[scriptblock]$Body){
 $root=Join-Path $base $Name
 [void][IO.Directory]::CreateDirectory($root)
 $id=[guid]::NewGuid().ToString('N')
 $script:workerStop=$false
 [RemoteDesktop]::BeforeSend=$null
 [RemoteDesktop]::DuringCapture=$null
 [RemoteDesktop]::Captures=[RemoteDesktop]::Sends=[RemoteDesktop]::Checks=0
 [RemoteDesktop]::LiveRelease()
 . $source
 & $Body
 Write-Output "PASS $Name"
}

try{
 Run-Case 'idle resumes with frame and input sequence intact' {
  Write-Control ((Now-Live)+2.5)
  Write-Batch 1 @{type='move';x=5;y=5}
  Invoke-Tick
  Assert ($script:liveSeq -eq 1 -and $script:liveAck -eq 1 -and [RemoteDesktop]::Sends -eq 1) 'Initial frame or receipt missing'
  $released=[RemoteDesktop]::Releases
  foreach($gap in 1..2){
   Write-Control ((Now-Live)-0.1)
   Invoke-Tick
   Assert ($script:liveSeq -eq $gap -and [RemoteDesktop]::Releases -gt $released -and $script:liveDeadline -eq 0) 'Expired lease did not pause capture and release'
   Assert (-not (Test-Path -LiteralPath (Join-Path $root 'live-ended.json'))) 'Idle expiry ended session'
   Invoke-Tick
   Assert ($script:liveSeq -eq $gap) 'Expired lease captured a frame'
   Write-Control ((Now-Live)+2.5)
   Invoke-Tick
   Assert ($script:liveSeq -eq ($gap+1) -and $script:liveAck -eq 1 -and [RemoteDesktop]::Sends -eq 1) 'Resume reset a sequence or replayed input'
  }
  Write-Batch 2 @{type='move';x=7;y=8}
  Invoke-LiveDesktop
  Assert ($script:liveAck -eq 2 -and [RemoteDesktop]::Sends -eq 2) 'Next input sequence did not execute'
 }
 Run-Case 'renewal arrives before next poll after observed deadline' {
  Write-Control ((Now-Live)+0.25)
  Invoke-LiveDesktop
  Start-Sleep -Milliseconds 275
  Write-Control ((Now-Live)+2.5)
  Invoke-LiveDesktop
  Assert ($script:liveDeadline -eq 0 -and [RemoteDesktop]::Captures -eq 0) 'Overwritten expiry did not pause the session'
  Invoke-Tick
  Assert ($script:liveSeq -eq 1) 'Fresh lease did not resume'
 }
 foreach($kind in 'keydown','down','queued','executing','accepted'){
  Run-Case "expiry with $kind is terminal" {
   Write-Control ((Now-Live)+2.5)
   Invoke-LiveDesktop
   switch($kind){
    'keydown' {Write-Batch 1 @{type='keydown';vk=65};Invoke-LiveDesktop}
    'down' {Write-Batch 1 @{type='down';button=0;x=5;y=5};Invoke-LiveDesktop}
    'queued' {Write-Batch 1 @{type='move';x=5;y=5}}
    'executing' {[IO.File]::WriteAllText((Join-Path $root ($id+'-0000000001.live-input.json.executing')),'')}
    'accepted' {[IO.File]::WriteAllText((Join-Path $root ($id+'-0000000001.live-accepted')),'')}
   }
   Write-Control ((Now-Live)-0.1)
   Invoke-LiveDesktop
   Assert-Ended
   Assert ([RemoteDesktop]::Sends -eq [int]($kind -in 'keydown','down')) 'Unexpected input dispatch'
  }
 }
 Run-Case 'batch posted while paused is never executed' {
  Write-Control ((Now-Live)-0.5)
  Invoke-LiveDesktop
  Write-Batch 1 @{type='move';x=5;y=5}
  Write-Control ((Now-Live)+2.5)
  Invoke-LiveDesktop
  Assert-Ended
  Assert ([RemoteDesktop]::Sends -eq 0) 'Late batch executed'
 }
 Run-Case 'revoke while paused survives a worker restart' {
  Write-Control ((Now-Live)-0.5)
  Invoke-LiveDesktop
  Write-Control 0
  Invoke-LiveDesktop
  Assert-Ended
  . $source
  Invoke-Tick
  Assert ([RemoteDesktop]::Captures -eq 0 -and -not $script:liveId) 'Tombstoned session reopened after restart'
 }
 Run-Case 'worker stop wins over a valid lease' {
  Write-Control ((Now-Live)+2.5)
  Write-Batch 1 @{type='keydown';vk=65}
  Invoke-LiveDesktop
  $script:workerStop=$true
  Invoke-LiveDesktop
  $script:workerStop=$false
  Assert-Ended
 }
 Run-Case 'worker cleanup ends the current session' {
  Write-Control ((Now-Live)+2.5)
  Invoke-LiveDesktop
  Stop-LiveDesktopWorker
  Assert-Ended
 }
 Run-Case 'expired batch cannot borrow time from a renewal' {
  Write-Control ((Now-Live)+2.5)
  Write-Batch 1 @{type='move';x=5;y=5} ((Now-Live)-0.1)
  Invoke-LiveDesktop
  Assert-Ended
  Assert ([RemoteDesktop]::Sends -eq 0) 'Expired input executed'
 }
 foreach($change in 'expiry','revoke','stop','batch deadline','pinned lease'){
  Run-Case "$change during native input stops the rest of the batch" {
   # Function scope remains visible to delegates invoked synchronously by Send.
   $script:guardCount=0
   [RemoteDesktop]::BeforeSend=[Action]{
    $script:guardCount++
    if($script:guardCount -ne 2){return}
    switch($change){
     'expiry' {Write-Control ((Now-Live)-0.1)}
     'revoke' {Write-Control 0}
     'stop' {$script:workerStop=$true}
     'batch deadline' {Start-Sleep -Milliseconds 350;Write-Control ((Now-Live)+2.5)}
     'pinned lease' {Write-Control ((Now-Live)+2.5);Start-Sleep -Milliseconds 350}
    }
   }
   $leaseMs=if($change -eq 'pinned lease'){0.3}else{2.5}
   $batchMs=if($change -eq 'batch deadline'){0.3}else{1.5}
   Write-Control ((Now-Live)+$leaseMs)
   Write-Batch 1 @{type='text';text='abc'} ((Now-Live)+$batchMs)
   Invoke-LiveDesktop
   $script:workerStop=$false
   Assert-Ended
   Assert ([RemoteDesktop]::Sends -eq 1 -and $script:liveAck -eq 0) 'Partial batch was continued or acknowledged'
  }
 }
 Run-Case 'capture completing after expiry is discarded' {
  [RemoteDesktop]::DuringCapture=[Action]{Write-Control ((Now-Live)-0.1)}
  Write-Control ((Now-Live)+2.5)
  Invoke-Tick
  Assert ([RemoteDesktop]::Captures -eq 1 -and $script:liveSeq -eq 0 -and $script:liveDeadline -eq 0) 'Expired frame was published'
  [RemoteDesktop]::DuringCapture=$null
  Write-Control ((Now-Live)+2.5)
  Invoke-Tick
  Assert ($script:liveSeq -eq 1) 'Idle capture did not recover'
 }
}finally{
 if([IO.Directory]::Exists($base)){[IO.Directory]::Delete($base,$true)}
}
