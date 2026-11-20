<#
.SYNOPSIS
    Draait de live contracttests tegen de echte SolarEdge API (alleen lezen).
.DESCRIPTION
    Vraagt e-mailadres en wachtwoord (afgeschermd), zet ze alleen voor deze run in omgevingsvariabelen
    en verwijdert het wachtwoord daarna weer. Accounts met MFA worden niet ondersteund.
.EXAMPLE
    .\scripts\run-live-tests.ps1
.EXAMPLE
    .\scripts\run-live-tests.ps1 -Username jij@example.com -Strict
#>
param(
    [string]$Username,
    [long]$SiteId,
    [switch]$Strict
)

$root = Split-Path -Parent $PSScriptRoot
if (-not $Username) { $Username = Read-Host 'E-mailadres' }
$secure = Read-Host 'Wachtwoord' -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)

try {
    $env:SOLAREDGE_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    $env:SOLAREDGE_USERNAME = $Username
    $env:SOLAREDGE_LIVE_TESTS = '1'
    if ($SiteId) { $env:SOLAREDGE_SITE_ID = "$SiteId" }
    if ($Strict) { $env:SOLAREDGE_STRICT = '1' }

    & "$root\.venv\Scripts\python.exe" -m pytest "$root\tests\live" -p no:homeassistant -p no:cacheprovider -s -q -rA
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    Remove-Item Env:SOLAREDGE_PASSWORD, Env:SOLAREDGE_USERNAME, Env:SOLAREDGE_LIVE_TESTS, Env:SOLAREDGE_SITE_ID, Env:SOLAREDGE_STRICT -ErrorAction SilentlyContinue
}
