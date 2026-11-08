# Run with Windows PowerShell 5.1, or pwsh for a portable transport check.
$ErrorActionPreference = 'Stop'
$relaySource = Join-Path (Split-Path $PSScriptRoot -Parent) 'windows/Relay.cs'
if ($PSVersionTable.PSEdition -eq 'Core') {
    Add-Type -Path $relaySource -IgnoreWarnings
} else {
    Add-Type -Path $relaySource
}

$paired = 'https://192.0.2.11:18769'
$wan = 'https://relay.example:443'
$lan = 'https://192.0.2.12:18769'
if ([DesktopRelay]::NormalizeEndpoint('https://[fd00::1]:24321/') -ne 'https://[fd00::1]:24321') {
    throw 'Invalid IPv6 normalization'
}

$previous = ''
foreach ($expected in @($lan, 'https://relay.example', $paired, $lan)) {
    $actual = [DesktopRelay]::NextEndpoint($paired, $wan, $lan, $previous)
    if ($actual -ne $expected) { throw "Expected $expected, received $actual" }
    $previous = $actual
}
if ([DesktopRelay]::NextEndpoint($paired, $wan, '', '') -ne 'https://relay.example') {
    throw 'A newly started off-LAN device should prefer the configured WAN route'
}
if ([DesktopRelay]::NextEndpoint($paired, '', '', $paired) -ne $paired) {
    throw 'Legacy pairing without an overlay route must reconnect to its original endpoint'
}
foreach ($bad in @('http://host', 'https://user@host', 'https://host/path', 'https://host?x',
                    'https://host#x', 'https://host:0', 'https://host:65536')) {
    $rejected = $false
    try { [void][DesktopRelay]::NormalizeEndpoint($bad) } catch { $rejected = $true }
    if (-not $rejected) { throw "Accepted invalid endpoint $bad" }
}
Write-Output 'LAN/WAN route tests passed.'
