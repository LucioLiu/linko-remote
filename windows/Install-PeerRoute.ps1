# The controller exports this script with the selected device's endpoint and certificate pin.
param([int]$Port=28767)
$ErrorActionPreference='Stop'
if($Port -lt 1024 -or $Port -gt 65535){throw 'Invalid local TCP port'}
if([Environment]::Is64BitOperatingSystem -eq $false){throw 'This transport requires 64-bit Windows'}
$base=Join-Path $env:LOCALAPPDATA 'LanPilot\peer'
$deviceRoot=Join-Path $env:LOCALAPPDATA 'CodexRemoteDesktop'
if(-not (Test-Path (Join-Path $deviceRoot 'relay-pairing.bin'))){throw 'Complete device pairing first'}
Add-Type -AssemblyName System.Security
$rsa=New-Object Security.Cryptography.RSACryptoServiceProvider
$rsa.PersistKeyInCsp=$false
try{
 $sealedKey=[IO.File]::ReadAllBytes((Join-Path $deviceRoot 'pair-key.dpapi'))
 $rsa.ImportCspBlob([Security.Cryptography.ProtectedData]::Unprotect($sealedKey,$null,[Security.Cryptography.DataProtectionScope]::CurrentUser))
 $pairing=[IO.File]::ReadAllBytes((Join-Path $deviceRoot 'relay-pairing.bin'))
 $paired=[Text.Encoding]::UTF8.GetString($rsa.Decrypt($pairing,$true)) | ConvertFrom-Json
 if($paired.pin -ne '@PIN@'){throw 'This route belongs to another device'}
}finally{$rsa.Dispose()}
New-Item -ItemType Directory -Force $base | Out-Null
$exe=Join-Path $base 'dumbpipe.exe'
$startup=Join-Path ([Environment]::GetFolderPath('Startup')) 'LanPilot Peer.lnk'
if(-not (Test-Path $exe) -or (Get-FileHash $exe).Hash -ne '@EXE_SHA@'){
 $zip=Join-Path $base ('dumbpipe-'+[guid]::NewGuid()+'.zip')
 $stage=Join-Path $base ('unpack-'+[guid]::NewGuid())
 try{
  & curl.exe --fail --location --max-time 120 --silent --show-error --output $zip 'https://github.com/n0-computer/dumbpipe/releases/download/v@VERSION@/dumbpipe-v@VERSION@-windows-x86_64.zip'
  if($LASTEXITCODE -ne 0 -or (Get-FileHash $zip).Hash -ne '@ZIP_SHA@'){throw 'Transport package hash mismatch'}
  Expand-Archive -LiteralPath $zip -DestinationPath $stage
  $binary=Join-Path $stage 'dumbpipe.exe'
  if((Get-FileHash $binary).Hash -ne '@EXE_SHA@'){throw 'Transport executable hash mismatch'}
  Copy-Item -LiteralPath $binary -Destination $exe
 }finally{
  Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath $stage -Force -Recurse -ErrorAction SilentlyContinue
 }
}
$argument="connect-tcp --addr 127.0.0.1:$Port --custom-alpn @ALPN@ @TICKET@"
$listener=Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
$process=$null
if($listener){
 $owner=Get-CimInstance Win32_Process -Filter ('ProcessId='+$listener.OwningProcess)
 if(-not $owner -or $owner.Name -ne 'dumbpipe.exe' -or $owner.CommandLine -notlike '*@ALPN@*' -or (Get-FileHash $owner.ExecutablePath).Hash -ne '@EXE_SHA@'){throw 'Local port already in use'}
}else{
 $process=Start-Process -WindowStyle Hidden -FilePath $exe -ArgumentList $argument -PassThru
}
if(-not ('LanPilotPeerProbe' -as [type])){
 Add-Type -TypeDefinition @'
using System;
using System.Net.Security;
using System.Net.Sockets;
using System.Security.Cryptography;
public static class LanPilotPeerProbe {
    public static bool Verify(int port, string pin) {
        using (var tcp = new TcpClient()) {
            tcp.ReceiveTimeout = 12000;
            tcp.SendTimeout = 12000;
            tcp.Connect("127.0.0.1", port);
            using (var tls = new SslStream(tcp.GetStream(), false, (sender, cert, chain, errors) => {
                using (var sha = SHA256.Create()) {
                    return cert != null && String.Equals(
                        BitConverter.ToString(sha.ComputeHash(cert.GetRawCertData())).Replace("-", ""),
                        pin, StringComparison.OrdinalIgnoreCase);
                }
            })) {
                tls.AuthenticateAsClient("127.0.0.1");
                return tls.IsAuthenticated;
            }
        }
    }
}
'@
}
try{
 $certSeen=$false
 for($i=0;$i -lt 3 -and -not $certSeen;$i++){
  Start-Sleep -Seconds 3
  if($process -and $process.HasExited){throw 'Peer connector exited'}
  try{$certSeen=[LanPilotPeerProbe]::Verify($Port,'@PIN@')}catch{}
 }
 if(-not $certSeen){throw 'Paired controller unreachable'}
 $route=Join-Path $deviceRoot 'relay-route.json'
 $temp=Join-Path $deviceRoot ('peer-route-'+[guid]::NewGuid()+'.tmp')
 [IO.File]::WriteAllText($temp,(@{url="https://127.0.0.1:$Port"} | ConvertTo-Json -Compress))
 if(Test-Path $route){[IO.File]::Replace($temp,$route,$route+'.before-peer')}else{[IO.File]::Move($temp,$route)}
 $shell=New-Object -ComObject WScript.Shell
 $link=$shell.CreateShortcut($startup)
 $link.TargetPath=$env:SystemRoot+'\System32\wscript.exe'
 $hiddenStarter=Join-Path $base 'start-peer.vbs'
 $line='CreateObject("WScript.Shell").Run Chr(34) & "'+$exe.Replace('"','""')+'" & Chr(34) & " '+$argument+'", 0, False'
 [IO.File]::WriteAllText($hiddenStarter,$line,[Text.Encoding]::Unicode)
 $link.Arguments='//B //Nologo "'+$hiddenStarter+'"'
 $link.WorkingDirectory=$base
 $link.Save()
 Write-Output "Paired controller certificate verified. Worker route: https://127.0.0.1:$Port"
}catch{
 if($process -and -not $process.HasExited){$process.Kill()}
 throw
}
