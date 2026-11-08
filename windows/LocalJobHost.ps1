param([Parameter(Mandatory=$true)][ValidatePattern('^[a-f0-9]{32}$')][string]$RequestId)
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'LocalJob.ps1')
$root=Join-Path $env:LOCALAPPDATA 'CodexRemoteDesktop'
$folder=Join-Path (Join-Path $root 'job-queue') $RequestId
$done=Join-Path $folder 'finished.json'
$slot=$null;$running=$null
try{
 Test-PlainJobPath $root;Test-PlainJobPath (Split-Path $folder);Test-PlainJobPath $folder
 $slot=[IO.File]::Open((Join-Path (Split-Path $folder) 'worker.lock'),[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
 $running=[IO.File]::Open((Join-Path $folder 'running.lock'),[IO.FileMode]::CreateNew,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
 $path=Join-Path $folder 'manifest.json';Test-PlainJobPath $path
 $manifest=[IO.File]::ReadAllText($path)|ConvertFrom-Json
 if($manifest.operation -cne 'reconcile-ledgers' -or $manifest.execution -cne 'background'){throw 'JOB_INVALID_BACKGROUND_MANIFEST'}
 Initialize-JobEngine
 $data=Invoke-LocalJob $root $RequestId $manifest
 Write-JobJson $done @{ok=$true;data=$data}
}catch{Write-JobJson $done @{ok=$false;error=$_.Exception.Message}}finally{if($running){$running.Dispose()};if($slot){$slot.Dispose()}}
