# AITP installer for Windows (Windows PowerShell 5.1 or PowerShell 7).
#
#   & ([scriptblock]::Create((irm https://github.com/2742zdz/AITP/releases/latest/download/setup.ps1))) `
#       -BaseUrl 'https://gateway.example.com' -ApiKey '<api_key>'
#
# Downloads aitp-manifest.json of the latest GitHub release, checks it,
# downloads the program for this machine from that same release (by its
# version, so a release published meanwhile cannot mix in), checks its size
# and SHA-256 against the manifest, installs it (default
# %LOCALAPPDATA%\AITP\bin\aitp.exe, added to the user PATH), then runs
# `aitp init`, which configures Claude Code and starts (or refreshes) the
# local proxy.
#
# Trust: the manifest and the program are trusted because they come over
# HTTPS from GitHub. This script does not check the manifest's minisign
# signature; `aitp update` does (see README).
#
# For tests only: -DownloadBase URL (or AITP_SETUP_DOWNLOAD_BASE) reads the
# manifest and the program from one flat directory instead, and
# AITP_SETUP_RELEASES_URL replaces https://github.com/2742zdz/AITP/releases.
#
# Errors are thrown, never `exit`: run as a script block, `exit` would close
# the user's PowerShell window.

param(
    [string]$BaseUrl = "",
    [string]$ApiKey = "",
    [string]$DownloadBase = "",
    [string]$InstallDir = "",
    [switch]$NoStart,
    [switch]$Codex,
    [switch]$InstallOnly,
    [switch]$Help
)

$ErrorActionPreference = "Stop"

$GitHubReleases = "https://github.com/2742zdz/AITP/releases"
$ManifestName = "aitp-manifest.json"
$ManifestLimit = 1048576
$ProgramMin = 1048576
$ProgramMax = 67108864
$VersionPattern = '(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})'
$EntryPattern = '\{"sha256":"([0-9a-f]{64})","size":(0|[1-9][0-9]{0,9})\}'
$OnWindows = ($PSVersionTable.PSEdition -ne "Core") -or $IsWindows

function Write-Setup {
    param([string]$Message)
    [Console]::Error.WriteLine("aitp-setup: $Message")
}

function Show-Usage {
    @'
Usage: setup.ps1 -BaseUrl URL -ApiKey KEY [options]

  -BaseUrl URL          The Gateway base URL (passed to `aitp init`).
  -ApiKey KEY           The Gateway API key. Omitted: asked for on the console.
  -InstallDir DIR       Install directory (default: %LOCALAPPDATA%\AITP\bin).
  -NoStart              Configure, but do not start the local proxy (a proxy
                        stopped to replace the program is started again).
  -Codex                Also configure Codex (`aitp init-codex`).
  -InstallOnly          Only install or update the program; configure nothing
                        (-BaseUrl and -ApiKey are then optional).

AITP is downloaded from the latest release of https://github.com/2742zdz/AITP.
Environment (lower precedence than the parameters): AITP_INSTALL_DIR.
'@
}

# An absolute http(s) URL without credentials, query, fragment or spaces.
function Assert-Url {
    param([string]$What, [string]$Url)
    if ($Url -cnotmatch '\Ahttps?://([A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(:[0-9]{1,5})?(/[^\s?#]*)?\z') {
        throw "$What is not an http(s) URL without credentials, query or fragment: $Url"
    }
}

# Plain http only to this machine (tests), or with AITP_SETUP_ALLOW_HTTP=1.
function Assert-DownloadScheme {
    param([string]$Url)
    if ($Url.StartsWith("https://", [StringComparison]::Ordinal)) { return }
    $hostName = ([Uri]$Url).Host
    if (@("127.0.0.1", "localhost", "[::1]", "::1") -contains $hostName) { return }
    if ($env:AITP_SETUP_ALLOW_HTTP -eq "1") { return }
    throw "Refusing to download over plain http from $Url."
}

