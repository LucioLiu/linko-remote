$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Windows.Forms
function Confirm-InstalledPayload($Directory,$Manifest){
 foreach($entry in $Manifest.PSObject.Properties){
  if($entry.Name.StartsWith('code/') -or $entry.Name.StartsWith('runtime/')){
   $file=Join-Path $Directory $entry.Name
   if(-not (Test-Path -LiteralPath $file -PathType Leaf) -or (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -ine $entry.Value){
    throw ('已安装文件校验未通过：'+$entry.Name)
   }
  }
 }
}

function Install-Payload($Source,$Target,$Manifest){
 if(-not (Test-Path -LiteralPath $Target)){
  $staging=$Target+'.partial-'+[Guid]::NewGuid().ToString('N')
  try{
   New-Item -ItemType Directory -Path $staging -Force|Out-Null
   foreach($name in @('runtime','code')){Copy-Item -LiteralPath (Join-Path $Source $name) -Destination $staging -Recurse}
   Confirm-InstalledPayload $staging $Manifest
   try{[IO.Directory]::Move($staging,$Target)}catch [IO.IOException]{
    # A concurrent installer may already have moved a complete copy into place.
    if(-not (Test-Path -LiteralPath $Target -PathType Container)){throw}
   }
  }finally{
   if(Test-Path -LiteralPath $staging){Remove-Item -LiteralPath $staging -Recurse -Force}
  }
 }
 Confirm-InstalledPayload $Target $Manifest
}
$installLock=$null
try{
 $manifest=Get-Content -LiteralPath (Join-Path $PSScriptRoot 'checksums.json') -Raw|ConvertFrom-Json
 foreach($entry in $manifest.PSObject.Properties){
  $file=Join-Path $PSScriptRoot $entry.Name
  if((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -ine $entry.Value){throw ('文件校验失败：'+$entry.Name)}
 }
 $linkoHome=Join-Path $env:LOCALAPPDATA 'LinkoRemote'
 New-Item -ItemType Directory -Path $linkoHome -Force|Out-Null
 try{
  $installLock=[IO.File]::Open((Join-Path $linkoHome 'install.lock'),[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
 }catch [IO.IOException]{throw '无法取得邻控安装锁，请确认其他安装窗口已关闭后重试。'}
 $base=Join-Path $linkoHome 'app'
 $build=(Get-FileHash -LiteralPath (Join-Path $PSScriptRoot 'checksums.json')).Hash.Substring(0,10).ToLowerInvariant()
 $release=Get-Content -LiteralPath (Join-Path $PSScriptRoot 'code\release.json') -Raw|ConvertFrom-Json
 if($release.version -notmatch '^\d+\.\d+\.\d+(-(preview|beta|rc)\.\d+)?$'){throw '安装包版本信息无效'}
 $target=Join-Path $base ($release.version+'-'+$build)
 Install-Payload $PSScriptRoot $target $manifest
 $python=Join-Path $target 'runtime\pythonw.exe'
 $arguments='-m lanpilot.peer_client --open-browser'
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
   if(Get-Process -Id $prior.pid -ErrorAction SilentlyContinue){throw '邻控尚未退出，请退出当前运行的邻控后重试。'}
  }
 }
 $started=Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $target -RedirectStandardError (Join-Path $linkoHome 'startup.err') -PassThru
 $ready=$false
 for($attempt=0;$attempt -lt 30;$attempt++){
  Start-Sleep -Milliseconds 500
  try{
   $session=Get-Content -LiteralPath (Join-Path $linkoHome 'shell-session.json') -Raw|ConvertFrom-Json
   if($session.pid -ne $started.Id){continue}
   $address=[Uri]$session.url
   if($address.Host -ne '127.0.0.1' -or $address.Scheme -ne 'http'){throw 'Invalid local session'}
   $headers=@{'X-Linko-Session'=$address.Fragment.Substring(1)}
   $check=Invoke-WebRequest -UseBasicParsing -Method POST -Uri ($address.GetLeftPart([UriPartial]::Authority)+'/api/state') -Headers $headers -ContentType 'application/json' -Body '{}' -TimeoutSec 2
   if($check.StatusCode -eq 200){$ready=$true;break}
  }catch{}
 }
 if(-not $ready){throw ('邻控本地服务没有正常启动，错误记录位于：'+(Join-Path $linkoHome 'startup.err'))}
 $shell=New-Object -ComObject WScript.Shell
 foreach($folder in @('Desktop','Programs')){
  $shortcutDirectory=[Environment]::GetFolderPath($folder)
  $path=Join-Path $shortcutDirectory '邻控.lnk'
  $link=$shell.CreateShortcut($path);$link.TargetPath=$python;$link.Arguments=$arguments;$link.WorkingDirectory=$target;$link.Save()
  $previousPath=Join-Path $shortcutDirectory '邻控统一预览.lnk'
  if(Test-Path -LiteralPath $previousPath -PathType Leaf){
   $previousLink=$shell.CreateShortcut($previousPath)
   if($previousLink.TargetPath.StartsWith($base+'\',[StringComparison]::OrdinalIgnoreCase) -and $previousLink.Arguments -eq $arguments){
    Remove-Item -LiteralPath $previousPath
   }
  }
 }
}catch{
 [Windows.Forms.MessageBox]::Show($_.Exception.Message,'邻控启动未完成')|Out-Null
 exit 1
}finally{
 if($installLock){$installLock.Dispose()}
}
