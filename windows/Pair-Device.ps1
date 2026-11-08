param([string]$PublicKeyPath)
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Security
$root=Join-Path $env:LOCALAPPDATA 'CodexRemoteDesktop'
New-Item -ItemType Directory -Force -Path $root | Out-Null
$key=Join-Path $root 'pair-key.dpapi'
$rsa=New-Object Security.Cryptography.RSACryptoServiceProvider(2048)
$rsa.PersistKeyInCsp=$false
try {
 if(Test-Path -LiteralPath $key) {
  $rsa.ImportCspBlob([Security.Cryptography.ProtectedData]::Unprotect([IO.File]::ReadAllBytes($key),$null,[Security.Cryptography.DataProtectionScope]::CurrentUser))
 } else {
  $protected=[Security.Cryptography.ProtectedData]::Protect($rsa.ExportCspBlob($true),$null,[Security.Cryptography.DataProtectionScope]::CurrentUser)
  $pending=Join-Path $root ([guid]::NewGuid().ToString('N')+'.key')
  try {
   [IO.File]::WriteAllBytes($pending,$protected)
   [IO.File]::Move($pending,$key)
  }finally{if(Test-Path -LiteralPath $pending){Remove-Item -LiteralPath $pending}}
 }
 $public=$rsa.ToXmlString($false)
 if($PublicKeyPath){[IO.File]::WriteAllText($PublicKeyPath,$public,[Text.Encoding]::ASCII)}
 else{$public}
} finally {$rsa.Dispose()}
