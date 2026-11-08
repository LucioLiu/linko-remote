$ErrorActionPreference='Stop'
Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes,WindowsBase,Accessibility
Add-Type -ReferencedAssemblies @('System.Drawing',[System.Windows.Automation.AutomationElement].Assembly.Location,[System.Windows.Automation.TextPatternIdentifiers].Assembly.Location,[System.Windows.Rect].Assembly.Location,[Accessibility.IAccessible].Assembly.Location) -Path (Join-Path $PSScriptRoot 'Desktop.cs')
function Image-Bytes([int]$x,[int]$y,[int]$width=320) {
 $bitmap=New-Object Drawing.Bitmap($width,240)
 try {
  $g=[Drawing.Graphics]::FromImage($bitmap)
  try {$g.Clear([Drawing.Color]::White)}finally{$g.Dispose()}
  if($x -ge 0){$bitmap.SetPixel($x,$y,[Drawing.Color]::Black)}
  $stream=New-Object IO.MemoryStream
  try {$bitmap.Save($stream,[Drawing.Imaging.ImageFormat]::Png);return ,$stream.ToArray()}finally{$stream.Dispose()}
 }finally{$bitmap.Dispose()}
}
$a=Image-Bytes -1 -1
$outside=Image-Bytes 300 200
$inside=Image-Bytes 100 100
$changedSize=Image-Bytes -1 -1 321
$region=[int[]]@(40,40,160,160)
if(-not [RemoteDesktop]::RegionUnchanged($a,$a,$region)){throw 'IDENTICAL_FAILED'}
if(-not [RemoteDesktop]::RegionUnchanged($a,$outside,$region)){throw 'BACKGROUND_CHANGE_FAILED'}
if([RemoteDesktop]::RegionUnchanged($a,$inside,$region)){throw 'TARGET_CHANGE_ACCEPTED'}
if([RemoteDesktop]::RegionUnchanged($a,$changedSize,$region)){throw 'SIZE_CHANGE_ACCEPTED'}
$rejected=$false
try{[void][RemoteDesktop]::RegionUnchanged($a,$a,[int[]]@(500,500,10,10))}catch{$rejected=$true}
if(-not $rejected){throw 'OUTSIDE_REGION_ACCEPTED'}
if(-not [RemoteDesktop]::RegionUnchanged($a,$inside,$region,[int[]]@(100,100,2,20))){throw 'CARET_BLINK_REJECTED'}
if([RemoteDesktop]::RegionUnchanged($a,$inside,$region,[int[]]@(101,100,2,20))){throw 'ADJACENT_CHANGE_ACCEPTED'}
$rejected=$false
try{[void][RemoteDesktop]::RegionUnchanged($a,$inside,$region,[int[]]@(100,100,20,20))}catch{$rejected=$true}
if(-not $rejected){throw 'OVERSIZED_CARET_ACCEPTED'}
Write-Output 'GUARD_TESTS_PASS: identical/background accepted; target/size/outside rejected. No desktop input.'
Write-Output 'CARET_TESTS_PASS: narrow caret accepted; adjacent change and oversized caret rejected.'
