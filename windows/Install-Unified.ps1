$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Windows.Forms
try{
 $manifest=Get-Content -LiteralPath (Join-Path $PSScriptRoot 'checksums.json') -Raw|ConvertFrom-Json
 foreach($entry in $manifest.PSObject.Properties){
  $file=Join-Path $PSScriptRoot $entry.Name
  if((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -ine $entry.Value){throw ('文件校验失败：'+$entry.Name)}
 }
 $base=Join-Path $env:LOCALAPPDATA 'LinkoRemote\app'
 $build=(Get-FileHash -LiteralPath (Join-Path $PSScriptRoot 'checksums.json')).Hash.Substring(0,10).ToLowerInvariant()
 $release=Get-Content -LiteralPath (Join-Path $PSScriptRoot 'code\release.json') -Raw|ConvertFrom-Json
 if($release.version -notmatch '^\d+\.\d+\.\d+(-(preview|beta|rc)\.\d+)?$'){throw '安装包版本信息无效'}
 $target=Join-Path $base ($release.version+'-'+$build)
 if(-not (Test-Path -LiteralPath $target)){
  New-Item -ItemType Directory -Path $target -Force|Out-Null
  foreach($name in @('runtime','code')){Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination $target -Recurse}
 }
 $python=Join-Path $target 'runtime\pythonw.exe'
 foreach($entry in $manifest.PSObject.Properties){
  if($entry.Name.StartsWith('code/') -or $entry.Name.StartsWith('runtime/')){
   if((Get-FileHash -LiteralPath (Join-Path $target $entry.Name)).Hash -ine $entry.Value){throw '已安装文件校验未通过，请保留报错并联系维护者。'}
  }
 }
 $arguments='-m lanpilot.peer_client --open-browser'
 $shell=New-Object -ComObject WScript.Shell
 foreach($folder in @('Desktop','Programs')){
  $path=Join-Path ([Environment]::GetFolderPath($folder)) '邻控统一预览.lnk'
  $link=$shell.CreateShortcut($path);$link.TargetPath=$python;$link.Arguments=$arguments;$link.WorkingDirectory=$target;$link.Save()
 }
 $linkoHome=Join-Path $env:LOCALAPPDATA 'LinkoRemote'
 New-Item -ItemType Directory -Path $linkoHome -Force|Out-Null
 # Upgrade only this independent preview instance, never the 0.7 agent.
 $sessionPath=Join-Path $linkoHome 'shell-session.json'
 if(Test-Path -LiteralPath $sessionPath){
  $prior=$null
  try{
   $prior=Get-Content -LiteralPath $sessionPath -Raw|ConvertFrom-Json
   $priorAddress=[Uri]$prior.url
   if($priorAddress.Host -eq '127.0.0.1' -and $priorAddress.Scheme -eq 'http'){
    $headers=@{'X-Linko-Session'=$priorAddress.Fragment.Substring(1)}
    Invoke-WebRequest -UseBasicParsing -Method POST -Uri ($priorAddress.GetLeftPart([UriPartial]::Authority)+'/api/shutdown') -Headers $headers -ContentType 'application/json' -Body '{}' -TimeoutSec 5|Out-Null
   }
  }catch{}
  if($prior){
   for($attempt=0;$attempt -lt 40;$attempt++){
    if(-not (Get-Process -Id $prior.pid -ErrorAction SilentlyContinue)){break}
    Start-Sleep -Milliseconds 500
   }
   if(Get-Process -Id $prior.pid -ErrorAction SilentlyContinue){throw '旧预览实例尚未退出，请关闭邻控统一预览后重试。'}
  }
 }
 Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $target -RedirectStandardError (Join-Path $linkoHome 'startup.err')|Out-Null
 $ready=$false
 for($attempt=0;$attempt -lt 30;$attempt++){
  Start-Sleep -Milliseconds 500
  try{
   $session=Get-Content -LiteralPath (Join-Path $linkoHome 'shell-session.json') -Raw|ConvertFrom-Json
   $address=[Uri]$session.url
   if($address.Host -ne '127.0.0.1' -or $address.Scheme -ne 'http'){throw 'Invalid local session'}
   $headers=@{'X-Linko-Session'=$address.Fragment.Substring(1)}
   $check=Invoke-WebRequest -UseBasicParsing -Method POST -Uri ($address.GetLeftPart([UriPartial]::Authority)+'/api/state') -Headers $headers -ContentType 'application/json' -Body '{}' -TimeoutSec 2
   if($check.StatusCode -eq 200){$ready=$true;break}
  }catch{}
 }
 if(-not $ready){throw ('邻控本地服务没有正常启动，错误记录位于：'+(Join-Path $linkoHome 'startup.err'))}
}catch{
 [Windows.Forms.MessageBox]::Show($_.Exception.Message,'邻控启动未完成')|Out-Null
 exit 1
}
