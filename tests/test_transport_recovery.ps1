$ErrorActionPreference='Stop'
$source=Join-Path $PSScriptRoot '../windows/Relay.cs'
if($PSVersionTable.PSEdition -eq 'Core'){Add-Type -Path $source -IgnoreWarnings}else{Add-Type -Path $source}
function Check($e,$expected){
 $wrapped=[InvalidOperationException]::new('call wrapper',$e)
 if([DesktopRelay]::IsTransient($wrapped) -ne $expected){throw ('Unexpected decision: '+$e.GetType().Name)}
}
Check ([IO.IOException]::new('stream closed')) $true
Check ([Net.Sockets.SocketException]::new(10054)) $true
Check ([TimeoutException]::new()) $true
Check ([Net.WebException]::new('offline',[Net.WebExceptionStatus]::ConnectFailure)) $true
Check ([Net.WebException]::new('pin mismatch',[Net.WebExceptionStatus]::TrustFailure)) $false
Check ([Net.WebException]::new('protocol',[Net.WebExceptionStatus]::ProtocolError)) $false
Check ([IO.IOException]::new('stream',[Security.Authentication.AuthenticationException]::new('identity'))) $false
Check ([UnauthorizedAccessException]::new('local policy')) $false
Check ([InvalidOperationException]::new('unknown')) $false
Write-Output 'Transport failure classifications passed.'
