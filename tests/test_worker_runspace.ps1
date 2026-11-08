$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot '../windows/WorkerRunspace.ps1')
$root=[IO.Path]::Combine([IO.Path]::GetTempPath(),[guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($root)|Out-Null
$script=Join-Path $root 'worker-fixture.ps1'
$fixture=@'
param([string]$Root,[Threading.ManualResetEvent]$StopEvent,[string]$Generation)
$ErrorActionPreference='Stop'
$lock=[IO.File]::Open((Join-Path $Root 'lock'),'OpenOrCreate','ReadWrite','None')
try{
 $live=@{pid=$PID;session=$Generation;apartment=[Threading.Thread]::CurrentThread.GetApartmentState().ToString()}|ConvertTo-Json
 [IO.File]::WriteAllText((Join-Path $Root ($Generation+'.json')),$live)
 [void]$StopEvent.WaitOne(10000)
 # Simulate completion of a durable receipt before releasing the worker lock.
 [IO.File]::WriteAllText((Join-Path $Root ($Generation+'.receipt')),'drained')
}finally{$lock.Dispose()}
'@
[IO.File]::WriteAllText($script,$fixture)
try{
 $previous=''
 foreach($i in 1..2){
  $worker=New-DesktopWorker $script @{Root=$root}
  try{
   if($worker.Generation -eq $previous){throw 'Worker generation was reused'}
   $previous=$worker.Generation
   $file=Join-Path $root ($worker.Generation+'.json')
   $deadline=[DateTime]::UtcNow.AddSeconds(4)
   while(-not (Test-Path -LiteralPath $file) -and [DateTime]::UtcNow -lt $deadline){Start-Sleep -Milliseconds 20}
   $live=Get-Content -LiteralPath $file -Raw|ConvertFrom-Json
   if($live.pid -ne $PID -or $live.session -cne $worker.Generation){throw 'Worker host identity mismatch'}
   if($env:OS -eq 'Windows_NT' -and $live.apartment -ne 'STA'){throw 'Wrong desktop worker thread model'}
   $extra=New-DesktopWorker $script @{Root=$root}
   $failed=$false
   try{Close-DesktopWorker $extra}catch{$failed=$true}
   if(-not $failed){throw 'Duplicate worker acquired the lock'}
   Request-DesktopWorkerStop $worker
  }finally{Close-DesktopWorker $worker}
  if(-not (Test-Path -LiteralPath (Join-Path $root ($previous+'.receipt')))){throw 'Worker cleanup did not finish'}
 }
}finally{[IO.Directory]::Delete($root,$true)}
Write-Output 'runspace lifecycle OK'
