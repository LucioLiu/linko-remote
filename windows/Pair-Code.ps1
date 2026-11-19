$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Windows.Forms,System.Drawing
try{
 $meta=Get-Content -LiteralPath (Join-Path $PSScriptRoot 'pairing-endpoint.json') -Raw -Encoding UTF8|ConvertFrom-Json
 if([string]$meta.isolated_test -match '^[0-9a-f]{32}$'){
  $env:LANPILOT_ISOLATED=[string]$meta.isolated_test
  $env:LOCALAPPDATA=Join-Path ([IO.Path]::GetTempPath()) ('LanPilot-Invite-QA-'+$env:LANPILOT_ISOLATED)
 }
 $root=Join-Path $env:LOCALAPPDATA 'CodexRemoteDesktop'
 if(Test-Path -LiteralPath (Join-Path $root 'relay-pairing.bin')){
  $installed=Get-Content -LiteralPath (Join-Path $root 'client.json') -Raw|ConvertFrom-Json
  $exe=[IO.Path]::GetFullPath([string]$installed.exe)
  $prefix=[IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'LanPilot'))+[IO.Path]::DirectorySeparatorChar
  if(-not $exe.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase) -or [IO.Path]::GetFileName($exe) -ne 'LanPilot.exe'){throw 'Invalid installed client path.'}
  Start-Process -FilePath $exe
  return
 }
 $form=New-Object Windows.Forms.Form
 $form.Text='邻控 · Linko Remote';$form.ClientSize=New-Object Drawing.Size(500,340);$form.StartPosition='CenterScreen'
 $form.FormBorderStyle='FixedDialog';$form.MaximizeBox=$false;$form.Font=New-Object Drawing.Font('Microsoft YaHei UI',11)
 $form.BackColor=[Drawing.Color]::White
 $title=New-Object Windows.Forms.Label;$title.Text='用验证码连接';$title.Font=New-Object Drawing.Font('Microsoft YaHei UI',23,[Drawing.FontStyle]::Bold);$title.SetBounds(28,22,440,50);$form.Controls.Add($title)
 $hint=New-Object Windows.Forms.Label;$hint.Text="点击 Mac 邻控的「复制连接码」，然后粘贴到这里。`n支持跨网连接，无需交换配对文件。";$hint.SetBounds(30,85,440,60);$form.Controls.Add($hint)
 $field=New-Object Windows.Forms.TextBox;$field.Font=New-Object Drawing.Font('Consolas',14);$field.SetBounds(30,152,440,50);$field.MaxLength=2048;$form.Controls.Add($field)
 $foot=New-Object Windows.Forms.Label;$foot.Text='连接码 10 分钟有效。专用客户端也可输入 12 位数字。';$foot.SetBounds(30,210,440,45);$foot.ForeColor=[Drawing.Color]::DimGray;$form.Controls.Add($foot)
 $connect=New-Object Windows.Forms.Button;$connect.Text='连接这台电脑';$connect.SetBounds(265,270,205,42);$connect.DialogResult='OK';$form.Controls.Add($connect);$form.AcceptButton=$connect
 while($form.ShowDialog() -eq 'OK'){
  try{
   $value=$field.Text.Trim()
   if($value.StartsWith('linko:')){
    $encoded=$value.Substring(6).Replace('-','+').Replace('_','/')
    if($encoded.Length -gt 2000){throw '连接码太长。'}
    $encoded=$encoded.PadRight($encoded.Length+((4-$encoded.Length%4)%4),'=')
    $decoded=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($encoded))|ConvertFrom-Json
    if($decoded.v -ne 1 -or [string]$decoded.code -notmatch '^[0-9]{12}$' -or [string]$decoded.pin -notmatch '^[a-f0-9]{64}$'){throw '连接码格式不正确。'}
    if(-not ('DesktopRelay' -as [type])){Add-Type -Path (Join-Path $PSScriptRoot 'Relay.cs')}
    $url=[DesktopRelay]::NormalizeEndpoint([string]$decoded.url)
    $updated=@{url=$url;pin=[string]$decoded.pin;controller='Linko Remote'}
    if([string]$meta.isolated_test -match '^[0-9a-f]{32}$'){$updated.isolated_test=[string]$meta.isolated_test}
    [IO.File]::WriteAllText((Join-Path $PSScriptRoot 'pairing-endpoint.json'),($updated|ConvertTo-Json -Compress))
    $code=[string]$decoded.code
    break
   }
   $code=$value -replace '[\s-]',''
   if($code -match '^[0-9]{12}$' -and $meta.url -and $meta.pin){break}
   throw '请从 Mac 邻控复制完整连接码后粘贴。'
  }catch{[Windows.Forms.MessageBox]::Show($_.Exception.Message,'检查连接码')|Out-Null}
 }
 if($form.DialogResult -ne 'OK'){return}
 $form.Dispose()
 & (Join-Path $PSScriptRoot 'Claim-Invitation.ps1') -PairingCode $code
}catch{
 [Windows.Forms.MessageBox]::Show(('连接未完成。请检查验证码是否过期、网络是否正常，再使用同一个安装包重试。'+"`n"+$_.Exception.Message),'邻控')|Out-Null
 exit 1
}
