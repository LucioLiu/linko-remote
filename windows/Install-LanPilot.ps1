param([ValidateRange(1,300)][int]$HeartbeatTimeout=35,[switch]$IsolatedTest,[switch]$NewIdentityOnly)
$ErrorActionPreference='Stop'
function Get-InstalledHeartbeat([string]$Root,[string]$Destination,[datetime]$Since) {
 $path=Join-Path $Root 'online.json'
 try{
  $live=Get-Content -LiteralPath $path -Raw|ConvertFrom-Json
  $when=([DateTimeOffset]$live.last_seen).UtcDateTime
  if($when -lt $Since -or $when -gt [DateTime]::UtcNow.AddSeconds(2)){return}
  $worker=Get-CimInstance Win32_Process -Filter ('ProcessId='+[int]$live.pid) -ErrorAction Stop
  if($live.session -notmatch '^[a-f0-9]{32}$'){return}
  $filename=if($live.host -eq 'panel'){'LanPilot.ps1'}else{'Start-Desktop.ps1'}
  $scriptPath=Join-Path $Destination $filename
  if($worker.SessionId -eq (Get-Process -Id $PID).SessionId -and $worker.CommandLine -match ('(?i)(?:^|\s)-File\s+"?'+[regex]::Escape($scriptPath)+'"?(?:\s|$)')){
   return $live
  }
 }catch{}
}
function Write-InstallState([string]$State,$Heartbeat) {
 $result=[ordered]@{state=$State;directory=$dest;startup=$startup;worker_pid=$null;heartbeat_utc=$null;checked_utc=[DateTime]::UtcNow.ToString('o')}
 if($Heartbeat){$result.worker_pid=$Heartbeat.pid;$result.heartbeat_utc=$Heartbeat.last_seen}
 $raw=$result|ConvertTo-Json -Compress
 [IO.File]::WriteAllText((Join-Path $dest 'install-result.json'),$raw)
 Write-Output $raw
}
try {
 if($IsolatedTest -and ($env:LANPILOT_ISOLATED -notmatch '^[0-9a-f]{32}$' -or $env:LOCALAPPDATA -ne (Join-Path ([IO.Path]::GetTempPath()) ('LanPilot-Invite-QA-'+$env:LANPILOT_ISOLATED)))){throw 'Invalid isolated acceptance profile.'}
 Add-Type -AssemblyName System.Security
 $root=Join-Path $env:LOCALAPPDATA 'CodexRemoteDesktop'
 $key=Join-Path $root 'pair-key.dpapi'
 if(-not (Test-Path $key)){throw 'This installer requires the pairing key for this Windows account. Run Pair-Device.ps1 first.'}
 $manifest=Get-Content -Raw (Join-Path $PSScriptRoot 'manifest.json')|ConvertFrom-Json
 foreach($f in $manifest.PSObject.Properties){if((Get-FileHash (Join-Path $PSScriptRoot $f.Name)).Hash -ne $f.Value){throw ('Package checksum mismatch: '+$f.Name)}}
 . (Join-Path $PSScriptRoot 'UserStartup.ps1')
 if(-not ('DesktopRelay' -as [type])){Add-Type -Path (Join-Path $PSScriptRoot 'Relay.cs')}
 # Prove this package belongs to this Windows user BEFORE touching any running program.
 $rsa=New-Object Security.Cryptography.RSACryptoServiceProvider
 $rsa.PersistKeyInCsp=$false
 try {
  $rsa.ImportCspBlob([Security.Cryptography.ProtectedData]::Unprotect([IO.File]::ReadAllBytes($key),$null,[Security.Cryptography.DataProtectionScope]::CurrentUser))
  $pair=[Text.Encoding]::UTF8.GetString($rsa.Decrypt([IO.File]::ReadAllBytes((Join-Path $PSScriptRoot 'pairing.bin')),$true))|ConvertFrom-Json
  if($pair.pin -notmatch '^[a-f0-9]{64}$' -or -not $pair.token){throw 'Invalid pairing'}
  [void][DesktopRelay]::NormalizeEndpoint($pair.url)
 }finally{$rsa.Dispose()}
 $base=Join-Path $env:LOCALAPPDATA 'LanPilot'
 $dest=Join-Path $base ('0.7.0-'+(Get-Date -Format 'yyyyMMddHHmmssfff'))
 New-Item -ItemType Directory -Force $dest|Out-Null
 foreach($name in @('Desktop.cs','Relay.cs','Discovery.cs','Start-Desktop.ps1','LanPilot.ps1','Launcher.cs','UserStartup.ps1','WorkerRunspace.ps1','LocalJob.ps1','LocalJobHost.ps1','LedgerReconcile.cs')){Copy-Item (Join-Path $PSScriptRoot $name) $dest}
 Add-Type -ReferencedAssemblies System.Windows.Forms -TypeDefinition ([IO.File]::ReadAllText((Join-Path $dest 'Launcher.cs'))) -OutputAssembly (Join-Path $dest 'LanPilot.exe') -OutputType WindowsApplication
 # Validate script syntax before changing the installed instance.
 foreach($name in @('Start-Desktop.ps1','LanPilot.ps1','UserStartup.ps1','WorkerRunspace.ps1','LocalJob.ps1','LocalJobHost.ps1')){
  $tokens=$null;$errors=$null
  [void][Management.Automation.Language.Parser]::ParseFile((Join-Path $dest $name),[ref]$tokens,[ref]$errors)
  if($errors.Count){throw ('Invalid script: '+$name)}
 }
 $pending=Join-Path $root 'pending-receipt.json'
 $target=Join-Path $root 'relay-pairing.bin'
 $incoming=Join-Path $PSScriptRoot 'pairing.bin'
 $same=(Test-Path -LiteralPath $target) -and ((Get-FileHash -LiteralPath $target).Hash -eq (Get-FileHash -LiteralPath $incoming).Hash)
 if(-not $same) {
  # If the installed identity has the same token and certificate, keep it. A package may
  # contain fresh RSA-OAEP ciphertext for that same pairing.
  if(Test-Path -LiteralPath $target){
   $rsa=New-Object Security.Cryptography.RSACryptoServiceProvider
   $rsa.PersistKeyInCsp=$false
   try{
    $rsa.ImportCspBlob([Security.Cryptography.ProtectedData]::Unprotect([IO.File]::ReadAllBytes($key),$null,[Security.Cryptography.DataProtectionScope]::CurrentUser))
    $prior=[Text.Encoding]::UTF8.GetString($rsa.Decrypt([IO.File]::ReadAllBytes($target),$true))|ConvertFrom-Json
    $same=$prior.token -ceq $pair.token -and $prior.pin -ceq $pair.pin -and $prior.url -eq $pair.url
   }finally{$rsa.Dispose()}
  }
 }
 if(-not $same){
  $lock=[IO.File]::Open((Join-Path $root 'worker.lock'),'OpenOrCreate','ReadWrite','None')
  try {
   if($NewIdentityOnly -and (Test-Path -LiteralPath $target)){throw 'Invitation cannot replace paired identity.'}
   if(Test-Path -LiteralPath $pending){throw 'Unconfirmed receipt: reconcile the original request before changing the active pairing.'}
   $temp=Join-Path $root ([guid]::NewGuid().ToString('N')+'.pair')
   try{
    [IO.File]::Copy($incoming,$temp)
    if(Test-Path -LiteralPath $target){[IO.File]::Replace($temp,$target,($target+'.backup-'+[guid]::NewGuid().ToString('N')))}
    else{[IO.File]::Move($temp,$target)}
   }finally{if(Test-Path -LiteralPath $temp){Remove-Item -LiteralPath $temp}}
  }finally{$lock.Dispose()}
 }
 $exe=Join-Path $dest 'LanPilot.exe'
 [IO.File]::WriteAllText((Join-Path $root 'client.json'),(@{exe=$exe}|ConvertTo-Json -Compress))
 if($IsolatedTest){$startup='isolated_test_no_startup'}else{
 $startup=Set-UserStartup $exe
 if((Get-UserStartup $exe) -ne $startup){throw 'Login startup target was not verified.'}
 foreach($folder in @([Environment]::GetFolderPath('Desktop'),[Environment]::GetFolderPath('Programs'))){
  try{Set-AppLink (Join-Path $folder 'LanPilot.lnk') $exe ''}
  catch{Write-Warning ('Optional shortcut unavailable: '+$_.Exception.Message)}
 }
 }
 # Keep every running panel/worker intact; the interactive worker holds the exclusive file lock.
 $since=[DateTime]::UtcNow
 Start-Process -FilePath $exe -ArgumentList '--hidden'
 $verifyStartupAfter=$since.AddSeconds(23)
 $deadline=$since.AddSeconds([Math]::Max(25,$HeartbeatTimeout))
 $startupConfirmed=[bool]$IsolatedTest
 do{
  Start-Sleep -Milliseconds 600
  if(-not $startupConfirmed -and [DateTime]::UtcNow -ge $verifyStartupAfter){
   $startup=Confirm-UserStartup $exe
   $startupConfirmed=$true
  }
  $heartbeat=Get-InstalledHeartbeat $root $dest $since
  if($heartbeat -and $startupConfirmed){Write-InstallState 'installed_connected' $heartbeat;exit 0}
 }while([DateTime]::UtcNow -lt $deadline)
 Write-InstallState 'installed_waiting_for_connection' $null
 Write-Warning 'Files and login startup verified; this installed worker has no confirmed heartbeat. A running worker, an unreachable relay, or session policy may be preventing connection.'
 exit 2
}catch{Write-Error ('INSTALL FAILED: '+$_.Exception.Message) -ErrorAction Continue;exit 1}
