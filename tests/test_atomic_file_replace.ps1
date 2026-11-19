# Exercise the actual File.Replace calls without starting a UI or changing a real profile.
$ErrorActionPreference='Stop'
$root=Join-Path ([IO.Path]::GetTempPath()) ('Linko-replace-'+[guid]::NewGuid().ToString('N'))
[void][IO.Directory]::CreateDirectory($root)
try{
 $sources=@((Join-Path $PSScriptRoot '../windows/LanPilot.ps1'),(Join-Path $PSScriptRoot '../windows/Claim-Invitation.ps1'),(Join-Path $PSScriptRoot '../windows/LocalJob.ps1'))
 if($env:LINKO_UPGRADE_SCRIPT){$sources+=$env:LINKO_UPGRADE_SCRIPT}
 $count=0
 foreach($source in $sources){
  $tokens=$null;$errors=$null
  $ast=[Management.Automation.Language.Parser]::ParseFile($source,[ref]$tokens,[ref]$errors)
  if($errors.Count){throw ('Parse error: '+$source)}
  $calls=$ast.FindAll({param($node) $node -is [Management.Automation.Language.InvokeMemberExpressionAst] -and $node.Member.Value -eq 'Replace' -and $node.Expression.Extent.Text -match 'IO\.File'},$true)
  foreach($call in $calls){
   if($call.Arguments.Count -ne 3){continue}
   if($call.Arguments[2].Extent.Text -eq '$null'){throw ('Unsafe string null in '+$source+':'+$call.Extent.StartLineNumber)}
   $count++
   $incoming=Join-Path $root ('incoming-'+$count+'.json')
   $existing=Join-Path $root ('route-'+$count+'.json')
   $old='{"url":"https://old.example","name":"old"}'
   $new='{"url":"https://new.example","name":"Test PC"}'
   [IO.File]::WriteAllText($incoming,$new)
   [IO.File]::WriteAllText($existing,$old)
   # Bind only the two filesystem arguments, then execute the real source expression.
   & {
    param($Expression,$SourceVariable,$TargetVariable,$Incoming,$Existing)
    Set-Variable -Name $SourceVariable -Value $Incoming -Scope Local
    Set-Variable -Name $TargetVariable -Value $Existing -Scope Local
    & ([scriptblock]::Create($Expression))
   } $call.Extent.Text $call.Arguments[0].VariablePath.UserPath $call.Arguments[1].VariablePath.UserPath $incoming $existing
   if([IO.File]::Exists($incoming) -or [IO.File]::ReadAllText($existing) -cne $new){throw ('Replacement not persisted: '+$source)}
  }
 }
 if($count -lt 5){throw 'Expected real panel, pairing and job replacement calls.'}
 Write-Output ('Atomic file replacement checks passed: '+$count+' source calls.')
}finally{[IO.Directory]::Delete($root,$true)}