function Get-Sha256 {
    param([string]$Path)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($Path)
    try {
        return ([BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $stream.Dispose()
        $algorithm.Dispose()
    }
}

function Get-AssetName {
    # The native architecture: the registry value is not changed by WOW64 or
    # by x64 emulation on ARM64, unlike the process environment.
    $architecture = ""
    try {
        $architecture = [string](Get-ItemProperty -LiteralPath "HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" -Name PROCESSOR_ARCHITECTURE -ErrorAction Stop).PROCESSOR_ARCHITECTURE
    }
    catch {
        $architecture = ""
    }
    if ([string]::IsNullOrWhiteSpace($architecture)) {
        $architecture = [string]$env:PROCESSOR_ARCHITEW6432
    }
    if ([string]::IsNullOrWhiteSpace($architecture)) {
        $architecture = [string]$env:PROCESSOR_ARCHITECTURE
    }
    switch ($architecture.Trim().ToUpperInvariant()) {
        "AMD64" { return "aitp-windows-x86_64.exe" }
        "X64" { return "aitp-windows-x86_64.exe" }
        "ARM64" { return "aitp-windows-aarch64.exe" }
        default { throw "Unsupported Windows architecture: '$architecture'." }
    }
}

function Save-Url {
    param([string]$Url, [string]$Destination, [long]$Limit)
    $client = New-Object Net.WebClient
    try {
        $client.DownloadFile($Url, $Destination)
    }
    catch {
        throw "Download failed: $Url ($($_.Exception.Message))"
    }
    finally {
        $client.Dispose()
    }
    if ((Get-Item -LiteralPath $Destination).Length -gt $Limit) {
        throw "$Url is larger than $Limit bytes."
    }
}

function Compare-Version {
    param([string]$Left, [string]$Right)
    $a = $Left.Split('.'); $b = $Right.Split('.')
    for ($i = 0; $i -lt 3; $i++) {
        $x = [long]$a[$i]; $y = [long]$b[$i]
        if ($x -lt $y) { return -1 }
        if ($x -gt $y) { return 1 }
    }
    return 0
}

# Quotes one argument with the CommandLineToArgvW rules (Windows PowerShell
# 5.1 has only the single-string ProcessStartInfo.Arguments).
function ConvertTo-Argument {
    param([AllowEmptyString()][string]$Value)
    if ($Value.Length -eq 0) { return '""' }
    if ($Value -notmatch '[\s"]') { return $Value }
    $builder = New-Object Text.StringBuilder
    $null = $builder.Append('"')
    $backslashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq [char]'\') { $backslashes++; continue }
        if ($character -eq [char]'"') {
            $null = $builder.Append([string]::new([char]'\', $backslashes * 2 + 1)).Append('"')
            $backslashes = 0
            continue
        }
        if ($backslashes -gt 0) {
            $null = $builder.Append([string]::new([char]'\', $backslashes))
            $backslashes = 0
        }
        $null = $builder.Append($character)
    }
    $null = $builder.Append([string]::new([char]'\', $backslashes * 2)).Append('"')
    return $builder.ToString()
}

# Runs the program with its output on this console (not in a pipeline that
# a detached proxy could keep open) and returns its exit code.
function Invoke-Program {
    param([string]$Path, [string[]]$Arguments, [hashtable]$Environment = @{})
    $startInfo = New-Object Diagnostics.ProcessStartInfo
    $startInfo.FileName = $Path
    $startInfo.UseShellExecute = $false
    $startInfo.Arguments = [string]::Join(" ", [string[]]@($Arguments | ForEach-Object { ConvertTo-Argument $_ }))
    foreach ($name in $Environment.Keys) {
        $startInfo.EnvironmentVariables[$name] = [string]$Environment[$name]
    }
    $process = [Diagnostics.Process]::Start($startInfo)
    try {
        $process.WaitForExit()
        return [int]$process.ExitCode
    }
    finally {
        $process.Dispose()
    }
}

# Whether the local proxy of environment $Product (claude, codex) runs:
# `aitp --environment <product> status` reports "service active: yes".
function Test-ProxyActive {
    param([string]$Path, [string]$Product)
    try {
        $startInfo = New-Object Diagnostics.ProcessStartInfo
        $startInfo.FileName = $Path
        $startInfo.UseShellExecute = $false
        $startInfo.RedirectStandardOutput = $true
        $startInfo.Arguments = "--environment $Product status"
        $process = [Diagnostics.Process]::Start($startInfo)
        try {
            $output = $process.StandardOutput.ReadToEnd()
            $process.WaitForExit()
        }
        finally {
            $process.Dispose()
        }
        return @($output -split "\r?\n") -ccontains "service active: yes"
    }
    catch {
        return $false
    }
}

# Starts again the proxies this run stopped ($State.Stopped) that do not run
# now: after a failure, and after `aitp init` (which starts only its own).
function Restore-Proxies {
    if ($State.Stopped.Count -eq 0) { return }
    $path = $State.Program
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        Write-Setup "warning: the stopped AITP proxies ($($State.Stopped -join ', ')) cannot be started again: $path is missing"
        return
    }
    foreach ($product in $State.Stopped) {
        if (Test-ProxyActive $path $product) { continue }
        Write-Setup "starting the AITP local proxy ($product) again"
        try {
            $code = Invoke-Program $path @("--environment", $product, "start")
            if ($code -ne 0) { Write-Setup "warning: aitp --environment $product start exited with $code" }
        }
        catch {
            Write-Setup "warning: could not start the AITP local proxy ($product): $($_.Exception.Message)"
        }
    }
}

# The version an aitp program reports ("aitp X.Y.Z"), or "".
function Get-ProgramVersion {
    param([string]$Path)
    try {
        $output = (& $Path --version 2>$null | Out-String).Trim()
    }
    catch {
        return ""
    }
    if ($output -cmatch "\Aaitp ($VersionPattern)\z") { return $Matches[1] }
    return ""
}

# Replaces $Destination with $Source (same directory), retrying while a
# process that is shutting down still holds the old program: the old program
# is renamed aside, the new one renamed into place; if that fails, the old one
# is renamed back, so $Destination is never left missing unnoticed.
function Move-Replace {
    param([string]$Source, [string]$Destination, [int]$Attempts = 120)
    for ($attempt = 1; ; $attempt++) {
        $aside = $null
        try {
            if (Test-Path -LiteralPath $Destination -PathType Leaf) {
                $aside = Join-Path (Split-Path -Parent $Destination) (".aitp.replace." + [guid]::NewGuid().ToString("N") + ".bak")
                [IO.File]::Move($Destination, $aside)
            }
            [IO.File]::Move($Source, $Destination)
        }
        catch {
            $failure = $_.Exception.Message
            if ($aside -and (Test-Path -LiteralPath $aside -PathType Leaf) -and -not (Test-Path -LiteralPath $Destination)) {
                try {
                    [IO.File]::Move($aside, $Destination)
                }
                catch {
                    throw "Could not replace $Destination ($failure), nor put the previous program back ($($_.Exception.Message)): it is at $aside; rename it to $(Split-Path -Leaf $Destination)."
                }
            }
            if ($attempt -ge $Attempts) {
                $outcome = if (Test-Path -LiteralPath $Destination -PathType Leaf) { "the previous program is still installed" } else { "nothing is installed" }
                throw "Could not replace $Destination ($failure); $outcome. Stop AITP (aitp stop) or close programs started by it, then run setup again."
            }
            Start-Sleep -Milliseconds 250
            continue
        }
        # The old program may still be running (then it stays; uninstall removes it).
        if ($aside) { Remove-Item -LiteralPath $aside -Force -ErrorAction SilentlyContinue }
        return
    }
}

# Moves $Source to $Destination (aitp-previous.exe), replacing whatever is
# there without writing through it: a link is removed, not followed. A
# failure only loses the copy of the previous program.
function Set-PreviousProgram {
    param([string]$Source, [string]$Destination)
    try {
        # Deleting a file or a link never follows it; a link to a directory
        # (or an empty directory) goes with Directory.Delete.
        try { [IO.File]::Delete($Destination) } catch { }
        if ([IO.Directory]::Exists($Destination)) {
            try { [IO.Directory]::Delete($Destination, $false) } catch { }
        }
        [IO.File]::Move($Source, $Destination)
    }
    catch {
        Write-Setup "warning: could not keep the previous program at $Destination ($($_.Exception.Message))"
        Remove-Item -LiteralPath $Source -Force -ErrorAction SilentlyContinue
    }
}

function Add-UserPath {
    param([string]$Directory)
    $comparison = [StringComparison]::OrdinalIgnoreCase
    $userPath = [string][Environment]::GetEnvironmentVariable("Path", "User")
    $present = $false
    foreach ($part in $userPath.Split(';')) {
        if ($part.Trim().Length -gt 0 -and [string]::Equals($part.TrimEnd('\'), $Directory.TrimEnd('\'), $comparison)) {
            $present = $true
        }
    }
    if (-not $present) {
        $newPath = if ([string]::IsNullOrWhiteSpace($userPath)) { $Directory } else { $userPath.TrimEnd(';') + ";" + $Directory }
        [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
        Write-Setup "added $Directory to your user PATH (new terminals)"
    }
}

function Test-OnPath {
    param([string]$Directory)
    foreach ($part in ([string]$env:Path).Split([IO.Path]::PathSeparator)) {
        if ($part.Trim().Length -gt 0 -and
            [string]::Equals($part.TrimEnd('\', '/'), $Directory.TrimEnd('\', '/'), [StringComparison]::OrdinalIgnoreCase)) {
            return $true
        }
    }
    return $false
}

# Checks the manifest (the canonical form the release writes: one line of
# sorted, compact JSON) and returns the release entry of $Asset.
function Read-Manifest {
    param([string]$Path, [string]$Asset)
    $bytes = [IO.File]::ReadAllBytes($Path)
    foreach ($byte in $bytes) {
        if ($byte -ge 0x80) { throw "The manifest is not an AITP release manifest (not ASCII)." }
    }
    $text = [Text.Encoding]::ASCII.GetString($bytes)
    $entries = '"[A-Za-z0-9][A-Za-z0-9._-]*":' + $EntryPattern
    $pattern = '\A\{"assets":\{' + $entries + '(,' + $entries + ')*\},"distribution":"aitp","manifest_schema":1,"version":"(' + $VersionPattern + ')"\}\n\z'
    $match = [regex]::Match($text, $pattern)
    if (-not $match.Success) {
        throw "The manifest is not an AITP release manifest (schema 1, distribution aitp)."
    }
    # Every asset name once (as the release tools and the program require).
    $names = New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    foreach ($name in [regex]::Matches($text, '[{,]"([A-Za-z0-9][A-Za-z0-9._-]*)":\{"sha256"')) {
        if (-not $names.Add($name.Groups[1].Value)) { throw "The manifest lists $($name.Groups[1].Value) twice." }
    }
    $version = [regex]::Match($text, ',"version":"([^"]*)"\}\n\z').Groups[1].Value
    $found = [regex]::Matches($text, '[{,]"' + [regex]::Escape($Asset) + '":' + $EntryPattern)
    if ($found.Count -ne 1) {
        throw "The manifest lists $Asset $($found.Count) times (expected once); this platform may not be released."
    }
    $size = [long]$found[0].Groups[2].Value
    if ($size -lt $ProgramMin -or $size -gt $ProgramMax) {
        throw "The manifest gives $Asset a size of $size bytes, not between $ProgramMin and $ProgramMax."
    }
    return [pscustomobject]@{ Version = $version; Sha256 = $found[0].Groups[1].Value; Size = $size }
}

function Install-Aitp {
    if ($Help) { Show-Usage; return }

    $base = if ($DownloadBase) { $DownloadBase } else { [string]$env:AITP_SETUP_DOWNLOAD_BASE }
    $releases = if ($env:AITP_SETUP_RELEASES_URL) { $env:AITP_SETUP_RELEASES_URL } else { $GitHubReleases }
    $directory = if ($InstallDir) { $InstallDir } else { [string]$env:AITP_INSTALL_DIR }
    $key = $ApiKey

    if (-not $InstallOnly -and [string]::IsNullOrEmpty($BaseUrl)) {
        Show-Usage
        throw "-BaseUrl is required."
    }
    if ($BaseUrl) { Assert-Url "-BaseUrl" $BaseUrl }
    # The manifest of the latest release; the program comes from the release
    # the manifest names (or, for tests, both from one flat directory).
    if ($base) {
        $base = $base.TrimEnd('/')
        Assert-Url "the download base" $base
        Assert-DownloadScheme $base
        $manifestUrl = "$base/$ManifestName"
        $assetBase = $base
    }
    else {
        $releases = $releases.TrimEnd('/')
        Assert-Url "the releases URL" $releases
        Assert-DownloadScheme $releases
        $manifestUrl = "$releases/latest/download/$ManifestName"
        $assetBase = $null
    }

    if (-not $InstallOnly) {
        if ([string]::IsNullOrEmpty($key)) {
            $secure = Read-Host -AsSecureString "API key for $BaseUrl"
            $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
            try { $key = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
            finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
        }
        if ([string]::IsNullOrWhiteSpace($key)) { throw "The API key is empty." }
        if ($key.Contains("`r") -or $key.Contains("`n")) { throw "The API key must be a single line." }
    }

    $defaultDirectory = $false
    if ([string]::IsNullOrEmpty($directory)) {
        if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) { throw "LOCALAPPDATA is not set; pass -InstallDir." }
        $directory = Join-Path $env:LOCALAPPDATA "AITP\bin"
        $defaultDirectory = $true
    }
    $directory = [IO.Path]::GetFullPath($directory)
    $installPath = Join-Path $directory "aitp.exe"
    $backupPath = Join-Path $directory "aitp-previous.exe"
    $asset = Get-AssetName

    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $work = Join-Path ([IO.Path]::GetTempPath()) ("aitp-setup-" + [guid]::NewGuid().ToString("N"))
    $null = New-Item -ItemType Directory -Path $work
    $staged = $null
    $previous = $null
    $progress = $ProgressPreference
    $ProgressPreference = "SilentlyContinue"
    try {
        Write-Setup "release manifest: $manifestUrl"
        $manifestPath = Join-Path $work $ManifestName
        Save-Url $manifestUrl $manifestPath $ManifestLimit
        $release = Read-Manifest $manifestPath $asset
        if (-not $assetBase) { $assetBase = "$releases/download/v$($release.Version)" }
        Write-Setup "latest release: aitp $($release.Version) ($asset)"

        $null = New-Item -ItemType Directory -Path $directory -Force
        if (Test-Path -LiteralPath $installPath -PathType Container) { throw "Not a file: $installPath" }
        $installed = Test-Path -LiteralPath $installPath -PathType Leaf
        if ($installed -and ((Get-Item -LiteralPath $installPath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "Refusing to install over a link: $installPath"
        }

        $updated = $false
        if ($installed -and (Get-Sha256 $installPath) -ceq $release.Sha256) {
            Write-Setup "aitp $($release.Version) is already installed at $installPath; download skipped"
        }
        else {
            $installedVersion = if ($installed) { Get-ProgramVersion $installPath } else { "" }
            if ($installedVersion -and (Compare-Version $installedVersion $release.Version) -gt 0) {
                Write-Setup "aitp $installedVersion at $installPath is newer than $($release.Version); keeping it"
            }
            else {
                $candidate = Join-Path $work $asset
                Write-Setup "downloading $assetBase/$asset ($($release.Size) bytes)"
                Save-Url "$assetBase/$asset" $candidate $release.Size
                $size = (Get-Item -LiteralPath $candidate).Length
                if ($size -ne $release.Size) {
                    throw "$asset has $size bytes, the manifest says $($release.Size); nothing installed."
                }
                if ((Get-Sha256 $candidate) -cne $release.Sha256) {
                    throw "$asset does not match the manifest's SHA-256; nothing installed."
                }
                if (-not $OnWindows) { & chmod 0755 $candidate }
                if ((Get-ProgramVersion $candidate) -cne $release.Version) {
                    throw "The downloaded program does not report version $($release.Version); nothing installed."
                }

                # Everything that can fail on its own happens before any proxy
                # is stopped: the new program staged, the old one copied.
                $staged = Join-Path $directory (".aitp.install." + [guid]::NewGuid().ToString("N") + ".exe")
                [IO.File]::Copy($candidate, $staged, $false)
                if (-not $OnWindows) { & chmod 0755 $staged }
                if ($installed) {
                    $previous = Join-Path $directory (".aitp.install." + [guid]::NewGuid().ToString("N") + ".exe")
                    [IO.File]::Copy($installPath, $previous, $false)
                    # A running proxy holds aitp.exe open: stop the ones that
                    # run (Restore-Proxies starts them again at the end, if
                    # `aitp init` has not).
                    $State.Program = $installPath
                    foreach ($product in @("claude", "codex")) {
                        if (-not (Test-ProxyActive $installPath $product)) { continue }
                        Write-Setup "stopping the AITP local proxy ($product) to replace the program"
                        $null = $State.Stopped.Add($product)
                        $code = Invoke-Program $installPath @("--environment", $product, "stop")
                        if ($code -ne 0) { Write-Setup "warning: aitp stop exited with $code" }
                    }
                }
                Move-Replace $staged $installPath
                $staged = $null
                if ($previous) {
                    Set-PreviousProgram $previous $backupPath
                    $previous = $null
                }
                $updated = $true
                if ($installedVersion) {
                    Write-Setup "updated $installPath from aitp $installedVersion to $($release.Version) (previous program: $backupPath)"
                }
                else {
                    Write-Setup "installed aitp $($release.Version) at $installPath"
                }
            }
        }
    }
    finally {
        $ProgressPreference = $progress
        if ($staged) { Remove-Item -LiteralPath $staged -Force -ErrorAction SilentlyContinue }
        if ($previous) { Remove-Item -LiteralPath $previous -Force -ErrorAction SilentlyContinue }
        Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
    }

    # `aitp uninstall` removes the default directory's user PATH entry; a
    # custom directory's entry is left to the user, so it is not added here.
    if ($defaultDirectory -and $OnWindows) { Add-UserPath $directory }
    $onPath = Test-OnPath $directory
    if (-not $onPath) { $env:Path = $directory + [IO.Path]::PathSeparator + $env:Path }

    if ($InstallOnly) {
        Write-Setup "done (-InstallOnly: nothing configured)"
        return
    }
    $skipStart = if ($NoStart) { "1" } else { [string]$env:AITP_SKIP_START }
    Write-Setup "configuring Claude Code (aitp init)"
    $environment = @{ AITP_BINARY_UPDATED = $(if ($updated) { "1" } else { "0" }); AITP_SKIP_START = $skipStart }
    $code = Invoke-Program $installPath @("init", "--base-url", $BaseUrl, "--api-key", $key) $environment
    if ($code -ne 0) { throw "aitp init failed (exit $code)." }
    if ($Codex) {
        Write-Setup "configuring Codex (aitp init-codex)"
        $environment.AITP_BINARY_UPDATED = "0"
        $code = Invoke-Program $installPath @("init-codex", "--base-url", $BaseUrl, "--api-key", $key) $environment
        if ($code -ne 0) { throw "aitp init-codex failed (exit $code)." }
    }
    $key = $null

    Write-Setup ""
    if (-not $defaultDirectory -and -not $onPath) {
        Write-Setup "$directory is not on your PATH; add it to run aitp by name"
    }
    if ($NoStart) { Write-Setup "next: start the local proxy with: aitp start" }
    Write-Setup "next: run Claude Code as usual (claude); it now goes through the AITP local proxy."
    Write-Setup "      (aitp claude also works, and starts the proxy if it is not running.)"
    if ($Codex) { Write-Setup "      For Codex run: aitp codex" }
    Write-Setup "check: aitp doctor    remove: aitp uninstall"
}

# The proxies this run stopped to replace the program, started again however
# the run ends (a hashtable: the functions change its contents, never the
# variable, so nothing leaks into the caller's session).
$State = @{ Stopped = New-Object Collections.ArrayList; Program = $null }
try {
    Install-Aitp
}
finally {
    Restore-Proxies
}
