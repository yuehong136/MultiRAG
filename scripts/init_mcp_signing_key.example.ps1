# Generate one unencrypted ES256 / P-256 signing keypair for MultiRAG on Windows.
#
# This is key material for identity.mcp_issuer, not a TLS certificate. It does
# not create a CSR and does not contact a CA. The private key stays with the
# MultiRAG issuer; consumers obtain only public keys from the JWKS endpoint.
#
# Example:
#   .\scripts\init_mcp_signing_key.example.ps1 `
#     -Kid p3-test-2026-08 `
#     -KeyDir C:\ProgramData\MultiRAG\secrets\p3

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9_-]+$')]
    [ValidateLength(1, 64)]
    [string]$Kid,

    [string]$KeyDir = "$env:ProgramData\MultiRAG\secrets\p3",

    # The Windows account that will run MultiRAG. It receives read access to
    # both PEM files. The default is the account running this script.
    [string]$PrivateKeyReader = "",

    # Prefer a new kid for rotation. Force is intended only for disposable
    # test material and replaces the two files for this exact kid.
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Assert-LastExitCode {
    param([string]$Operation)

    if ($LASTEXITCODE -ne 0) {
        throw "$Operation failed with exit code $LASTEXITCODE"
    }
}

function Assert-NotReparsePoint {
    param([string]$LiteralPath, [string]$Description)

    if (-not (Test-Path -LiteralPath $LiteralPath)) {
        return
    }
    $item = Get-Item -LiteralPath $LiteralPath -Force
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Description must not be a symbolic link or reparse point: $LiteralPath"
    }
}

function ConvertTo-YamlSingleQuoted {
    param([string]$Value)

    return "'" + $Value.Replace("'", "''") + "'"
}

if (-not [System.IO.Path]::IsPathRooted($KeyDir)) {
    throw "-KeyDir must be an absolute path"
}

$openSsl = Get-Command openssl.exe -ErrorAction SilentlyContinue
if ($null -eq $openSsl) {
    $openSsl = Get-Command openssl -ErrorAction SilentlyContinue
}
if ($null -eq $openSsl) {
    throw "OpenSSL is required and must be available on PATH"
}

$currentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
if ([string]::IsNullOrWhiteSpace($PrivateKeyReader)) {
    $PrivateKeyReader = $currentIdentity
}

