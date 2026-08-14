<#
.SYNOPSIS
    Builds ClipboardTyper.exe with PyInstaller, stages it with the MSIX
    manifest and assets, and packages it into a .msix using the Windows
    SDK's makeappx.exe - for either local sideload testing or a real
    Partner Center submission, without ever hand-editing AppxManifest.xml.

.DESCRIPTION
    Run this from a normal PowerShell window on Windows (not this repo's
    Linux dev sandbox - it needs PyInstaller and the Windows SDK, both of
    which only exist on Windows).

    AppxManifest.xml on disk always holds your real Partner Center identity
    (Name/Publisher/PublisherDisplayName) and is never modified by this
    script, whichever -Target you build with:

      -Target Store  (default) - packages AppxManifest.xml exactly as
        checked in (your real identity), unsigned. Upload straight to
        Partner Center - it signs the package itself during certification.

      -Target Local - builds an install-ready package for testing on your
        own PC right now. The staged copy's Identity is swapped, in memory,
        for a local-test Name/Publisher (see $LocalTestName below), and the
        package is automatically signed with a matching self-signed test
        certificate (created on first use). The real AppxManifest.xml file
        is untouched - the swap only happens in the staged copy inside
        packaging\staging, which is gitignored.

    Prerequisites:
      - Python + this project's requirements.txt installed (pip install -r
        ..\requirements.txt), plus PyInstaller (pip install pyinstaller).
      - makeappx.exe / signtool.exe: the script looks for them on PATH, then
        under a normal Windows SDK install location, and if neither is found
        it auto-downloads the small Microsoft.Windows.SDK.BuildTools NuGet
        package into packaging\.tools (no SDK installer needed). If that
        download is blocked by your network, install the Windows SDK
        manually and choose the "MSIX Packaging Tools" feature.
      - For -Target Store: AppxManifest.xml's Name/Publisher fields filled
        in with the values Partner Center gives you after you reserve the
        app name (see README.md's MSIX/Store section). -Target Local works
        with no manifest changes at all.

    Every -Target Store build is checked against packaging\.last_store_version
    (a small tracked file recording the last version actually built for the
    Store) and refuses to build if <Identity Version> hasn't changed since
    then - see README.md's versioning policy for how much to bump it.

.PARAMETER Version
    Overrides the <Identity Version="..."> in the staged AppxManifest.xml
    for this build, e.g. -Version 1.2.0.0. Optional - if omitted, the
    manifest's existing version is used as-is. Only touches the Identity
    element's own Version attribute, never TargetDeviceFamily's
    MinVersion/MaxVersionTested.

.PARAMETER Target
    'Store' (default) - real identity, unsigned, ready for Partner Center.
    'Local' - local-test identity, automatically self-signed, ready to
    sideload-install on this PC right now for testing.

.EXAMPLE
    .\build_msix.ps1                          # Store submission build
    .\build_msix.ps1 -Target Local             # local sideload test build
    .\build_msix.ps1 -Target Store -Version 1.1.0.0
#>

param(
    [string]$Version = $null,
    [ValidateSet('Store', 'Local')]
    [string]$Target = 'Store'
)

$ErrorActionPreference = "Stop"

$packagingDir = $PSScriptRoot
$repoRoot = Split-Path $packagingDir -Parent
$stagingDir = Join-Path $packagingDir "staging"
$distDir = Join-Path $repoRoot "dist"
$outDir = Join-Path $packagingDir "out"

# Local-test identity used only for -Target Local, only in the staged copy.
# Deliberately matches the Subject of the self-signed test certificate
# created below - MSIX requires those two to match exactly for a signed
# package to install.
$LocalTestName = "ClipboardTyper.LocalTest"
$LocalTestPublisher = "CN=ClipboardTyperLocalTest"
$LocalTestPublisherDisplayName = "Clipboard Typer (local test build)"

Write-Host "==> Repo root:      $repoRoot"
Write-Host "==> Staging folder: $stagingDir"
Write-Host "==> Target:         $Target"

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

# The manifest is read into memory and only ever written back out to the
# staged copy - AppxManifest.xml on disk (your source of truth, holding
# your real Partner Center identity) is never modified by this script.
$manifestSrc = Join-Path $packagingDir "AppxManifest.xml"
$manifestDst = Join-Path $stagingDir "AppxManifest.xml"
$manifestContent = Get-Content $manifestSrc -Raw

# Attribute/element replacements below are all scoped to a specific tag so
# they can never accidentally touch a same-named attribute elsewhere in the
# file (e.g. a blind "Version=" replace would also corrupt
# TargetDeviceFamily's MinVersion="..." / MaxVersionTested="...", since
# "Version=" is a literal substring of "MinVersion=").
function Set-XmlAttr($Text, $Tag, $Attr, $Value) {
    $pattern = "(<$Tag\b[^>]*?\b$Attr=`")[^`"]*(`")"
    return [regex]::Replace($Text, $pattern, ('$1' + $Value + '$2'))
}

function Set-XmlElementText($Text, $Tag, $Value) {
    $pattern = "(<$Tag>)[^<]*(</$Tag>)"
    return [regex]::Replace($Text, $pattern, ('$1' + $Value + '$2'))
}

if ($Version) {
    Write-Host "==> Setting package version to $Version"
    $manifestContent = Set-XmlAttr $manifestContent "Identity" "Version" $Version
}

if ($Target -eq 'Local') {
    Write-Host "==> Using local-test identity ($LocalTestName / $LocalTestPublisher) in the staged package only."
    $manifestContent = Set-XmlAttr $manifestContent "Identity" "Name" $LocalTestName
    $manifestContent = Set-XmlAttr $manifestContent "Identity" "Publisher" $LocalTestPublisher
    $manifestContent = Set-XmlElementText $manifestContent "PublisherDisplayName" $LocalTestPublisherDisplayName
} else {
    if ($manifestContent -match [regex]::Escape($LocalTestName) -or $manifestContent -match [regex]::Escape($LocalTestPublisher)) {
        Write-Warning "AppxManifest.xml itself still has the local-test Name/Publisher checked in."
        Write-Warning "Replace them with your real Partner Center values (App management > App identity) before submitting - see README.md's MSIX/Store section."
    }
}

Set-Content -Path $manifestDst -Value $manifestContent -Encoding UTF8

# --- 3. Locate makeappx.exe / signtool.exe ----------------------------------
# Tries, in order: PATH, an existing Windows SDK install, then auto-downloads
# the "Microsoft.Windows.SDK.BuildTools" NuGet package (the same tools, ~15MB,
# no installer/reboot/admin rights needed) into packaging\.tools and caches
# it there. If your network blocks nuget.org (common on locked-down corporate
# machines), this fallback will fail cleanly and print the manual option.
$toolsCacheDir = Join-Path $packagingDir ".tools"

function Get-SdkBuildToolsFromNuGet {
    $marker = Join-Path $toolsCacheDir ".extracted"
    if (Test-Path $marker) { return $toolsCacheDir }

    Write-Host "==> makeappx/signtool not found locally - fetching Microsoft.Windows.SDK.BuildTools from NuGet..."
    New-Item -ItemType Directory -Path $toolsCacheDir -Force | Out-Null

    $versionsUrl = "https://api.nuget.org/v3-flatcontainer/microsoft.windows.sdk.buildtools/index.json"
    $latest = (Invoke-RestMethod -Uri $versionsUrl -UseBasicParsing).versions[-1]
    Write-Host "==> Using Microsoft.Windows.SDK.BuildTools $latest"

    $nupkgUrl = "https://api.nuget.org/v3-flatcontainer/microsoft.windows.sdk.buildtools/$latest/microsoft.windows.sdk.buildtools.$latest.nupkg"
    $zipPath = Join-Path $toolsCacheDir "buildtools.zip"
    Invoke-WebRequest -Uri $nupkgUrl -OutFile $zipPath -UseBasicParsing

    Expand-Archive -Path $zipPath -DestinationPath $toolsCacheDir -Force
    New-Item -ItemType File -Path $marker | Out-Null
    return $toolsCacheDir
}

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

    try {
        $toolsDir = Get-SdkBuildToolsFromNuGet
        $found = Get-ChildItem -Path $toolsDir -Recurse -Filter $toolName -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match "\\x64\\" } |
            Select-Object -First 1
        if ($found) { return $found.FullName }
    } catch {
        Write-Warning "Could not auto-fetch build tools from NuGet (probably a network/proxy restriction): $_"
    }
    return $null
}

