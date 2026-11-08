$ErrorActionPreference='Stop'
Add-Type @'
using System.Runtime.InteropServices;
public static class FixtureDpi {
 [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
}
'@
[void][FixtureDpi]::SetProcessDPIAware()
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$out=Join-Path $PSScriptRoot 'harness-result.json'
$script:clicks=0
$background=New-Object Windows.Forms.Form
$background.Text='Codex Test Background Animation'
$background.StartPosition='Manual'
$background.Location=New-Object Drawing.Point(0,0)
$background.Size=New-Object Drawing.Size(1800,1000)
$background.BackColor=[Drawing.Color]::LightSteelBlue
$clock=New-Object Windows.Forms.Label
$clock.Location=New-Object Drawing.Point(10,10)
$clock.Size=New-Object Drawing.Size(500,50)
$clock.Font=New-Object Drawing.Font('Segoe UI',18)
$background.Controls.Add($clock)
$timer=New-Object Windows.Forms.Timer
$timer.Interval=100
$timer.Add_Tick({$clock.Text='Background: '+[DateTime]::Now.ToString('HH:mm:ss.fff')})
$form=New-Object Windows.Forms.Form
$form.Text='Codex Remote Test - disposable fixture'
$form.StartPosition='Manual'
$form.Location=New-Object Drawing.Point(180,130)
$form.AutoScaleMode='None'
$form.Size=New-Object Drawing.Size(1100,850)
$form.Font=New-Object Drawing.Font('Segoe UI',14)
$title=New-Object Windows.Forms.Label
$title.Text='REMOTE CONTROL TEST - no business data'
$title.Location=New-Object Drawing.Point(20,20)
$title.Size=New-Object Drawing.Size(900,40)
$form.Controls.Add($title)
$box=New-Object Windows.Forms.TextBox
$box.Multiline=$true
$box.AcceptsReturn=$true
$box.AcceptsTab=$true
$box.ScrollBars='Vertical'
$box.Location=New-Object Drawing.Point(20,80)
$box.Size=New-Object Drawing.Size(1020,400)
$box.Text='TYPE HERE'
$form.Controls.Add($box)
$button=New-Object Windows.Forms.Button
$button.Text='Click test: 0'
$button.Location=New-Object Drawing.Point(20,500)
$button.Size=New-Object Drawing.Size(320,65)
$button.Add_Click({$script:clicks++;$button.Text='Click test: '+$script:clicks})
$form.Controls.Add($button)
$save=New-Object Windows.Forms.Button
$save.Text='Save test evidence'
$save.Location=New-Object Drawing.Point(370,500)
$save.Size=New-Object Drawing.Size(320,65)
$save.Add_Click({
 @{text=$box.Text;clicks=$script:clicks;time=[DateTime]::UtcNow.ToString('o')} |
  ConvertTo-Json -Compress | Set-Content -LiteralPath $out -Encoding UTF8
 $save.Text='Saved '+[DateTime]::Now.ToString('HH:mm:ss')
})
$form.Controls.Add($save)
$script:changes=0
$changeLabel=New-Object Windows.Forms.Label
$changeLabel.Text='Target unchanged'
$changeLabel.Location=New-Object Drawing.Point(390,600)
$changeLabel.Size=New-Object Drawing.Size(600,50)
$form.Controls.Add($changeLabel)
$arm=New-Object Windows.Forms.Button
$arm.Text='Change target in 2 seconds'
$arm.Location=New-Object Drawing.Point(20,590)
$arm.Size=New-Object Drawing.Size(350,65)
$changeTimer=New-Object Windows.Forms.Timer
$changeTimer.Interval=2000
$changeTimer.Add_Tick({
 $changeTimer.Stop();$script:changes++
 $changeLabel.Text='TARGET CHANGED '+$script:changes
 $arm.Text='Change target in 2 seconds'
})
$arm.Add_Click({if(-not $changeTimer.Enabled){$arm.Text='Armed';$changeTimer.Start()}})
$form.Controls.Add($arm)
$form.Add_Shown({$box.Focus()})
$form.Add_FormClosed({$timer.Stop();$changeTimer.Stop();$background.Close()})
$background.Show()
$timer.Start()
try{[Windows.Forms.Application]::Run($form)}finally{$timer.Dispose();$changeTimer.Dispose();$form.Dispose();$background.Dispose()}