$KeyDir = [System.IO.Path]::GetFullPath($KeyDir)
$pathRoot = [System.IO.Path]::GetPathRoot($KeyDir).TrimEnd([char[]]"\/")
$trimmedKeyDir = $KeyDir.TrimEnd([char[]]"\/")
if ($trimmedKeyDir -eq $pathRoot) {
    throw "Refusing to change ACLs on a filesystem root"
}
foreach ($broadDirectory in @($env:ProgramData, $env:LOCALAPPDATA, $env:USERPROFILE)) {
    if (-not [string]::IsNullOrWhiteSpace($broadDirectory) -and
        $trimmedKeyDir.Equals($broadDirectory.TrimEnd([char[]]"\/"), [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to change ACLs on a broad system or user directory: $KeyDir"
    }
}

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$repoPrefix = $repoRoot.TrimEnd([char[]]"\/") + [System.IO.Path]::DirectorySeparatorChar
if ($trimmedKeyDir.Equals($repoRoot.TrimEnd([char[]]"\/"), [System.StringComparison]::OrdinalIgnoreCase) -or
    $trimmedKeyDir.StartsWith($repoPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Key directory must be outside the MultiRAG repository"
}

Assert-NotReparsePoint -LiteralPath $KeyDir -Description "Key directory"
if ((Test-Path -LiteralPath $KeyDir) -and -not (Get-Item -LiteralPath $KeyDir -Force).PSIsContainer) {
    throw "Key directory path exists but is not a directory: $KeyDir"
}
$keyDirectoryExists = Test-Path -LiteralPath $KeyDir
if ($keyDirectoryExists) {
    foreach ($entry in @(Get-ChildItem -LiteralPath $KeyDir -Force)) {
        if (($entry.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or
            $entry.Name -notmatch '^[A-Za-z0-9_-]{1,64}-(private|public)\.pem$') {
            throw "Existing key directory contains an unmanaged entry; use a dedicated directory: $($entry.Name)"
        }
    }
}
else {
    New-Item -ItemType Directory -Path $KeyDir -Force | Out-Null
}

# The operator owns the directory. SYSTEM retains recovery access. If a
# distinct service account is selected, it receives directory traversal only;
# the exact PEM files receive read access below.
& icacls.exe $KeyDir /inheritance:r /grant:r "${currentIdentity}:(OI)(CI)F" "SYSTEM:(OI)(CI)F" | Out-Null
Assert-LastExitCode -Operation "Restricting key directory ACL"
if ($PrivateKeyReader -ne $currentIdentity) {
    & icacls.exe $KeyDir /grant:r "${PrivateKeyReader}:(RX)" | Out-Null
    Assert-LastExitCode -Operation "Granting the MultiRAG service account directory access"
}

$privateKey = Join-Path $KeyDir "$Kid-private.pem"
$publicKey = Join-Path $KeyDir "$Kid-public.pem"
foreach ($outputPath in @($privateKey, $publicKey)) {
    Assert-NotReparsePoint -LiteralPath $outputPath -Description "Key output"
    if ((Test-Path -LiteralPath $outputPath) -and -not $Force) {
        throw "$outputPath already exists; use a new kid for rotation or pass -Force"
    }
}

$nonce = [Guid]::NewGuid().ToString("N")
$privateTemp = Join-Path $KeyDir ".$Kid.private.$nonce.tmp"
$publicTemp = Join-Path $KeyDir ".$Kid.public.$nonce.tmp"
$derivedDer = Join-Path $KeyDir ".$Kid.derived.$nonce.tmp"
$publicDer = Join-Path $KeyDir ".$Kid.public-der.$nonce.tmp"
$temporaryFiles = @($privateTemp, $publicTemp, $derivedDer, $publicDer)

try {
    & $openSsl.Source genpkey -algorithm EC -pkeyopt ec_paramgen_curve:prime256v1 -out $privateTemp
    Assert-LastExitCode -Operation "Generating the P-256 private key"

    & $openSsl.Source pkey -in $privateTemp -pubout -out $publicTemp
    Assert-LastExitCode -Operation "Deriving the public key"

    & $openSsl.Source pkey -in $privateTemp -check -noout
    Assert-LastExitCode -Operation "Validating the private key"
    & $openSsl.Source pkey -pubin -in $publicTemp -noout
    Assert-LastExitCode -Operation "Validating the public key"

    & $openSsl.Source pkey -in $privateTemp -pubout -outform DER -out $derivedDer
    Assert-LastExitCode -Operation "Encoding the private key's public component"
    & $openSsl.Source pkey -pubin -in $publicTemp -outform DER -out $publicDer
    Assert-LastExitCode -Operation "Encoding the public key"

    $derivedHash = (Get-FileHash -LiteralPath $derivedDer -Algorithm SHA256).Hash
    $publicHash = (Get-FileHash -LiteralPath $publicDer -Algorithm SHA256).Hash
    if ($derivedHash -ne $publicHash) {
        throw "Generated public key does not match private key"
    }

    # Restrict the temporary PEMs before their atomic rename, so a failure
    # cannot leave a final private path with inherited developer ACLs.
    & icacls.exe $privateTemp /inheritance:r /grant:r "${PrivateKeyReader}:(R)" "SYSTEM:(F)" | Out-Null
    Assert-LastExitCode -Operation "Restricting private key ACL"
    & icacls.exe $publicTemp /inheritance:r /grant:r "${PrivateKeyReader}:(R)" "SYSTEM:(F)" | Out-Null
    Assert-LastExitCode -Operation "Restricting public key ACL"

    Move-Item -LiteralPath $publicTemp -Destination $publicKey -Force
    Move-Item -LiteralPath $privateTemp -Destination $privateKey -Force

    $yamlPrivate = ConvertTo-YamlSingleQuoted -Value $privateKey
    $yamlPublic = ConvertTo-YamlSingleQuoted -Value $publicKey

    Write-Output "Generated ES256 / P-256 signing keypair"
    Write-Output "kid: $Kid"
    Write-Output "private key (MultiRAG only): $privateKey"
    Write-Output "public key (published through JWKS): $publicKey"
    Write-Output "public key SHA-256: $publicHash"
    Write-Output ""
    Write-Output "identity.mcp_issuer.key_provider:"
    Write-Output "  kind: file"
    Write-Output "  active_key_id: '$Kid'"
    Write-Output "  private_key_file: $yamlPrivate"
    Write-Output "  public_key_files:"
    Write-Output "    '$Kid': $yamlPublic"
    Write-Output ""
    Write-Output "Do not commit either PEM file. Do not send the private key to of_mcp."
    Write-Output "See docs/enterprise-identity-mcp/P3_SIGNING_KEYS.md before enabling the issuer."
}
finally {
    foreach ($temporaryFile in $temporaryFiles) {
        if (Test-Path -LiteralPath $temporaryFile) {
            Remove-Item -LiteralPath $temporaryFile -Force
        }
    }
}
