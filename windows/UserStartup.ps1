# Current-user login startup. Keep one verified target, even if the Startup folder is unavailable.
$script:runKey='HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$script:runName='LanPilot'

function Get-StartupLinkPath {
 $folder=[Environment]::GetFolderPath('Startup')
 if(-not [string]::IsNullOrWhiteSpace($folder)){return (Join-Path $folder 'LanPilot.lnk')}
}

function Get-StartupLinkTarget([string]$LinkPath) {
 if(-not $LinkPath -or -not (Test-Path -LiteralPath $LinkPath -PathType Leaf)){return $null}
 $shell=New-Object -ComObject WScript.Shell
 return $shell.CreateShortcut($LinkPath)
}

function Set-AppLink([string]$LinkPath,[string]$Exe,[string]$Arguments) {
 if(-not $LinkPath){throw 'The shortcut folder is unavailable.'}
 $shell=New-Object -ComObject WScript.Shell
 $link=$shell.CreateShortcut($LinkPath);$link.TargetPath=$Exe
 $link.WorkingDirectory=[IO.Path]::GetDirectoryName($Exe);$link.Arguments=$Arguments;$link.Save()
 $actual=Get-StartupLinkTarget $LinkPath
 if(-not $actual -or $actual.TargetPath -ne $Exe -or [string]$actual.Arguments -ne $Arguments){
  throw 'The new shortcut could not be verified.'
 }
}

function Get-RunCommand {
 if(Test-Path -LiteralPath $script:runKey){
  return (Get-Item -LiteralPath $script:runKey).GetValue($script:runName,$null)
 }
}

function Get-UserStartup([string]$Exe) {
 $command='"'+$Exe+'" --hidden'
 if((Get-RunCommand) -eq $command){return 'HKCU Run'}
 try{
  $link=Get-StartupLinkTarget (Get-StartupLinkPath)
  if($link -and $link.TargetPath -eq $Exe -and $link.Arguments -eq '--hidden'){return 'Startup shortcut'}
 }catch{}
 return $null
}

function Test-StartupDisabled {
 foreach($branch in @('HKCU:','HKLM:')){
  $policy=$branch+'\Software\Microsoft\Windows\CurrentVersion\Policies\Explorer'
  if(Test-Path -LiteralPath $policy){
   if((Get-Item -LiteralPath $policy).GetValue('DisableCurrentUserRun',0) -eq 1){return $true}
  }
  foreach($pair in @(@('Run',$script:runName),@('StartupFolder','LanPilot.lnk'))){
   $approved=$branch+'\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\'+$pair[0]
   if(Test-Path -LiteralPath $approved){
    $bytes=(Get-Item -LiteralPath $approved).GetValue($pair[1],$null)
    if($bytes -is [byte[]] -and $bytes.Length -ge 4 -and ($bytes[0] -band 1)){return $true}
   }
  }
 }
 return $false
}

function Set-RunStartup([string]$Exe) {
 if(Test-StartupDisabled){throw 'Login startup disabled by Windows settings or policy.'}
 $command='"'+$Exe+'" --hidden'
 if(-not (Test-Path -LiteralPath $script:runKey)){New-Item -Path $script:runKey -Force | Out-Null}
 New-ItemProperty -LiteralPath $script:runKey -Name $script:runName -Value $command -PropertyType String -Force | Out-Null
 if((Get-RunCommand) -ne $command){throw 'Current-user Run entry was not persisted.'}
 return 'HKCU Run'
}

function Set-UserStartup([string]$Exe) {
 if(Test-StartupDisabled){throw 'Login startup disabled by Windows settings or policy.'}
 $path=Get-StartupLinkPath
 try{
  Set-AppLink $path $Exe '--hidden'
 }catch{return (Set-RunStartup $Exe)}
 if(Get-RunCommand){Remove-ItemProperty -LiteralPath $script:runKey -Name $script:runName}
 return 'Startup shortcut'
}

function Confirm-UserStartup([string]$Exe) {
 if(Test-StartupDisabled){throw 'Login startup disabled by Windows settings or policy.'}
 $actual=Get-UserStartup $Exe
 if($actual){return $actual}
 # Folder redirection or sync can drop a shortcut shortly after Save returned success.
 return (Set-RunStartup $Exe)
}

function Disable-UserStartup([string]$Exe) {
 $path=Get-StartupLinkPath
 $link=Get-StartupLinkTarget $path
 if($link -and $link.TargetPath -eq $Exe){Remove-Item -LiteralPath $path}
 if((Get-RunCommand) -eq ('"'+$Exe+'" --hidden')){Remove-ItemProperty -LiteralPath $script:runKey -Name $script:runName}
}
