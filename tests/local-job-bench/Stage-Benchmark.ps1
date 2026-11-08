param([ValidateSet('A','B')][String]$InitialRoute)
$ErrorActionPreference='Stop'
$desktopRoot=Join-Path $env:LOCALAPPDATA CodexRemoteDesktop
$inbox=Join-Path $desktopRoot job-inbox
$inputDir=Join-Path $inbox bench1109
if((Test-Path (Join-Path $inbox 'bench1109-a/result.csv')) -or (Test-Path (Join-Path $inbox 'bench1109-b/receipt.json'))){throw 'Refusing to replace an existing benchmark output'}
New-Item -Type Directory -Force $inputDir|Out-Null
$unpack=Join-Path $PSScriptRoot inputs
Expand-Archive -LiteralPath (Join-Path $PSScriptRoot inputs.zip) -DestinationPath $unpack
$files=@(Get-ChildItem -LiteralPath $unpack -File)
if($files.Count -ne 12){throw 'Expected 12 synthetic inputs'}
foreach($f in $files){
 if($f.Name -notmatch '^(expected|actual)-0[1-6]\.csv$'){throw 'Unexpected input filename'}
 $to=Join-Path $inputDir $f.Name
 if(Test-Path $to){if((Get-FileHash $to).Hash -ne (Get-FileHash $f.FullName).Hash){throw 'Conflicting existing input'}}else{Copy-Item -LiteralPath $f.FullName -Destination $to}
}
$fxdir=Join-Path $env:LOCALAPPDATA 'LanPilot/BenchReconcile1109'
New-Item -Type Directory -Force $fxdir|Out-Null
$exe=Join-Path $fxdir ReconcileGui.exe
if(Test-Path $exe){throw 'Benchmark GUI already exists'}
$sources=@((Join-Path $PSScriptRoot LedgerReconcile.cs),(Join-Path $PSScriptRoot ReconcileGui.cs))
$references=@('System','System.Core','System.Xml','System.Windows.Forms','System.Drawing','System.Web.Extensions','Microsoft.VisualBasic','System.Runtime.Serialization')
Add-Type -Path $sources -ReferencedAssemblies $references -OutputAssembly $exe -OutputType WindowsApplication
if(-not (Test-Path -LiteralPath $exe)){throw 'Fixture binary unavailable'}
$q=Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine=('"'+$exe+'" '+$InitialRoute)}
if($q.ReturnValue -ne 0){throw 'Failed to launch ordinary desktop fixture'}
Start-Sleep -Milliseconds 600
$p=Get-Process -Id $q.ProcessId
Write-Host 'BENCH_GUI_READY' $InitialRoute $p.Id $p.SessionId $p.MainWindowTitle