$makeappx = Find-SdkTool "makeappx.exe"
if (-not $makeappx) {
    throw "makeappx.exe not found, and the automatic NuGet fetch didn't work either. Manual fix: install the " +
          "Windows SDK from https://developer.microsoft.com/windows/downloads/windows-sdk/ - a Custom install " +
          "with just the 'MSIX Packaging Tools' feature checked is enough, you don't need the whole SDK."
}
Write-Host "==> Using makeappx: $makeappx"

# --- 4. Pack the .msix -------------------------------------------------------
if (-not (Test-Path $outDir)) { New-Item -ItemType Directory -Path $outDir | Out-Null }

# Read the version back out of the staged manifest (reflects -Version if it
# was passed, otherwise whatever AppxManifest.xml already had) so the output
# filename always names the exact version it contains - important for a
# Store submission, since Partner Center rejects a resubmission that reuses
# a version it has already seen, and a versioned filename makes it obvious
# at a glance which build is which once you have more than one in `out\`.
$versionMatch = [regex]::Match($manifestContent, '<Identity\b[^>]*?\bVersion="([\d\.]+)"')
$effectiveVersion = if ($versionMatch.Success) { $versionMatch.Groups[1].Value } else { "unknown-version" }

# Every Store build must carry a version different from the last one that
# was actually built for the Store - Partner Center rejects a resubmission
# that reuses a version it's already seen, and it's easy to forget to bump
# <Identity Version="..."> after a round of code changes. packaging\
# .last_store_version (tracked in git, so it's a shared record across
# machines/sessions) remembers the last Store-target version built here;
# -Target Local builds never touch it, since those aren't submissions.
$lastStoreVersionFile = Join-Path $packagingDir ".last_store_version"
if ($Target -eq 'Store' -and (Test-Path $lastStoreVersionFile)) {
    $lastStoreVersion = (Get-Content $lastStoreVersionFile -Raw).Trim()
    if ($lastStoreVersion -and $lastStoreVersion -eq $effectiveVersion) {
        throw "AppxManifest.xml's <Identity Version=`"$effectiveVersion`"> matches the last version built for the " +
              "Store (see packaging\.last_store_version). Partner Center rejects a resubmission that reuses a " +
              "version it has already seen - bump <Identity Version> in AppxManifest.xml (or pass -Version) first. " +
              "See README.md's versioning policy: small fixes bump the Build number, real enhancements bump Minor, " +
              "and major/drastic changes bump Major."
    }
}

$msixName = if ($Target -eq 'Local') {
    "ClipboardTyper-LocalTest-$effectiveVersion.msix"
} else {
    "ClipboardTyper-$effectiveVersion.msix"
}
$msixPath = Join-Path $outDir $msixName

Write-Host "`n==> Packing $msixPath ..."
& $makeappx pack /d $stagingDir /p $msixPath /overwrite
if ($LASTEXITCODE -ne 0) { throw "makeappx failed (exit $LASTEXITCODE)." }

Write-Host "`n==> Package created: $msixPath"

if ($Target -eq 'Store') {
    Set-Content -Path $lastStoreVersionFile -Value $effectiveVersion -Encoding UTF8 -NoNewline
    Write-Host "==> Recorded $effectiveVersion as the last Store build version (packaging\.last_store_version) - commit this file so the record persists."
}

# --- 5. -Target Local: sign with a local test certificate --------------------
if ($Target -eq 'Local') {
    $signtool = Find-SdkTool "signtool.exe"
    if (-not $signtool) {
        throw "signtool.exe not found. Install the Windows SDK or add it to PATH."
    }

    $certSubject = $LocalTestPublisher
    $pfxPath = Join-Path $packagingDir "ClipboardTyperLocalTest.pfx"
    $pfxPassword = "clipboardtyper"  # local test cert only - not used for the real Store submission

    $cert = Get-ChildItem Cert:\CurrentUser\My | Where-Object { $_.Subject -eq $certSubject } | Select-Object -First 1
    if (-not $cert) {
        Write-Host "`n==> Creating local self-signed test certificate ($certSubject)..."
        # Basic Constraints must explicitly say "false" (Subject Type=End
        # Entity) - an empty value here produces a malformed extension that
        # Windows' chain engine silently rejects as an untrusted root, even
        # after the cert is imported into Trusted Root/Trusted People. That
        # was the actual cause of "certificate chain ... terminated in a
        # root certificate which is not trusted" during Add-AppxPackage.
        $cert = New-SelfSignedCertificate -Type Custom -Subject $certSubject `
            -KeyUsage DigitalSignature -FriendlyName "Clipboard Typer local test cert" `
            -CertStoreLocation "Cert:\CurrentUser\My" `
            -TextExtension @("2.5.29.37={text}1.3.6.1.5.5.7.3.3", "2.5.29.19={text}false")
        $securePwd = ConvertTo-SecureString -String $pfxPassword -Force -AsPlainText
        Export-PfxCertificate -Cert $cert -FilePath $pfxPath -Password $securePwd | Out-Null
        Write-Host "==> IMPORTANT: install $pfxPath into 'Trusted People' (CurrentUser) to sideload without a security warning:"
        Write-Host "    Certutil -user -p $pfxPassword -importpfx $pfxPath TrustedPeople"
    }

    # Match by thumbprint, not /n subject-name substring matching combined
    # with /a (auto-select) - that combination is what failed with "No
    # certificates were found that met all the given criteria" even though
    # the certificate genuinely existed. Thumbprint is exact, no ambiguity.
    Write-Host "`n==> Signing package with local test certificate (thumbprint $($cert.Thumbprint))..."
    & $signtool sign /fd SHA256 /s My /sha1 $cert.Thumbprint $msixPath
    if ($LASTEXITCODE -ne 0) { throw "signtool failed (exit $LASTEXITCODE)." }
    Write-Host "==> Signed. You can now sideload-install this .msix for local testing (double-click it, or Add-AppxPackage)."
} else {
    Write-Host "`n(Store target - not signed. Upload as-is to Partner Center; it signs the package during certification.)"
}

Write-Host "`nDone."
