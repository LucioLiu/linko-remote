$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Windows.Forms
$meta=Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot 'invitation.json') -Encoding UTF8|ConvertFrom-Json
$isolated=([string]$meta.isolated_test -match '^[0-9a-f]{32}$')
if($isolated){
 $env:LANPILOT_ISOLATED=[string]$meta.isolated_test
 $env:LOCALAPPDATA=Join-Path ([IO.Path]::GetTempPath()) ('LanPilot-Invite-QA-'+$env:LANPILOT_ISOLATED)
}else{Remove-Item Env:LANPILOT_ISOLATED -ErrorAction SilentlyContinue}
$root=Join-Path $env:LOCALAPPDATA 'CodexRemoteDesktop'
if(Test-Path -LiteralPath (Join-Path $root 'relay-pairing.bin')){throw 'Already paired: this invitation cannot replace an existing identity.'}
if([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() -ge [int64]$meta.expires){throw 'Invitation expired; obtain a new installer.'}
$msg="Connect this Windows account to the LanPilot Mac controller as '$($meta.name)'? The controller will be able to view and operate this signed-in desktop."
if($isolated){$msg="Connect a temporary READ-ONLY test profile as '$($meta.name)'? Your normal pairing, startup and shortcuts will not be changed."}
if([Globalization.CultureInfo]::CurrentUICulture.TwoLetterISOLanguageName -eq 'zh'){
 if($isolated){$msg="将此电脑以「$($meta.name)」的名称临时接入邻控吗？这个验收实例只能查看屏幕，不会修改已有配对、启动项或快捷方式。"}
 else{$msg="将此 Windows 账户以「$($meta.name)」的名称接入邻控 Mac 吗？这台 Mac 将能查看当前桌面、操作键鼠，并运行限定的文件作业。"}
}
if([Windows.Forms.MessageBox]::Show($msg,'LanPilot | Connect this PC','YesNo','Question') -ne 'Yes'){return}
Add-Type -Path (Join-Path $PSScriptRoot 'Relay.cs')
$base=[DesktopRelay]::NormalizeEndpoint([string]$meta.url)
[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12
New-Item -ItemType Directory -Force -Path $root|Out-Null
$claimLock=[IO.File]::Open((Join-Path $root 'invite.lock'),'OpenOrCreate','ReadWrite','None')
try{
if(Test-Path -LiteralPath (Join-Path $root 'relay-pairing.bin')){throw 'Existing pairing; invitation stopped.'}
$xml=& (Join-Path $PSScriptRoot 'Pair-Device.ps1')
$reply=[DesktopRelay]::Exchange(($base+'/claim'),[string]$meta.token,[string]$meta.pin,'POST',(@{public_key=[string]$xml}|ConvertTo-Json -Compress))|ConvertFrom-Json
if($reply.device_id -notmatch '^[0-9a-f]{12}$' -or ([DesktopRelay]::NormalizeEndpoint($reply.route)) -cne $base){throw 'Invalid claim response.'}
$pairing=[Convert]::FromBase64String($reply.pairing)
if($pairing.Length -ne 256){throw 'Invalid pairing ciphertext.'}
[IO.File]::WriteAllBytes((Join-Path $PSScriptRoot 'pairing.bin'),$pairing)
$pending=Join-Path $root ([guid]::NewGuid().ToString('N')+'.route')
[IO.File]::WriteAllText($pending,(@{url=$base}|ConvertTo-Json -Compress))
try {
 if(Test-Path -LiteralPath (Join-Path $root 'relay-pairing.bin')){throw 'An existing device was paired concurrently.'}
 $route=Join-Path $root 'relay-route.json'
 if(Test-Path -LiteralPath $route){[IO.File]::Replace($pending,$route,[System.Management.Automation.Language.NullString]::Value)}
 else{[IO.File]::Move($pending,$route)}
}finally{if(Test-Path -LiteralPath $pending){Remove-Item -LiteralPath $pending}}
& (Join-Path $PSScriptRoot 'Install-LanPilot.ps1') -IsolatedTest:$isolated -NewIdentityOnly
if($LASTEXITCODE -ne 0){exit $LASTEXITCODE}
}finally{$claimLock.Dispose()}
