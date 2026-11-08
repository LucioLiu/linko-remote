# Start desktop I/O on a dedicated interactive STA thread in the panel's process.
function New-DesktopWorker([string]$ScriptPath,[hashtable]$Parameters) {
 $space=[RunspaceFactory]::CreateRunspace()
 $stop=New-Object Threading.ManualResetEvent($false)
 $pipeline=[PowerShell]::Create()
 try {
  $space.ApartmentState='STA';$space.ThreadOptions='ReuseThread';$space.Open()
  $pipeline.Runspace=$space
  $generation=[guid]::NewGuid().ToString('N')
  [void]$pipeline.AddCommand($ScriptPath).AddParameters($Parameters).AddParameter('StopEvent',$stop).AddParameter('Generation',$generation)
  $invocation=$pipeline.BeginInvoke()
  return [pscustomobject]@{Runspace=$space;Pipeline=$pipeline;Invocation=$invocation;StopEvent=$stop;Generation=$generation}
 }catch{
  $pipeline.Dispose();$space.Dispose();$stop.Dispose();throw
 }
}

function Request-DesktopWorkerStop($Worker) {
 if($Worker){[void]$Worker.StopEvent.Set()}
}

function Close-DesktopWorker($Worker) {
 if(-not $Worker){return}
 Request-DesktopWorkerStop $Worker
 try{
  # Worker drains its current result, retaining the outbox if delivery is unavailable.
  [void]$Worker.Pipeline.EndInvoke($Worker.Invocation)
 }finally{
  $Worker.Pipeline.Dispose();$Worker.Runspace.Dispose();$Worker.StopEvent.Dispose()
 }
}
