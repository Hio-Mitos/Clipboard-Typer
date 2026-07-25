<#
.SYNOPSIS
    Builds ClipboardTyper.exe with PyInstaller, stages it with the MSIX
    manifest and assets, and packages it into a .msix using the Windows
    SDK's makeappx.exe.

.DESCRIPTION
    Run this from a normal PowerShell window on Windows (not this repo's
    Linux dev sandbox - it needs PyInstaller and the Windows SDK, both of
    which only exist on Windows).

    Prerequisites:
      - Python + this project's requirements.txt installed (pip install -r
        ..\requirements.txt), plus PyInstaller (pip install pyinstaller).
      - Windows SDK installed (comes with Visual Studio, or standalone from
        https://developer.microsoft.com/windows/downloads/windows-sdk/).
        This script looks for makeappx.exe / signtool.exe under the usual
        "Windows Kits\10\bin\<version>\x64" install location; if yours is
        somewhere else, edit $sdkBinRoot below or make sure both tools are
        already on your PATH.
      - AppxManifest.xml's Name/Publisher fields filled in with the values
        Partner Center gives you after you reserve the app name (see
        README.md's MSIX/Store section).

.PARAMETER Version
    Overrides the <Identity Version="..."> in AppxManifest.xml for this
    build, e.g. -Version 1.2.0.0. Optional - if omitted, the manifest's
    existing version is used as-is.

.PARAMETER SignForTesting
    Also creates (or reuses) a local self-signed certificate and signs the
    .msix with it, so you can sideload-install it on your own dev machine
    to test before submitting to Partner Center. Do NOT use this
    certificate for the actual Store submission - Partner Center signs the
    package itself once it's uploaded.

.EXAMPLE
    .\build_msix.ps1
    .\build_msix.ps1 -Version 1.1.0.0 -SignForTesting
#>

param(
    [string]$Version = $null,
    [switch]$SignForTesting
)

$ErrorActionPreference = "Stop"

$packagingDir = $PSScriptRoot
$repoRoot = Split-Path $packagingDir -Parent
$stagingDir = Join-Path $packagingDir "staging"
$distDir = Join-Path $repoRoot "dist"
$outDir = Join-Path $packagingDir "out"

Write-Host "==> Repo root:      $repoRoot"
Write-Host "==> Staging folder: $stagingDir"

# --- 1. Build the EXE with PyInstaller -------------------------------------
Write-Host "`n==> Running PyInstaller..."
Push-Location $repoRoot
try {
    pyinstaller clipboard_typer.spec --noconfirm
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed (exit $LASTEXITCODE)." }
} finally {
    Pop-Location
}

$exePath = Join-Path $distDir "ClipboardTyper.exe"
if (-not (Test-Path $exePath)) {
    throw "Expected build output not found: $exePath"
}

# --- 2. Stage manifest + assets + exe ---------------------------------------
Write-Host "`n==> Staging package contents..."
if (Test-Path $stagingDir) { Remove-Item $stagingDir -Recurse -Force }
New-Item -ItemType Directory -Path $stagingDir | Out-Null

Copy-Item $exePath -Destination $stagingDir
Copy-Item (Join-Path $packagingDir "Assets") -Destination $stagingDir -Recurse

$manifestSrc = Join-Path $packagingDir "AppxManifest.xml"
$manifestDst = Join-Path $stagingDir "AppxManifest.xml"
Copy-Item $manifestSrc -Destination $manifestDst

if ($Version) {
    Write-Host "==> Setting package version to $Version"
    (Get-Content $manifestDst -Raw) -replace 'Version="[\d\.]+"', "Version=""$Version""" |
        Set-Content $manifestDst -Encoding UTF8
}

$manifestContent = Get-Content $manifestDst -Raw
if ($manifestContent -match 'REPLACE ME') {
    Write-Warning "AppxManifest.xml still has 'REPLACE ME' placeholders (Name/Publisher/PublisherDisplayName)."
    Write-Warning "Fill those in from Partner Center > App identity before submitting - the package will still build for local testing."
}

# --- 3. Locate makeappx.exe / signtool.exe ----------------------------------
function Find-SdkTool($toolName) {
    $onPath = Get-Command $toolName -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }

    $sdkBinRoot = "C:\Program Files (x86)\Windows Kits\10\bin"
    if (Test-Path $sdkBinRoot) {
        $found = Get-ChildItem -Path $sdkBinRoot -Recurse -Filter $toolName -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match "\\x64\\" } |
            Sort-Object FullName -Descending |
            Select-Object -First 1
        if ($found) { return $found.FullName }
    }
    return $null
}

$makeappx = Find-SdkTool "makeappx.exe"
if (-not $makeappx) {
    throw "makeappx.exe not found. Install the Windows SDK (https://developer.microsoft.com/windows/downloads/windows-sdk/) or add it to PATH."
}
Write-Host "==> Using makeappx: $makeappx"

# --- 4. Pack the .msix -------------------------------------------------------
if (-not (Test-Path $outDir)) { New-Item -ItemType Directory -Path $outDir | Out-Null }
$msixPath = Join-Path $outDir "ClipboardTyper.msix"

Write-Host "`n==> Packing $msixPath ..."
& $makeappx pack /d $stagingDir /p $msixPath /overwrite
if ($LASTEXITCODE -ne 0) { throw "makeappx failed (exit $LASTEXITCODE)." }

Write-Host "`n==> Package created: $msixPath"

# --- 5. Optional: sign with a local test certificate ------------------------
if ($SignForTesting) {
    $signtool = Find-SdkTool "signtool.exe"
    if (-not $signtool) {
        throw "signtool.exe not found. Install the Windows SDK or add it to PATH."
    }

    $certSubject = "CN=ClipboardTyperLocalTest"
    $pfxPath = Join-Path $packagingDir "ClipboardTyperLocalTest.pfx"
    $pfxPassword = "clipboardtyper"  # local test cert only - not used for the real Store submission

    $existingCert = Get-ChildItem Cert:\CurrentUser\My | Where-Object { $_.Subject -eq $certSubject }
    if (-not $existingCert) {
        Write-Host "`n==> Creating local self-signed test certificate ($certSubject)..."
        $existingCert = New-SelfSignedCertificate -Type Custom -Subject $certSubject `
            -KeyUsage DigitalSignature -FriendlyName "Clipboard Typer local test cert" `
            -CertStoreLocation "Cert:\CurrentUser\My" `
            -TextExtension @("2.5.29.37={text}1.3.6.1.5.5.7.3.3", "2.5.29.19={text}")
        $securePwd = ConvertTo-SecureString -String $pfxPassword -Force -AsPlainText
        Export-PfxCertificate -Cert $existingCert -FilePath $pfxPath -Password $securePwd | Out-Null
        Write-Host "==> IMPORTANT: install $pfxPath into 'Trusted People' (CurrentUser) to sideload without a security warning:"
        Write-Host "    Certutil -user -p $pfxPassword -importpfx $pfxPath TrustedPeople"
    }

    Write-Host "`n==> Signing package with local test certificate..."
    & $signtool sign /a /fd SHA256 /s My /n $certSubject $msixPath
    if ($LASTEXITCODE -ne 0) { throw "signtool failed (exit $LASTEXITCODE)." }
    Write-Host "==> Signed. You can now sideload-install this .msix for local testing (double-click it, or Add-AppxPackage)."
} else {
    Write-Host "`n(Not signed - pass -SignForTesting if you want to sideload-test this build locally.)"
    Write-Host "This unsigned .msix can still be uploaded to Partner Center as-is; the Store signs it during certification."
}

Write-Host "`nDone."
