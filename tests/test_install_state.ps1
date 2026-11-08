# Offline test: mock only Windows I/O. This does not change startup, registry or processes.
$ErrorActionPreference='Stop'
$windows=Join-Path $PSScriptRoot '../windows'
foreach($name in @('LanPilot.ps1','Start-Desktop.ps1','Install-LanPilot.ps1','UserStartup.ps1','WorkerRunspace.ps1')){
 $errors=$null;$tokens=$null
 $parsed=[Management.Automation.Language.Parser]::ParseFile((Join-Path $windows $name),[ref]$tokens,[ref]$errors)
 if($errors.Count){throw "$name failed to parse: $($errors[0])"}
 if($name -eq 'Install-LanPilot.ps1'){
  $definition=$parsed.Find({param($node)$node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-InstalledHeartbeat'},$true)
  . ([scriptblock]::Create($definition.Extent.Text))
 }
}
. (Join-Path $windows 'UserStartup.ps1')
$fakeExe='/fake/LanPilot.exe';$script:written=$null
function Test-StartupDisabled {return $false}
function Get-StartupLinkPath {return 'mock-startup'}
function Set-AppLink {throw 'Shortcut not persisted'}
function Test-Path {return $true}
function New-ItemProperty {param($LiteralPath,$Name,$Value,$PropertyType,[switch]$Force);$script:written=$Value}
function Get-RunCommand {return $script:written}
if((Set-UserStartup $fakeExe) -ne 'HKCU Run'){throw 'Fallback route not selected'}
if((Get-UserStartup $fakeExe) -ne 'HKCU Run'){throw 'Persisted startup not recognized'}
function New-ItemProperty {param($LiteralPath,$Name,$Value,$PropertyType,[switch]$Force)}
$script:written=$null
try{
 Set-UserStartup $fakeExe
 throw 'Missing verification'
}catch{
 if($_.Exception.Message -ne 'Current-user Run entry was not persisted.'){throw}
}
function New-ItemProperty {param($LiteralPath,$Name,$Value,$PropertyType,[switch]$Force);$script:written=$Value}
function Get-StartupLinkTarget {return $null}
if((Confirm-UserStartup $fakeExe) -ne 'HKCU Run'){throw 'Late shortcut disappearance not recovered'}
function Test-StartupDisabled {return $true}
try{Confirm-UserStartup $fakeExe;throw 'Policy bypassed'}
catch{if($_.Exception.Message -ne 'Login startup disabled by Windows settings or policy.'){throw}}
$liveTime=[DateTime]::UtcNow
$since=$liveTime.AddSeconds(-2);$fakeFolder='/fake/runtime';$workerScript=Join-Path $fakeFolder 'Start-Desktop.ps1'
$script:cmd='powershell -File "'+$workerScript+'" -Persistent'
function Get-Content {param($LiteralPath,[switch]$Raw);return (@{pid=125;session='a'*32;host=$script:workerHost;last_seen=$script:liveTime.ToString('o')}|ConvertTo-Json)}
function Get-CimInstance {param($ClassName,$Filter,$ErrorAction);return @{SessionId=3;CommandLine=$script:cmd}}
function Get-Process {param($Id);return @{SessionId=3}}
if(-not (Get-InstalledHeartbeat '/fake' $fakeFolder $since)){throw 'Fresh heartbeat missing'}
$script:workerHost='panel';$script:cmd='powershell -File "'+(Join-Path $fakeFolder 'LanPilot.ps1')+'" -StartHidden'
if(-not (Get-InstalledHeartbeat '/fake' $fakeFolder $since)){throw 'Live panel-hosted worker heartbeat missing'}
$script:workerHost='standalone'
$script:cmd='powershell -File /old/Start-Desktop.ps1'
if(Get-InstalledHeartbeat '/fake' $fakeFolder $since){throw 'Old worker heartbeat accepted'}
$script:cmd='powershell -File "'+$workerScript+'"'
$script:liveTime=$since.AddSeconds(-3)
if(Get-InstalledHeartbeat '/fake' $fakeFolder $since){throw 'Stale heartbeat accepted'}
Write-Output 'install state OK'
