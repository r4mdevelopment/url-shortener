param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$certDir = Join-Path $ProjectRoot "local-certs"
$crtPath = Join-Path $certDir "localhost.crt"
$keyPath = Join-Path $certDir "localhost.key"
$generator = Join-Path $PSScriptRoot "generate_localhost_cert.py"

if ($Force -or -not (Test-Path -LiteralPath $crtPath) -or -not (Test-Path -LiteralPath $keyPath)) {
    python $generator --output-dir $certDir --common-name localhost | Out-Null
}

$currentThumbprint = $null
try {
    $existingCert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($crtPath)
    $currentThumbprint = $existingCert.Thumbprint
} catch {
    throw "Failed to read certificate at $crtPath"
}

$alreadyTrusted = Get-ChildItem Cert:\CurrentUser\Root | Where-Object { $_.Thumbprint -eq $currentThumbprint }
if (-not $alreadyTrusted) {
    Import-Certificate -FilePath $crtPath -CertStoreLocation Cert:\CurrentUser\Root | Out-Null
}

Write-Host "HTTPS certificate is ready:"
Write-Host "  CRT: $crtPath"
Write-Host "  KEY: $keyPath"
Write-Host "  Thumbprint: $currentThumbprint"
Write-Host ""
Write-Host "Now you can start Docker and open https://localhost:8443"
