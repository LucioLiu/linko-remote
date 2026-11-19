param([switch]$StartHidden,[string]$RelayConfig=(Join-Path $env:LOCALAPPDATA 'CodexRemoteDesktop\relay-pairing.bin'))
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Windows.Forms,System.Drawing
. (Join-Path $PSScriptRoot 'UserStartup.ps1')
. (Join-Path $PSScriptRoot 'WorkerRunspace.ps1')
# A legacy panel may run as administrator; the worker's existing file lock still excludes concurrent control.
$userSid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$isolated=$env:LANPILOT_ISOLATED -match '^[0-9a-f]{32}$' -and $env:LOCALAPPDATA -eq (Join-Path ([IO.Path]::GetTempPath()) ('LanPilot-Invite-QA-'+$env:LANPILOT_ISOLATED))
$panelKey='Local\LanPilotPanel_v5_'+$userSid
if($isolated){$panelKey+='_'+$env:LANPILOT_ISOLATED}
$mutex=New-Object Threading.Mutex($false,$panelKey)
if(-not $mutex.WaitOne(0)){exit}
$script:worker=$null;$script:control=$true;$script:stopping=$false;$script:restart=$false;$script:retryAt=$null
$root=Join-Path $env:LOCALAPPDATA 'CodexRemoteDesktop'
$form=New-Object Windows.Forms.Form
$form.Text='邻控 · Linko Remote';$form.Size=New-Object Drawing.Size(620,480);$form.StartPosition='CenterScreen';$form.Font=New-Object Drawing.Font('Microsoft YaHei UI',11);$form.BackColor=[Drawing.Color]::White;$form.FormBorderStyle='FixedDialog';$form.MaximizeBox=$false
$title=New-Object Windows.Forms.Label;$title.Text='邻控';$title.Font=New-Object Drawing.Font('Microsoft YaHei UI',26,[Drawing.FontStyle]::Bold);$title.SetBounds(28,20,450,50);$form.Controls.Add($title)
$status=New-Object Windows.Forms.Label;$status.SetBounds(32,170,540,60);$status.Text='准备连接';$form.Controls.Add($status)
$connect=New-Object Windows.Forms.Button;$connect.Text='连接';$connect.SetBounds(30,240,150,42);$form.Controls.Add($connect)
$mode=New-Object Windows.Forms.CheckBox;$mode.Text='允许 AI 键鼠和限定文件作业';$mode.Checked= -not $isolated;$mode.Enabled= -not $isolated;$mode.SetBounds(200,245,350,30);$form.Controls.Add($mode)
$hint=New-Object Windows.Forms.Label;$hint.Text="保持此程序运行，关闭窗口后会驻留托盘。`n控制会作用于这台电脑的当前桌面。";$hint.SetBounds(32,360,550,60);$form.Controls.Add($hint)
$namePath=Join-Path $root 'display-name.json'
$computerName=$env:COMPUTERNAME
try{if(Test-Path -LiteralPath $namePath){$computerName=[string]((Get-Content -LiteralPath $namePath -Raw -Encoding UTF8|ConvertFrom-Json).name)}}catch{}
$nameLabel=New-Object Windows.Forms.Label;$nameLabel.Text=$computerName;$nameLabel.Font=New-Object Drawing.Font('Microsoft YaHei UI',17,[Drawing.FontStyle]::Bold);$nameLabel.SetBounds(32,85,410,40);$form.Controls.Add($nameLabel)
$editName=New-Object Windows.Forms.Button;$editName.Text='编辑名称';$editName.SetBounds(450,85,115,34);$form.Controls.Add($editName)
$editName.Add_Click({
 $dialog=New-Object Windows.Forms.Form;$dialog.Text='编辑这台电脑的名称';$dialog.Size=New-Object Drawing.Size(420,180);$dialog.StartPosition='CenterParent'
 $field=New-Object Windows.Forms.TextBox;$field.SetBounds(20,20,360,30);$field.Text=$nameLabel.Text;$field.MaxLength=100;$dialog.Controls.Add($field)
 $save=New-Object Windows.Forms.Button;$save.Text='保存';$save.SetBounds(270,70,110,32);$save.DialogResult='OK';$dialog.Controls.Add($save);$dialog.AcceptButton=$save
 try{if($dialog.ShowDialog($form) -eq 'OK'){
  $value=$field.Text.Trim();if(-not $value -or $value -match '[\x00-\x1f\x7f]'){throw '请输入有效的电脑名称。'}
  New-Item -ItemType Directory -Force -Path $root|Out-Null
  $tmp=Join-Path $root ([guid]::NewGuid().ToString('N')+'.tmp')
  try{[IO.File]::WriteAllText($tmp,(@{name=$value}|ConvertTo-Json -Compress));if(Test-Path -LiteralPath $namePath){[IO.File]::Replace($tmp,$namePath,$null)}else{[IO.File]::Move($tmp,$namePath)}}finally{if(Test-Path -LiteralPath $tmp){Remove-Item -LiteralPath $tmp}}
  $nameLabel.Text=$value
 }}catch{[Windows.Forms.MessageBox]::Show($_.Exception.Message,'保存失败')|Out-Null}finally{$dialog.Dispose()}
})
$nameHint=New-Object Windows.Forms.Label;$nameHint.Text='本机显示名称 · 控制端列表的备注可在 Mac 单独编辑';$nameHint.ForeColor=[Drawing.Color]::DimGray;$nameHint.SetBounds(32,130,550,28);$form.Controls.Add($nameHint)
$exe=Join-Path $PSScriptRoot 'LanPilot.exe'
$autostart=New-Object Windows.Forms.CheckBox;$autostart.Text='登录 Windows 后自动连接';if($isolated){$autostart.Enabled=$false}else{$autostart.Checked=[bool](Get-UserStartup $exe)};$autostart.SetBounds(32,302,330,30);$form.Controls.Add($autostart)
$script:updatingStartup=$false
$autostart.Add_CheckedChanged({
 if($script:updatingStartup){return}
 try{if($autostart.Checked){[void](Set-UserStartup $exe)}else{Disable-UserStartup $exe}}
 catch{
  $script:updatingStartup=$true
  try{$autostart.Checked=[bool](Get-UserStartup $exe)}finally{$script:updatingStartup=$false}
  [Windows.Forms.MessageBox]::Show($_.Exception.Message,'登录自启设置失败')|Out-Null
 }
})
$settings=New-Object Windows.Forms.Button;$settings.Text='远程地址…';$settings.SetBounds(420,298,145,36);$form.Controls.Add($settings)
$settings.Add_Click({
 $dialog=New-Object Windows.Forms.Form
 $dialog.Text='远程连接';$dialog.Size=New-Object Drawing.Size(475,190);$dialog.StartPosition='CenterParent'
 $label=New-Object Windows.Forms.Label;$label.SetBounds(15,15,435,42);$label.Text='输入此设备的 HTTPS 中继地址。留空时使用原配对地址和局域网发现。';$dialog.Controls.Add($label)
 $field=New-Object Windows.Forms.TextBox;$field.SetBounds(15,62,425,25);$dialog.Controls.Add($field)
 $path=Join-Path $root 'relay-route.json'
 try{
  if(Test-Path -LiteralPath $path){$field.Text=(Get-Content -LiteralPath $path -Raw | ConvertFrom-Json).url}
  $save=New-Object Windows.Forms.Button;$save.Text='保存';$save.DialogResult='OK';$save.SetBounds(340,101,100,30);$dialog.Controls.Add($save);$dialog.AcceptButton=$save
  if($dialog.ShowDialog($form) -ne 'OK'){return}
  $value=$field.Text.Trim()
  if($value){
   if(-not ('DesktopRelay' -as [type])){Add-Type -Path (Join-Path $PSScriptRoot 'Relay.cs')}
   $value=[DesktopRelay]::NormalizeEndpoint($value)
   $tmp=Join-Path $root ([guid]::NewGuid().ToString('N')+'.tmp')
   try{
    [IO.File]::WriteAllText($tmp,(@{url=$value}|ConvertTo-Json -Compress))
    if([IO.File]::Exists($path)){[IO.File]::Replace($tmp,$path,$null)}else{[IO.File]::Move($tmp,$path)}
   }finally{if(Test-Path -LiteralPath $tmp){Remove-Item -LiteralPath $tmp}}
  }elseif(Test-Path -LiteralPath $path){Remove-Item -LiteralPath $path}
  [Windows.Forms.MessageBox]::Show('已保存，将在重新连接时使用。','邻控')|Out-Null
 }catch{[Windows.Forms.MessageBox]::Show($_.Exception.Message,'保存失败')|Out-Null}
 finally{$dialog.Dispose()}
})
$tray=New-Object Windows.Forms.NotifyIcon;$tray.Icon=[Drawing.SystemIcons]::Application;$tray.Text='邻控';$tray.Visible=$true
$menu=New-Object Windows.Forms.ContextMenuStrip
$open=$menu.Items.Add('打开邻控');$open.Add_Click({$form.Show();$form.Activate()})
$quit=$menu.Items.Add('退出并停止控制');$quit.Add_Click({$script:stopping=$true;$form.Close()})
$tray.ContextMenuStrip=$menu;$tray.Add_DoubleClick({$form.Show();$form.Activate()})
function Stop-OwnedWorker {
 if($script:worker){Request-DesktopWorkerStop $script:worker;$connect.Enabled=$false;$status.Text='正在断开连接…'}
}
function Start-OwnedWorker {
 $script:retryAt=$null
 if(-not (Test-Path -LiteralPath $RelayConfig)){$status.Text='尚未配对';return}
 if($script:worker){return}
 try{
  $params=@{RelayConfig=$RelayConfig;TargetGuard=$true;Persistent=$true;LanDiscovery=(-not $isolated);WanOnly=$isolated;AllowInput=($mode.Checked -and -not $isolated)}
  $script:worker=New-DesktopWorker (Join-Path $PSScriptRoot 'Start-Desktop.ps1') $params
  $status.Text='正在连接控制端…';$connect.Text='断开';$connect.Enabled=$true
 }catch{$status.Text='无法启动连接：'+$_.Exception.Message;$connect.Text='连接';$connect.Enabled=$true}
}
$connect.Add_Click({if($script:retryAt){$script:retryAt=$null;$status.Text='已断开';$connect.Text='连接';return};if($script:worker){$script:restart=$false;Stop-OwnedWorker}else{Start-OwnedWorker}})
$mode.Add_CheckedChanged({if($script:worker){$script:restart=$true;Stop-OwnedWorker}})
$form.Add_FormClosing({param($sender,$e) if(-not $script:stopping -and $e.CloseReason -eq 'UserClosing'){$e.Cancel=$true;$form.Hide()}})
$timer=New-Object Windows.Forms.Timer;$timer.Interval=2000
$timer.Add_Tick({
 $worker=$script:worker
 if(-not $worker){
  if($script:retryAt -and -not $script:stopping -and [DateTime]::UtcNow -ge $script:retryAt){Start-OwnedWorker}
  return
 }
 if($worker.Invocation.IsCompleted){
  $requested=$worker.StopEvent.WaitOne(0)
  try{Close-DesktopWorker $worker;$status.Text='已断开'}
  catch{if($requested){$status.Text='已断开'}else{$status.Text='连接程序已停止：'+$_.Exception.GetBaseException().Message}}
  $script:worker=$null;$tray.Text='邻控';$connect.Text='连接';$connect.Enabled=$true
  if(-not $requested -and -not $script:stopping){
   $retryPath=Join-Path $root 'transport-restart.json'
   try{
    $reason=Get-Content -LiteralPath $retryPath -Raw -ErrorAction Stop|ConvertFrom-Json
    if($reason.generation -ceq $worker.Generation -and $reason.phase -in @('poll','result')){
     $script:retryAt=[DateTime]::UtcNow.AddSeconds(5);$status.Text='网络中断，正在重连…';$connect.Text='取消重连'
    }
   }catch{}
  }
  if($script:restart -and -not $script:stopping){$script:restart=$false;Start-OwnedWorker}
  return
 }
 if($worker.StopEvent.WaitOne(0)){return}
 $path=Join-Path $root 'online.json'
 try{
  $live=Get-Content -LiteralPath $path -Raw|ConvertFrom-Json
  $age=([DateTime]::UtcNow-([DateTimeOffset]$live.last_seen).UtcDateTime).TotalSeconds
  if($live.pid -eq $PID -and $live.session -ceq $worker.Generation -and $age -ge 0 -and $age -lt 40){
   $status.Text='已连接 · '+$(if($mode.Checked){'可接受 AI 操作'}else{'仅查看'});$tray.Text='邻控 · 已连接'
  }else{$status.Text='等待连接 · 换网后自动寻找控制端'}
 }catch{$status.Text='正在连接…'}
})
$form.Add_Shown({if($StartHidden){$form.Hide()};Start-OwnedWorker;$timer.Start()})
try{[Windows.Forms.Application]::Run($form)}finally{
 $timer.Stop()
 try{if($script:worker){Close-DesktopWorker $script:worker}}
 finally{$tray.Dispose();$mutex.ReleaseMutex();$mutex.Dispose()}
}
