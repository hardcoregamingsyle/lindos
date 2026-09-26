#Requires -Version 5.1
<#
.SYNOPSIS
    Lindos Transfer kit: copies your files, bookmarks and app list from this Windows PC into
    one transfer folder that Lindos can bring in ("Transfer from Windows").

.DESCRIPTION
    Double-click LindosTransfer.cmd (next to this file) to run it. It creates a folder named
    LindosTransfer-<computer>-<yyyyMMdd-HHmm> next to the kit (for example on your USB stick):

      files\<category>\      Desktop, Documents, Downloads, Music, Pictures, Videos, Saved Games,
                             Favorites and the OneDrive files that are already on this PC
      browsers\              bookmarks from Chrome, Edge, Brave, Opera, Vivaldi; Firefox
                             bookmarks and history
      wallpaper.<ext>        your desktop picture (not the pictures that come with Windows)
      fonts\                 fonts you installed yourself (never the fonts that come with Windows)
      wifi\                  your Wi-Fi network names (passwords only with -IncludeWifiPasswords)
      steam\                 your Steam library list (game files only with -IncludeSteamGames)
      apps.json              the programs installed on this PC (names only, nothing is copied)
      apps-winget.json       the same list from winget, when winget is available
      lindos-transfer.json   the table of contents that Lindos reads
      LindosTransfer.log     what happened, in plain text

    Promises this script keeps:
      * Nothing on this PC is changed, moved or deleted. Files are only copied.
      * Files that live only in OneDrive (not on this PC) are skipped and listed, never
        downloaded (robocopy /XA:O plus an attribute check before copying).
      * Saved browser passwords, cookies and payment data are never read or copied.
      * The fonts and pictures that come with Windows are never copied (licensed to this PC).
      * Administrator rights are requested only for the optional Wi-Fi password step.

.PARAMETER Destination
    Folder in which the transfer folder is created. Default: the folder this script is in
    (for example the USB stick).

.PARAMETER User
    Windows account (or profile folder name) to copy. Default: you. Copying another account
    works only for files your account may read; sign in as that person for the best result.

.PARAMETER Include
    Only these categories (comma separated). Default: all of them.
    desktop, documents, downloads, music, pictures, videos, saved-games, favorites, onedrive,
    bookmarks, firefox, wallpaper, fonts, wifi, apps, steam-games

.PARAMETER IncludeWifiPasswords
    Also export Wi-Fi passwords (asks first, then asks Windows for administrator permission).

.PARAMETER IncludeFirefoxPasswords
    Also copy Firefox's saved-password files unchanged (asks first).

.PARAMETER IncludeSteamGames
    Also copy the installed Steam game folders (can be very large).

.PARAMETER InventoryOnly
    Do not copy files: only write the app list and the table of contents, and show how much
    space a full transfer needs.

.EXAMPLE
    LindosTransfer.cmd
    Copies everything into a new transfer folder next to the kit.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\LindosTransfer.ps1 -Include documents,pictures -Destination E:\

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\LindosTransfer.ps1 -WhatIf
    Test run: shows what would be copied and how big it is, writes nothing.

.NOTES
    Exit codes: 0 done; 1 finished with errors (see LindosTransfer.log) or failed;
    2 wrong options; 3 PowerShell is restricted on this PC; 4 nothing to transfer;
    5 not enough free space.
    Part of Lindos (package lindos-transfer), GPL-3.0-or-later.
    This file is ASCII-only on purpose: Windows PowerShell 5.1 reads a script without a
    byte-order mark in the ANSI code page. It must keep working in PowerShell 5.1.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Position = 0)]
    [string]$Destination = '',
    [string]$User = '',
    [string[]]$Include = @(),
    [switch]$IncludeWifiPasswords,
    [switch]$IncludeFirefoxPasswords,
    [switch]$IncludeSteamGames,
    [switch]$InventoryOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ------------------------------------------------------------------------------------------------
# Constants and options (plain values only: this part must also run in ConstrainedLanguage mode
# so that the script can explain itself there).
# ------------------------------------------------------------------------------------------------
$script:ToolName = 'LindosTransfer.ps1'
$script:ToolVersion = '1.0.0'
$script:ManifestName = 'lindos-transfer.json'
$script:ScriptPath = $PSCommandPath
$script:ScriptDir = $PSScriptRoot

# Category ids are the lindos-transfer contract (SPEC-WINDOWS 29.5).
$script:Categories = @('desktop', 'documents', 'downloads', 'music', 'pictures', 'videos',
    'saved-games', 'favorites', 'onedrive', 'bookmarks', 'firefox', 'wallpaper', 'fonts',
    'wifi', 'apps', 'steam-games')
$script:FolderCategories = @('desktop', 'documents', 'downloads', 'music', 'pictures', 'videos',
    'saved-games', 'favorites', 'onedrive')
$script:CategoryAliases = @{ 'steam' = 'steam-games'; 'wi-fi' = 'wifi'; 'savedgames' = 'saved-games';
    'saved_games' = 'saved-games'; 'bookmark' = 'bookmarks'; 'wallpapers' = 'wallpaper'; 'font' = 'fonts' }
$script:CategoryLabels = @{
    'desktop' = 'Desktop'; 'documents' = 'Documents'; 'downloads' = 'Downloads'; 'music' = 'Music'
    'pictures' = 'Pictures'; 'videos' = 'Videos'; 'saved-games' = 'Saved Games'
    'favorites' = 'Favorites'; 'onedrive' = 'OneDrive (files already on this PC)'
    'bookmarks' = 'Browser bookmarks'; 'firefox' = 'Firefox bookmarks and history'
    'wallpaper' = 'Desktop wallpaper'; 'fonts' = 'Fonts you installed'; 'wifi' = 'Wi-Fi networks'
    'apps' = 'List of installed apps'; 'steam-games' = 'Steam library'
}
# "User Shell Folders" value names (each known folder by its own name, never a Local* GUID).
$script:KnownFolderValues = @{
    'desktop' = 'Desktop'; 'documents' = 'Personal'; 'music' = 'My Music'; 'pictures' = 'My Pictures'
    'videos' = 'My Video'; 'favorites' = 'Favorites'
    'downloads' = '{374DE290-123F-4565-9164-39C4925E467B}'
    'saved-games' = '{4C5C32FF-BB9D-43B0-B5B4-2D72E54EAAA4}'
}
$script:KnownFolderDefaults = @{
    'desktop' = 'Desktop'; 'documents' = 'Documents'; 'downloads' = 'Downloads'; 'music' = 'Music'
    'pictures' = 'Pictures'; 'videos' = 'Videos'; 'saved-games' = 'Saved Games'; 'favorites' = 'Favorites'
}
$script:SkipProfileNames = @('Default', 'Default User', 'Public', 'All Users', 'defaultuser0',
    'defaultuser100000', 'WDAGUtilityAccount', 'DefaultAccount', 'Guest', 'WsiAccount',
    'systemprofile', 'LocalService', 'NetworkService')
$script:FontExtensions = @('.ttf', '.otf', '.ttc', '.otc', '.fon', '.fnt', '.pfb', '.pfm')
$script:Fat32MaxFileBytes = 4294967295
$script:FatMaxFileBytes = 2147483647
$script:MaxSkippedListed = 5000
$script:MaxCommandLine = 30000
# Programs that are really parts of Windows or runtimes for other programs (SPEC-WINDOWS 29.9).
$script:RuntimeNamePattern = '(?i)(redistributable|\bruntime\b|^Microsoft \.NET|^Microsoft ASP\.NET|' +
    '^Microsoft Windows Desktop Runtime|vcredist|^Microsoft Visual C\+\+ 20\d\d|' +
    '^Windows Software Development Kit|^Microsoft Update Health Tools|^Update for |' +
    '^Security Update for |^Hotfix for |^Windows Driver Package)'

$script:Opt = @{
    Destination = $Destination
    User = $User
    Include = $Include
    IncludeWifiPasswords = [bool]$IncludeWifiPasswords
    IncludeFirefoxPasswords = [bool]$IncludeFirefoxPasswords
    IncludeSteamGames = [bool]$IncludeSteamGames
    InventoryOnly = [bool]$InventoryOnly
}
$script:BoundParams = @{}
foreach ($boundKey in $PSBoundParameters.Keys) { $script:BoundParams[$boundKey] = $PSBoundParameters[$boundKey] }
$script:DryRun = [bool]$WhatIfPreference
$script:OutDir = ''
$script:LogPath = ''
$script:UsageError = ''
$script:Failures = 0
$script:WifiPasswords = $false
$script:FirefoxPasswords = $false
$script:WindowsDir = ''
$script:Skipped = $null
$script:SkippedOverflow = $null

# ------------------------------------------------------------------------------------------------
# Output (plain language for people moving from Windows) and the log file
# ------------------------------------------------------------------------------------------------
function Write-LogLine {
    param([string]$Text)
    if (-not $script:LogPath -or $script:DryRun) { return }
    try {
        $encoding = New-Object System.Text.UTF8Encoding($false)
        $stamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss', [System.Globalization.CultureInfo]::InvariantCulture)
        [System.IO.File]::AppendAllText($script:LogPath, ($stamp + ' ' + $Text + "`r`n"), $encoding)
    } catch {
        # A log that cannot be written must never stop the transfer.
        $null = $_
    }
}

function Write-Title {
    param([string]$Text)
    Write-Host ''
    Write-Host $Text -ForegroundColor Cyan
    Write-LogLine ('== ' + $Text)
}

function Write-Info {
    param([string]$Text)
    Write-Host ('  ' + $Text)
    Write-LogLine $Text
}

function Write-Good {
    param([string]$Text)
    Write-Host ('  ' + $Text) -ForegroundColor Green
    Write-LogLine ('OK: ' + $Text)
}

function Write-Warn {
    param([string]$Text)
    Write-Host ('  ' + $Text) -ForegroundColor Yellow
    Write-LogLine ('WARNING: ' + $Text)
}

function Write-Bad {
    param([string]$Text)
    Write-Host ('  ' + $Text) -ForegroundColor Red
    Write-LogLine ('ERROR: ' + $Text)
}

function Format-Size {
    param([double]$Bytes)
    if ($Bytes -ge 1GB) { return ('{0:N1} GB' -f ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return ('{0:N0} MB' -f ($Bytes / 1MB)) }
    if ($Bytes -ge 1KB) { return ('{0:N0} KB' -f ($Bytes / 1KB)) }
    return ('{0:N0} bytes' -f $Bytes)
}

function Format-Count {
    param([long]$Count)
    return ('{0:N0}' -f $Count)
}

# ------------------------------------------------------------------------------------------------
# Small helpers
# Convention: functions that return a list end with "return , $list" (so an empty or one-item
# list stays a list) and callers use "(Get-Thing)", never "@(Get-Thing)", which would wrap the
# list inside another list in PowerShell 5.1.
# ------------------------------------------------------------------------------------------------
function ConvertTo-CommandLineArg {
    # Quote one argument with the Windows (CommandLineToArgvW / CRT) rules, including the
    # trailing-backslash rule ("E:\My Stuff\" must become "E:\My Stuff\\").
    param([AllowEmptyString()][string]$Value)
    if ($Value -eq '') { return '""' }
    if ($Value -notmatch '[\s"]') { return $Value }
    $builder = New-Object System.Text.StringBuilder
    [void]$builder.Append('"')
    $slashes = 0
    foreach ($ch in $Value.ToCharArray()) {
        if ($ch -eq '\') { $slashes++; continue }
        if ($ch -eq '"') {
            [void]$builder.Append(('\' * (2 * $slashes + 1)))
            [void]$builder.Append('"')
            $slashes = 0
            continue
        }
        if ($slashes -gt 0) { [void]$builder.Append(('\' * $slashes)); $slashes = 0 }
        [void]$builder.Append($ch)
    }
    if ($slashes -gt 0) { [void]$builder.Append(('\' * (2 * $slashes))) }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Join-CommandLine {
    param([string[]]$Arguments)
    $parts = New-Object System.Collections.ArrayList
    foreach ($a in @($Arguments)) { [void]$parts.Add((ConvertTo-CommandLineArg $a)) }
    return ($parts.ToArray() -join ' ')
}

function Get-PropValue {
    # Read a property or dictionary key without tripping Set-StrictMode when it is missing.
    param($Object, [string]$Name)
    if ($null -eq $Object) { return $null }
    if ($Object -is [System.Collections.IDictionary]) {
        # System.Collections.Generic.Dictionary`2 (what JavaScriptSerializer returns for a JSON
        # object) implements IDictionary.Contains/Item as EXPLICIT interface members: calling
        # .Contains(...)/[...] on the object (or on a variable holding the cast) fails to bind
        # ("Cannot find an overload for 'Contains'..."). The cast must stay inline at the call
        # site -- PowerShell only honours it there, not once the result is assigned to a variable.
        if (([System.Collections.IDictionary]$Object).Contains($Name)) { return ([System.Collections.IDictionary]$Object)[$Name] }
        return $null
    }
    $prop = $Object.PSObject.Properties[$Name]
    if ($null -ne $prop) { return $prop.Value }
    return $null
}

function Get-RegValue {
    param([string]$Path, [string]$Name)
    try {
        $item = Get-ItemProperty -LiteralPath $Path -Name $Name -ErrorAction Stop
        return (Get-PropValue $item $Name)
    } catch {
        return $null
    }
}

function ConvertTo-JsonArray {
    # Always hand ConvertTo-Json a real array (PowerShell 5.1 unwraps one-element pipelines).
    param($Items)
    if ($null -eq $Items) { return , @() }
    return , @($Items)
}

function Get-NormalizedPath {
    param([string]$Path)
    if (-not $Path) { return '' }
    $full = $Path
    try { $full = [System.IO.Path]::GetFullPath($Path) } catch { $full = $Path }
    if ($full.Length -gt 3) { $full = $full.TrimEnd('\') }
    return $full
}

function Test-PathInside {
    # True when $Child is $Parent itself or somewhere below it (case-insensitive).
    param([string]$Child, [string]$Parent)
    $c = Get-NormalizedPath $Child
    $p = Get-NormalizedPath $Parent
    if (-not $c -or -not $p) { return $false }
    if ($c -ieq $p) { return $true }
    $prefix = $p
    if (-not $prefix.EndsWith('\')) { $prefix = $prefix + '\' }
    return $c.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)
}

function Test-Interactive {
    try {
        return ([Environment]::UserInteractive -and -not [Console]::IsInputRedirected)
    } catch {
        return $false
    }
}

function Confirm-OptIn {
    param([string]$Title, [string[]]$Lines)
    Write-Host ''
    Write-Host ('  ' + $Title) -ForegroundColor Yellow
    foreach ($line in $Lines) { Write-Host ('  ' + $line) -ForegroundColor Yellow }
    if (-not (Test-Interactive)) {
        Write-Warn 'Nobody can answer this question here, so this step is skipped.'
        return $false
    }
    $answer = Read-Host '  Type YES and press Enter to include them (anything else skips this step)'
    $yes = ([string]$answer).Trim() -match '^(?i:y|yes)$'
    if ($yes) { Write-LogLine ('Opt-in accepted: ' + $Title) } else { Write-Info 'Skipped.' }
    return $yes
}

function Get-DriveInfoFor {
    param([string]$Path)
    try {
        $root = [System.IO.Path]::GetPathRoot($Path)
        if (-not $root -or $root.StartsWith('\\')) { return $null }
        $drive = New-Object System.IO.DriveInfo($root)
        if (-not $drive.IsReady) { return $null }
        return $drive
    } catch {
        return $null
    }
}

function Get-FreeBytes {
    param([string]$Path)
    $drive = Get-DriveInfoFor $Path
    if ($null -eq $drive) { return -1 }
    try { return [long]$drive.AvailableFreeSpace } catch { return -1 }
}

function Remove-OwnFile {
    # The only place this script deletes anything: files or empty folders that it created
    # itself inside its own transfer folder or the temp folder. Never anything else.
    param([string]$Path)
    $full = Get-NormalizedPath $Path
    $temp = Get-NormalizedPath ([System.IO.Path]::GetTempPath())
    $allowed = ($script:OutDir -and (Test-PathInside $full $script:OutDir) -and ($full -ine (Get-NormalizedPath $script:OutDir))) -or
        (Test-PathInside $full $temp)
    if (-not $allowed) { throw ('Refusing to delete ' + $full + ': it was not created by the transfer kit.') }
    if (Test-Path -LiteralPath $full -PathType Leaf) {
        Remove-Item -LiteralPath $full -Force -ErrorAction SilentlyContinue
    } elseif ((Test-Path -LiteralPath $full -PathType Container) -and
        @(Get-ChildItem -LiteralPath $full -Force -ErrorAction SilentlyContinue).Count -eq 0) {
        Remove-Item -LiteralPath $full -Force -ErrorAction SilentlyContinue
    }
}

function New-TempFile {
    return [System.IO.Path]::GetTempFileName()
}

function Add-Skipped {
    # Everything that is not copied is listed in lindos-transfer.json ("skipped").
    param([string]$Path, [string]$Reason, [string]$Group = '')
    if ($script:Skipped.Count -lt $script:MaxSkippedListed) {
        [void]$script:Skipped.Add([ordered]@{ path = $Path; reason = $Reason })
        return
    }
    $key = $Reason + '|' + $Group
    if ($script:SkippedOverflow.Contains($key)) {
        $script:SkippedOverflow[$key].Count = $script:SkippedOverflow[$key].Count + 1
    } else {
        $script:SkippedOverflow[$key] = [pscustomobject]@{ Reason = $Reason; Group = $Group; Count = 1 }
    }
}

function Get-SkippedList {
    $all = New-Object System.Collections.ArrayList
    foreach ($entry in $script:Skipped) { [void]$all.Add($entry) }
    foreach ($key in @($script:SkippedOverflow.Keys)) {
        $o = $script:SkippedOverflow[$key]
        [void]$all.Add([ordered]@{ path = $o.Group; reason = ($o.Reason + ' (and ' + $o.Count + ' more like this, not listed)') })
    }
    return , $all.ToArray()
}

# ------------------------------------------------------------------------------------------------
# Start-up checks: language mode, 64-bit, options
# ------------------------------------------------------------------------------------------------
function Test-FullLanguage {
    if ($ExecutionContext.SessionState.LanguageMode -eq 'FullLanguage') { return $true }
    Write-Host ''
    Write-Host 'Lindos Transfer cannot run on this PC.' -ForegroundColor Red
    Write-Host '  Windows runs PowerShell in a restricted mode here (ConstrainedLanguage), usually because'
    Write-Host '  of a company policy or Smart App Control. The kit does not try to get around that.'
    Write-Host '  You can still copy your folders by hand: open File Explorer, select Desktop, Documents,'
    Write-Host '  Downloads, Music, Pictures and Videos, and copy them to your USB stick. On Lindos,'
    Write-Host '  "Transfer from Windows" can also read the Windows drive directly on a dual-boot PC.'
    return $false
}

function Get-ForwardedArguments {
    param([hashtable]$Bound)
    $out = New-Object System.Collections.ArrayList
    foreach ($key in @($Bound.Keys | Sort-Object)) {
        $value = $Bound[$key]
        if ($value -is [System.Management.Automation.SwitchParameter]) {
            if ($value.IsPresent) { [void]$out.Add('-' + $key) }
            continue
        }
        if ($value -is [bool]) {
            if ($value) { [void]$out.Add('-' + $key) }
            continue
        }
        [void]$out.Add('-' + $key)
        if ($value -is [array]) { [void]$out.Add((@($value) -join ',')) } else { [void]$out.Add([string]$value) }
    }
    return , $out.ToArray()
}

function Invoke-64BitRelaunch {
    # A 32-bit PowerShell sees a redirected registry and misses most 64-bit apps.
    if ([Environment]::Is64BitProcess -or -not [Environment]::Is64BitOperatingSystem) { return $null }
    $native = Join-Path $env:WINDIR 'Sysnative\WindowsPowerShell\v1.0\powershell.exe'
    if (-not (Test-Path -LiteralPath $native)) {
        Write-Warn 'This is 32-bit PowerShell and the 64-bit one was not found; the app list may be incomplete.'
        return $null
    }
    Write-Host '  Restarting in 64-bit PowerShell (needed for a complete list of your apps)...'
    $argv = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $script:ScriptPath) + (Get-ForwardedArguments -Bound $script:BoundParams)
    $proc = Start-Process -FilePath $native -ArgumentList (Join-CommandLine $argv) -NoNewWindow -PassThru
    $null = $proc.Handle
    $proc.WaitForExit()
    return [int]$proc.ExitCode
}

function Get-SelectedCategories {
    param([string[]]$Include, [bool]$WifiPasswords, [bool]$FirefoxPasswords, [bool]$SteamGames)
    $script:UsageError = ''
    $requested = New-Object System.Collections.ArrayList
    foreach ($item in @($Include)) {
        if ($null -eq $item) { continue }
        foreach ($part in ([string]$item -split '[,;\s]+')) {
            $p = $part.Trim().ToLowerInvariant()
            if (-not $p) { continue }
            if ($script:CategoryAliases.ContainsKey($p)) { $p = $script:CategoryAliases[$p] }
            [void]$requested.Add($p)
        }
    }
    $selected = New-Object System.Collections.ArrayList
    if ($requested.Count -eq 0) { foreach ($c in $script:Categories) { [void]$selected.Add($c) } }
    foreach ($p in $requested) {
        if ($p -eq 'all') {
            foreach ($c in $script:Categories) { [void]$selected.Add($c) }
            continue
        }
        if ($script:Categories -notcontains $p) {
            $script:UsageError = "Unknown category '" + $p + "'. Choose from: " + ($script:Categories -join ', ') + '.'
            return , @()
        }
        [void]$selected.Add($p)
    }
    if ($WifiPasswords) { [void]$selected.Add('wifi') }
    if ($FirefoxPasswords) { [void]$selected.Add('firefox') }
    if ($SteamGames) { [void]$selected.Add('steam-games') }
    $ordered = New-Object System.Collections.ArrayList
    foreach ($c in $script:Categories) { if ($selected -contains $c) { [void]$ordered.Add($c) } }
    return , $ordered.ToArray()
}

# ------------------------------------------------------------------------------------------------
# Who and where
# ------------------------------------------------------------------------------------------------
function Resolve-TransferUser {
    param([string]$Name)
    $currentName = [Environment]::UserName
    $currentProfile = [Environment]::GetFolderPath('UserProfile')
    if (-not $Name -or $Name -ieq $currentName -or $Name -ieq (Split-Path -Leaf $currentProfile)) {
        return [pscustomobject]@{ Name = $currentName; Profile = $currentProfile; IsCurrent = $true }
    }
    if ($script:SkipProfileNames -contains $Name) { return $null }
    $profilePath = ''
    $listKey = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList'
    try {
        $account = New-Object System.Security.Principal.NTAccount($Name)
        $sid = $account.Translate([System.Security.Principal.SecurityIdentifier]).Value
        $raw = Get-RegValue ($listKey + '\' + $sid) 'ProfileImagePath'
        if ($raw) { $profilePath = [Environment]::ExpandEnvironmentVariables([string]$raw) }
    } catch {
        $profilePath = ''
    }
    if (-not $profilePath) {
        foreach ($key in @(Get-ChildItem -LiteralPath $listKey -ErrorAction SilentlyContinue)) {
            $raw = Get-RegValue $key.PSPath 'ProfileImagePath'
            if ($raw -and ((Split-Path -Leaf ([string]$raw)) -ieq $Name)) {
                $profilePath = [Environment]::ExpandEnvironmentVariables([string]$raw)
                break
            }
        }
    }
    if (-not $profilePath -or -not (Test-Path -LiteralPath $profilePath -PathType Container)) { return $null }
    if ($script:SkipProfileNames -contains (Split-Path -Leaf $profilePath)) { return $null }
    return [pscustomobject]@{ Name = $Name; Profile = (Get-NormalizedPath $profilePath); IsCurrent = $false }
}

function Get-UserPaths {
    param($UserInfo)
    if ($UserInfo.IsCurrent) {
        return [pscustomobject]@{
            Local = [Environment]::GetFolderPath('LocalApplicationData')
            Roaming = [Environment]::GetFolderPath('ApplicationData')
        }
    }
    return [pscustomobject]@{
        Local = (Join-Path $UserInfo.Profile 'AppData\Local')
        Roaming = (Join-Path $UserInfo.Profile 'AppData\Roaming')
    }
}

function Get-KnownFolderPath {
    param($UserInfo, [string]$Category)
    $default = Join-Path $UserInfo.Profile $script:KnownFolderDefaults[$Category]
    if (-not $UserInfo.IsCurrent) { return $default }
    $raw = Get-RegValue 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders' $script:KnownFolderValues[$Category]
    if ($raw) {
        $expanded = [Environment]::ExpandEnvironmentVariables([string]$raw)
        if ($expanded -and (Test-Path -LiteralPath $expanded -PathType Container)) { return (Get-NormalizedPath $expanded) }
    }
    return $default
}

function Get-OneDriveRoots {
    param($UserInfo)
    $candidates = New-Object System.Collections.ArrayList
    if ($UserInfo.IsCurrent) {
        foreach ($v in @($env:OneDriveConsumer, $env:OneDriveCommercial, $env:OneDrive)) { if ($v) { [void]$candidates.Add([string]$v) } }
        foreach ($key in @(Get-ChildItem -LiteralPath 'HKCU:\Software\Microsoft\OneDrive\Accounts' -ErrorAction SilentlyContinue)) {
            $folder = Get-RegValue $key.PSPath 'UserFolder'
            if ($folder) { [void]$candidates.Add([string]$folder) }
        }
    } else {
        foreach ($d in @(Get-ChildItem -LiteralPath $UserInfo.Profile -Directory -Filter 'OneDrive*' -Force -ErrorAction SilentlyContinue)) {
            [void]$candidates.Add($d.FullName)
        }
    }
    $found = New-Object System.Collections.ArrayList
    foreach ($c in $candidates) {
        $n = Get-NormalizedPath $c
        if (-not $n -or -not (Test-Path -LiteralPath $n -PathType Container)) { continue }
        $dup = $false
        foreach ($f in $found) { if ($f -ieq $n) { $dup = $true } }
        if (-not $dup) { [void]$found.Add($n) }
    }
    return , $found.ToArray()
}

function Test-LooksLikeZipPreview {
    # Explorer runs files opened from inside a .zip from %TEMP%\Temp1_<name>.zip\...
    param([string]$Dir)
    $temp = Get-NormalizedPath ([System.IO.Path]::GetTempPath())
    return ((Test-PathInside $Dir $temp) -and ($Dir -match '\\Temp\d+_[^\\]*\.zip'))
}

function Resolve-DestinationFolder {
    param([string]$Requested)
    $base = $Requested
    if (-not $base) {
        $base = $script:ScriptDir
        if (-not $base) { $base = (Get-Location).ProviderPath }
        if (Test-LooksLikeZipPreview $base) {
            Write-Bad 'It looks like the kit was opened straight from a .zip file.'
            Write-Info 'Please extract the zip first (right-click > Extract All), copy the LindosTransfer folder'
            Write-Info 'to your USB stick and double-click LindosTransfer.cmd there.'
            return ''
        }
    }
    if (-not (Test-Path -LiteralPath $base -PathType Container)) {
        Write-Bad ('The folder "' + $base + '" does not exist. Plug in your USB stick or choose another folder with -Destination.')
        return ''
    }
    return (Get-NormalizedPath (Resolve-Path -LiteralPath $base).ProviderPath)
}

# ------------------------------------------------------------------------------------------------
# Measuring folders (never opens a file, so OneDrive never downloads anything)
# ------------------------------------------------------------------------------------------------
function Get-CloudState {
    # 'offline': robocopy /XA:O skips it. 'recall': cloud-only without the Offline bit, must be
    # excluded by name. '': the file is really on this PC.
    param([long]$Attributes)
    $recallOnDataAccess = 0x00400000
    $recallOnOpen = 0x00040000
    $offline = 0x00001000
    if (($Attributes -band $offline) -ne 0) { return 'offline' }
    if ((($Attributes -band $recallOnDataAccess) -ne 0) -or (($Attributes -band $recallOnOpen) -ne 0)) { return 'recall' }
    return ''
}

function Get-LinkKind {
    # 'Junction' / 'SymbolicLink' for real links, 'unknown' when Windows will not say, '' for
    # other reparse points (for example OneDrive folders, which must be copied).
    param($Item)
    $kind = $null
    try { $kind = $Item.LinkType } catch { return 'unknown' }
    if ($kind -eq 'Junction' -or $kind -eq 'SymbolicLink') { return [string]$kind }
    return ''
}

function Measure-TransferFolder {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [string[]]$ExcludeDirs = @(),
        [long]$MaxFileBytes = 0
    )
    $cloudOffline = New-Object System.Collections.ArrayList
    $cloudRecall = New-Object System.Collections.ArrayList
    $tooBig = New-Object System.Collections.ArrayList
    $linkDirs = New-Object System.Collections.ArrayList
    $linkFiles = New-Object System.Collections.ArrayList
    $userLinks = New-Object System.Collections.ArrayList
    $unreadable = New-Object System.Collections.ArrayList
    $files = [long]0
    $bytes = [long]0
    $otherReparse = $false
    $excluded = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($x in @($ExcludeDirs)) { if ($x) { [void]$excluded.Add((Get-NormalizedPath $x)) } }
    $stack = New-Object System.Collections.Stack
    try {
        $stack.Push((New-Object System.IO.DirectoryInfo($Root)))
    } catch {
        [void]$unreadable.Add($Root)
    }
    while ($stack.Count -gt 0) {
        $dir = $stack.Pop()
        $entries = $null
        try { $entries = $dir.GetFileSystemInfos() } catch { [void]$unreadable.Add($dir.FullName); continue }
        foreach ($entry in $entries) {
            $attr = [long]$entry.Attributes
            $isReparse = (($attr -band 0x400) -ne 0)
            if ($entry -is [System.IO.DirectoryInfo]) {
                if ($excluded.Contains((Get-NormalizedPath $entry.FullName))) { continue }
                if ($isReparse) {
                    if (Get-LinkKind $entry) {
                        # Junctions and links are never followed (no loops, no duplicates).
                        [void]$linkDirs.Add($entry.FullName)
                        if (($attr -band 0x6) -ne 0x6) { [void]$userLinks.Add($entry.FullName) }
                        continue
                    }
                    $otherReparse = $true
                }
                $stack.Push($entry)
                continue
            }
            $name = $entry.Name
            if ($name -ieq 'desktop.ini' -or $name -ieq 'Thumbs.db' -or $name -like '~$*') { continue }
            if ($isReparse) {
                if (Get-LinkKind $entry) { [void]$linkFiles.Add($entry.FullName); continue }
                $otherReparse = $true
            }
            $cloud = Get-CloudState $attr
            if ($cloud -eq 'offline') { [void]$cloudOffline.Add($entry.FullName); continue }
            if ($cloud -eq 'recall') { [void]$cloudRecall.Add($entry.FullName); continue }
            $length = [long]$entry.Length
            if ($MaxFileBytes -gt 0 -and $length -gt $MaxFileBytes) { [void]$tooBig.Add($entry.FullName); continue }
            $files++
            $bytes += $length
        }
    }
    return [pscustomobject]@{
        Root = $Root; Files = $files; Bytes = $bytes
        CloudOffline = $cloudOffline; CloudRecall = $cloudRecall; TooBig = $tooBig
        LinkDirs = $linkDirs; LinkFiles = $linkFiles; UserLinks = $userLinks
        Unreadable = $unreadable; OtherReparse = $otherReparse
    }
}

# ------------------------------------------------------------------------------------------------
# Copying folders with robocopy
# Contract (SPEC-WINDOWS 29.11): robocopy /E /COPY:DAT /DCOPY:DAT /R:1 /W:1 /XJ /XA:O /MT:8 /NP /NDL
#   /UNILOG+:<log> /XF desktop.ini Thumbs.db ~$*      (exit codes below 8 mean success)
# Never /MIR, /PURGE, /MOV, /MOVE, /B or /ZB: this kit only ever copies.
# ------------------------------------------------------------------------------------------------
function Get-RobocopyArguments {
    param(
        [string]$Source,
        [string]$Target,
        [string]$LogPath,
        $Scan = $null,
        [string[]]$ExcludeDirs = @(),
        [long]$MaxFileBytes = 0
    )
    # /XJ is used unless the folder holds reparse points that are not links (OneDrive folders
    # and files): robocopy may treat those as junctions, so then the real links found while
    # measuring are excluded one by one instead (/XD, /XF) and nothing is followed either way.
    $useXJ = -not ($null -ne $Scan -and [bool](Get-PropValue $Scan 'OtherReparse'))
    $list = New-Object System.Collections.ArrayList
    [void]$list.Add($Source)
    [void]$list.Add($Target)
    foreach ($flag in @('/E', '/COPY:DAT', '/DCOPY:DAT', '/R:1', '/W:1')) { [void]$list.Add($flag) }
    if ($useXJ) { [void]$list.Add('/XJ') }
    foreach ($flag in @('/XA:O', '/MT:8', '/NP', '/NDL')) { [void]$list.Add($flag) }
    [void]$list.Add('/UNILOG+:' + $LogPath)
    if ($MaxFileBytes -gt 0) {
        [void]$list.Add('/MAX:' + $MaxFileBytes)
        [void]$list.Add('/FFT')
    }
    [void]$list.Add('/XF')
    foreach ($pattern in @('desktop.ini', 'Thumbs.db', '~$*')) { [void]$list.Add($pattern) }
    if ($null -ne $Scan) {
        foreach ($f in @(Get-PropValue $Scan 'CloudRecall')) { if ($f) { [void]$list.Add([string]$f) } }
        if (-not $useXJ) { foreach ($f in @(Get-PropValue $Scan 'LinkFiles')) { if ($f) { [void]$list.Add([string]$f) } } }
    }
    $dirs = New-Object System.Collections.ArrayList
    foreach ($d in @($ExcludeDirs)) { if ($d) { [void]$dirs.Add([string]$d) } }
    if ($null -ne $Scan -and -not $useXJ) { foreach ($d in @(Get-PropValue $Scan 'LinkDirs')) { if ($d) { [void]$dirs.Add([string]$d) } } }
    if ($dirs.Count -gt 0) {
        [void]$list.Add('/XD')
        foreach ($d in $dirs) { [void]$list.Add($d) }
    }
    return , $list.ToArray()
}

function Invoke-RobocopyWithProgress {
    param([string[]]$Arguments, [string]$Label, [long]$ExpectedBytes, [string]$DestinationRoot)
    $exe = Join-Path $script:WindowsDir 'System32\robocopy.exe'
    $line = Join-CommandLine $Arguments
    $free0 = Get-FreeBytes $DestinationRoot
    $started = Get-Date
    $proc = Start-Process -FilePath $exe -ArgumentList $line -NoNewWindow -PassThru
    $null = $proc.Handle
    $activity = 'Copying ' + $Label
    while (-not $proc.WaitForExit(1000)) {
        $elapsed = ((Get-Date) - $started).ToString('hh\:mm\:ss')
        $percent = -1
        $status = 'Still copying... (' + $elapsed + ' so far)'
        if ($free0 -ge 0 -and $ExpectedBytes -gt 0) {
            # Progress = how much the free space on the target drive went down (cheap and good enough).
            $now = Get-FreeBytes $DestinationRoot
            if ($now -ge 0) {
                $written = [Math]::Max([long]0, $free0 - $now)
                $percent = [int][Math]::Min(99, [Math]::Floor($written * 100.0 / $ExpectedBytes))
                $status = 'About ' + $percent + '% (' + (Format-Size $written) + ' of ' + (Format-Size $ExpectedBytes) + ', ' + $elapsed + ' so far)'
            }
        }
        Write-Progress -Activity $activity -Status $status -PercentComplete $percent
    }
    Write-Progress -Activity $activity -Completed
    return [int]$proc.ExitCode
}

function Copy-PlannedFolder {
    param($Entry, [long]$MaxFileBytes)
    $target = Join-Path $script:OutDir $Entry.Target
    $log = Join-Path $script:OutDir 'robocopy-log.txt'
    $arguments = (Get-RobocopyArguments -Source $Entry.Source -Target $target -LogPath $log -Scan $Entry.Scan -ExcludeDirs $Entry.Exclude -MaxFileBytes $MaxFileBytes)
    if ((Join-CommandLine $arguments).Length -gt $script:MaxCommandLine) {
        # Too many cloud-only files to list by name: copying would make Windows download them.
        Write-Warn ($Entry.Label + ' was not copied: it holds too many files that are only in the cloud.')
        Write-Info 'In OneDrive, right-click the folder and choose "Always keep on this device", then run the kit again.'
        Add-Skipped $Entry.Source 'folder has too many online-only files to skip one by one; make them available offline first'
        $script:Failures++
        return $false
    }
    Write-Info ('Copying ' + $Entry.Label + ' (' + (Format-Count $Entry.Scan.Files) + ' files, ' + (Format-Size $Entry.Scan.Bytes) + ')...')
    $code = Invoke-RobocopyWithProgress -Arguments $arguments -Label $Entry.Label -ExpectedBytes $Entry.Scan.Bytes -DestinationRoot $script:OutDir
    if ($code -lt 8) {
        Write-Good ($Entry.Label + ' copied.')
        return $true
    }
    if ($code -lt 16) {
        Write-Warn ('Some files in ' + $Entry.Label + ' could not be copied (details in robocopy-log.txt).')
        Add-Skipped $Entry.Source 'some files could not be copied (details in robocopy-log.txt)'
        $script:Failures++
        return $true
    }
    Write-Bad ('Copying ' + $Entry.Label + ' failed (robocopy error ' + $code + '; details in robocopy-log.txt).')
    Add-Skipped $Entry.Source ('copy failed (robocopy error ' + $code + ')')
    $script:Failures++
    return $false
}

function Add-ScanSkipped {
    param($Entry, [long]$MaxFileBytes)
    $scan = $Entry.Scan
    foreach ($f in $scan.CloudOffline) { Add-Skipped $f 'online-only' $Entry.Source }
    foreach ($f in $scan.CloudRecall) { Add-Skipped $f 'online-only' $Entry.Source }
    foreach ($f in $scan.TooBig) {
        if ($MaxFileBytes -eq $script:Fat32MaxFileBytes) {
            Add-Skipped $f 'larger than 4 GB, which a FAT32 drive cannot store (use an exFAT or NTFS drive)' $Entry.Source
        } else {
            Add-Skipped $f 'too large for the file system of the target drive (use an exFAT or NTFS drive)' $Entry.Source
        }
    }
    foreach ($d in $scan.UserLinks) { Add-Skipped $d 'a link to another folder (not followed; copy the real folder instead)' $Entry.Source }
    foreach ($d in $scan.Unreadable) { Add-Skipped $d 'could not be read (no permission)' $Entry.Source }
}

# ------------------------------------------------------------------------------------------------
# Browsers: bookmark files only. Saved passwords, cookies and payment data are never read.
# ------------------------------------------------------------------------------------------------
function Get-ChromiumSources {
    param($Paths)
    return , @(
        [pscustomobject]@{ Id = 'chrome'; Label = 'Google Chrome'; Root = (Join-Path $Paths.Local 'Google\Chrome\User Data'); Flat = $false },
        [pscustomobject]@{ Id = 'edge'; Label = 'Microsoft Edge'; Root = (Join-Path $Paths.Local 'Microsoft\Edge\User Data'); Flat = $false },
        [pscustomobject]@{ Id = 'brave'; Label = 'Brave'; Root = (Join-Path $Paths.Local 'BraveSoftware\Brave-Browser\User Data'); Flat = $false },
        [pscustomobject]@{ Id = 'vivaldi'; Label = 'Vivaldi'; Root = (Join-Path $Paths.Local 'Vivaldi\User Data'); Flat = $false },
        [pscustomobject]@{ Id = 'opera'; Label = 'Opera'; Root = (Join-Path $Paths.Roaming 'Opera Software\Opera Stable'); Flat = $true },
        [pscustomobject]@{ Id = 'opera-gx'; Label = 'Opera GX'; Root = (Join-Path $Paths.Roaming 'Opera Software\Opera GX Stable'); Flat = $true }
    )
}

function Get-ChromiumProfileDirs {
    param($Source)
    $dirs = New-Object System.Collections.ArrayList
    if (-not (Test-Path -LiteralPath $Source.Root -PathType Container)) { return , $dirs.ToArray() }
    if ($Source.Flat) {
        $leaf = Split-Path -Leaf $Source.Root
        [void]$dirs.Add([pscustomobject]@{ Profile = $leaf; Path = $Source.Root })
        $sub = Join-Path $Source.Root 'Default'
        if (Test-Path -LiteralPath $sub -PathType Container) { [void]$dirs.Add([pscustomobject]@{ Profile = ($leaf + ' Default'); Path = $sub }) }
        return , $dirs.ToArray()
    }
    foreach ($d in @(Get-ChildItem -LiteralPath $Source.Root -Directory -Force -ErrorAction SilentlyContinue)) {
        if ($d.Name -ne 'Default' -and $d.Name -notlike 'Profile *') { continue }
        [void]$dirs.Add([pscustomobject]@{ Profile = $d.Name; Path = $d.FullName })
    }
    return , $dirs.ToArray()
}

function ConvertFrom-JsonText {
    # JavaScriptSerializer copes with large files and keys that differ only in case, which
    # ConvertFrom-Json in PowerShell 5.1 does not.
    param([string]$Text)
    try {
        Add-Type -AssemblyName System.Web.Extensions -ErrorAction Stop
        $serializer = New-Object System.Web.Script.Serialization.JavaScriptSerializer
        $serializer.MaxJsonLength = [int]::MaxValue
        $serializer.RecursionLimit = 256
        return $serializer.DeserializeObject($Text)
    } catch {
        return (ConvertFrom-Json -InputObject $Text)
    }
}

function Get-ChromiumProfileName {
    # The profile's display name ("Person 1") from its Preferences file; only that one value is used.
    param([string]$ProfileDir, [string]$Fallback)
    $prefs = Join-Path $ProfileDir 'Preferences'
    try {
        $info = Get-Item -LiteralPath $prefs -Force -ErrorAction Stop
        if ($info.Length -gt 20MB) { return $Fallback }
        $data = ConvertFrom-JsonText ([System.IO.File]::ReadAllText($prefs, [System.Text.Encoding]::UTF8))
        $name = Get-PropValue (Get-PropValue $data 'profile') 'name'
        if ($name -is [string] -and $name.Trim()) { return $name.Trim() }
    } catch {
        return $Fallback
    }
    return $Fallback
}

function Copy-ChromiumBookmarks {
    param($Paths)
    $entries = New-Object System.Collections.ArrayList
    foreach ($source in (Get-ChromiumSources $Paths)) {
        foreach ($prof in (Get-ChromiumProfileDirs $source)) {
            $plain = Join-Path $prof.Path 'Bookmarks'
            $account = Join-Path $prof.Path 'AccountBookmarks'
            $hasPlain = Test-Path -LiteralPath $plain -PathType Leaf
            $hasAccount = Test-Path -LiteralPath $account -PathType Leaf
            if (-not $hasPlain -and -not $hasAccount) {
                # Only the file names are looked at here; encrypted bookmark files are never opened.
                $encrypted = @(Get-ChildItem -LiteralPath $prof.Path -Filter 'Encrypted*Bookmarks*' -File -Force -ErrorAction SilentlyContinue)
                if ($encrypted.Count -gt 0) {
                    Write-Warn ($source.Label + ' (' + $prof.Profile + ') keeps its bookmarks encrypted for this PC.')
                    Write-Info ('Open ' + $source.Label + ', choose Bookmarks > Export bookmarks, and save the file into your Documents.')
                    Add-Skipped $prof.Path ('bookmarks are encrypted by ' + $source.Label + '; use Export bookmarks in the browser')
                }
                continue
            }
            $rel = 'browsers/' + $source.Id + '/' + $prof.Profile
            $destDir = Join-Path $script:OutDir ($rel -replace '/', '\')
            $name = Get-ChromiumProfileName $prof.Path $prof.Profile
            if ($script:DryRun) {
                Write-Info ('Would copy the bookmarks of ' + $source.Label + ' (' + $name + ').')
            } else {
                New-Item -ItemType Directory -Path $destDir -Force | Out-Null
                if ($hasPlain) { Copy-Item -LiteralPath $plain -Destination (Join-Path $destDir 'Bookmarks') -Force }
                if ($hasAccount) { Copy-Item -LiteralPath $account -Destination (Join-Path $destDir 'AccountBookmarks') -Force }
                Write-Good ('Bookmarks of ' + $source.Label + ' (' + $name + ') copied.')
            }
            $bookmarks = $rel + '/AccountBookmarks'
            if ($hasPlain) { $bookmarks = $rel + '/Bookmarks' }
            [void]$entries.Add([ordered]@{ browser = $source.Id; profile = $prof.Profile; name = $name; bookmarks = $bookmarks })
        }
    }
    return , $entries.ToArray()
}

function ConvertFrom-IniText {
    param([string]$Text)
    $sections = [ordered]@{}
    $current = $null
    foreach ($raw in ($Text -split "`r?`n")) {
        $line = $raw.Trim()
        if (-not $line -or $line.StartsWith(';') -or $line.StartsWith('#')) { continue }
        if ($line -match '^\[(.+)\]$') {
            $current = $Matches[1].Trim()
            if (-not $sections.Contains($current)) { $sections[$current] = @{} }
            continue
        }
        if ($null -ne $current -and $line -match '^([^=]+)=(.*)$') {
            $sections[$current][$Matches[1].Trim()] = $Matches[2].Trim()
        }
    }
    return $sections
}

function Get-FirefoxProfiles {
    param($Paths)
    $roots = New-Object System.Collections.ArrayList
    [void]$roots.Add((Join-Path $Paths.Roaming 'Mozilla\Firefox'))
    # Firefox from the Microsoft Store keeps its profiles inside its package folder.
    foreach ($pkg in @(Get-ChildItem -LiteralPath (Join-Path $Paths.Local 'Packages') -Directory -Filter 'Mozilla.Firefox_*' -ErrorAction SilentlyContinue)) {
        [void]$roots.Add((Join-Path $pkg.FullName 'LocalCache\Roaming\Mozilla\Firefox'))
    }
    $result = New-Object System.Collections.ArrayList
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
    $ids = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($root in $roots) {
        if (-not (Test-Path -LiteralPath $root -PathType Container)) { continue }
        $dirs = New-Object System.Collections.ArrayList
        $ini = Join-Path $root 'profiles.ini'
        if (Test-Path -LiteralPath $ini -PathType Leaf) {
            $data = ConvertFrom-IniText ([System.IO.File]::ReadAllText($ini))
            foreach ($section in @($data.Keys)) {
                if ($section -notlike 'Profile*') { continue }
                $values = $data[$section]
                $path = [string](Get-PropValue $values 'Path')
                if (-not $path) { continue }
                $path = $path -replace '/', '\'
                if ([string](Get-PropValue $values 'IsRelative') -eq '1') { $path = Join-Path $root $path }
                [void]$dirs.Add($path)
            }
        }
        if ($dirs.Count -eq 0) {
            foreach ($d in @(Get-ChildItem -LiteralPath (Join-Path $root 'Profiles') -Directory -ErrorAction SilentlyContinue)) { [void]$dirs.Add($d.FullName) }
        }
        foreach ($d in $dirs) {
            $full = Get-NormalizedPath $d
            if (-not (Test-Path -LiteralPath (Join-Path $full 'places.sqlite') -PathType Leaf)) { continue }
            if (-not $seen.Add($full)) { continue }
            $id = Split-Path -Leaf $full
            $n = 2
            while (-not $ids.Add($id)) { $id = (Split-Path -Leaf $full) + '-' + $n; $n++ }
            [void]$result.Add([pscustomobject]@{ Id = $id; Path = $full })
        }
    }
    return , $result.ToArray()
}

function Copy-FirefoxData {
    param($Paths)
    $entries = New-Object System.Collections.ArrayList
    foreach ($prof in (Get-FirefoxProfiles $Paths)) {
        $rel = 'browsers/firefox/' + $prof.Id
        $destDir = Join-Path $script:OutDir ($rel -replace '/', '\')
        if ($script:DryRun) {
            Write-Info ('Would copy Firefox bookmarks and history (' + $prof.Id + ').')
        } else {
            New-Item -ItemType Directory -Path $destDir -Force | Out-Null
            $ok = $true
            # Bookmarks and history (+ the write-ahead logs so nothing recent is lost) and site icons.
            foreach ($file in @('places.sqlite', 'places.sqlite-wal', 'favicons.sqlite', 'favicons.sqlite-wal')) {
                $src = Join-Path $prof.Path $file
                if (-not (Test-Path -LiteralPath $src -PathType Leaf)) { continue }
                try {
                    Copy-Item -LiteralPath $src -Destination (Join-Path $destDir $file) -Force
                } catch {
                    $ok = $false
                    Write-Warn ('Could not copy ' + $file + ' of Firefox (' + $prof.Id + '). Close Firefox and run the kit again.')
                    Add-Skipped $src 'could not be copied (Firefox was probably open)'
                }
            }
            if (-not (Test-Path -LiteralPath (Join-Path $destDir 'places.sqlite') -PathType Leaf)) {
                Remove-OwnFile (Join-Path $destDir 'places.sqlite-wal')
                Remove-OwnFile (Join-Path $destDir 'favicons.sqlite')
                Remove-OwnFile (Join-Path $destDir 'favicons.sqlite-wal')
                Remove-OwnFile $destDir
                $script:Failures++
                continue
            }
            if ($script:FirefoxPasswords) { Copy-FirefoxPasswordFiles -ProfileDir $prof.Path -DestDir $destDir }
            if ($ok) { Write-Good ('Firefox bookmarks and history (' + $prof.Id + ') copied.') }
        }
        [void]$entries.Add([ordered]@{ browser = 'firefox'; profile = $prof.Id; path = $rel })
    }
    return , $entries.ToArray()
}

# BEGIN OPT-IN: firefox-passwords (only reached when -IncludeFirefoxPasswords was confirmed)
function Copy-FirefoxPasswordFiles {
    # Firefox protects these with its own Primary Password (not with Windows), so they are
    # copied unchanged and still need that password on Lindos. Never decrypted here.
    param([string]$ProfileDir, [string]$DestDir)
    foreach ($file in @('key4.db', 'logins.json')) {
        $src = Join-Path $ProfileDir $file
        if (-not (Test-Path -LiteralPath $src -PathType Leaf)) { continue }
        try {
            Copy-Item -LiteralPath $src -Destination (Join-Path $DestDir $file) -Force
        } catch {
            Write-Warn ('Could not copy the Firefox password file ' + $file + '.')
        }
    }
}
# END OPT-IN: firefox-passwords

# ------------------------------------------------------------------------------------------------
# Wallpaper and fonts (only your own; never the ones that come with Windows)
# ------------------------------------------------------------------------------------------------
function Get-WallpaperStyleName {
    param([string]$Style, [string]$Tile, [bool]$FromPolicy)
    $s = ([string]$Style).Trim()
    if ($FromPolicy) {
        # Group Policy "Desktop Wallpaper" uses its own numbers.
        switch ($s) {
            '0' { return 'center' }
            '1' { return 'tile' }
            '2' { return 'stretch' }
            '3' { return 'fit' }
            '4' { return 'fill' }
            '5' { return 'span' }
        }
        return 'fill'
    }
    switch ($s) {
        '0' { if (([string]$Tile).Trim() -eq '1') { return 'tile' } else { return 'center' } }
        '2' { return 'stretch' }
        '6' { return 'fit' }
        '10' { return 'fill' }
        '22' { return 'span' }
    }
    return 'fill'
}

function Get-ImageExtension {
    param([byte[]]$Head)
    if ($null -eq $Head -or $Head.Length -lt 2) { return '' }
    if ($Head.Length -ge 3 -and $Head[0] -eq 0xFF -and $Head[1] -eq 0xD8 -and $Head[2] -eq 0xFF) { return 'jpg' }
    if ($Head.Length -ge 4 -and $Head[0] -eq 0x89 -and $Head[1] -eq 0x50 -and $Head[2] -eq 0x4E -and $Head[3] -eq 0x47) { return 'png' }
    if ($Head[0] -eq 0x42 -and $Head[1] -eq 0x4D) { return 'bmp' }
    return ''
}

function Read-FileHead {
    param([string]$Path, [int]$Count)
    $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
    try {
        $buffer = New-Object byte[] $Count
        $read = $stream.Read($buffer, 0, $Count)
        $result = New-Object byte[] ([Math]::Max(0, $read))
        if ($read -gt 0) { [Array]::Copy($buffer, $result, $read) }
        return , $result
    } finally {
        $stream.Dispose()
    }
}

function Test-StockWallpaper {
    # Pictures that come with Windows (and Windows Spotlight) stay on Windows: Lindos has its own.
    param([string]$Path)
    if (-not $Path) { return $false }
    if (Test-PathInside $Path (Join-Path $script:WindowsDir 'Web')) { return $true }
    return ($Path -match 'IrisService|ContentDeliveryManager|MicrosoftWindows\.Client\.CBS_')
}

function Copy-Wallpaper {
    param($UserInfo, $Paths)
    if (-not $UserInfo.IsCurrent) {
        Write-Info 'The wallpaper is only copied when you run the kit as that person.'
        return $null
    }
    $policyPath = [string](Get-RegValue 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Policies\System' 'Wallpaper')
    if ($policyPath) {
        $original = $policyPath
        $style = Get-WallpaperStyleName ([string](Get-RegValue 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Policies\System' 'WallpaperStyle')) '' $true
    } else {
        $original = [string](Get-RegValue 'HKCU:\Control Panel\Desktop' 'WallPaper')
        $style = Get-WallpaperStyleName ([string](Get-RegValue 'HKCU:\Control Panel\Desktop' 'WallpaperStyle')) ([string](Get-RegValue 'HKCU:\Control Panel\Desktop' 'TileWallpaper')) $false
    }
    if (-not $original) {
        Write-Info 'Your desktop uses a plain colour, so there is no wallpaper to copy.'
        return $null
    }
    if (Test-StockWallpaper $original) {
        Write-Info 'Your wallpaper is one of the pictures that come with Windows; Lindos uses its own instead.'
        return $null
    }
    $transcoded = Join-Path $Paths.Roaming 'Microsoft\Windows\Themes\TranscodedWallpaper'
    if (-not (Test-Path -LiteralPath $transcoded -PathType Leaf)) {
        Write-Info 'Windows has no copy of your wallpaper picture, so it is not copied.'
        return $null
    }
    $ext = Get-ImageExtension (Read-FileHead $transcoded 8)
    if (-not $ext) {
        Add-Skipped $transcoded 'wallpaper is in a picture format Lindos does not recognise'
        return $null
    }
    $fileName = 'wallpaper.' + $ext
    if ($script:DryRun) {
        Write-Info ('Would copy your wallpaper (' + $style + ').')
    } else {
        Copy-Item -LiteralPath $transcoded -Destination (Join-Path $script:OutDir $fileName) -Force
        Write-Good 'Wallpaper copied.'
    }
    return [pscustomobject]@{ File = $fileName; Style = $style }
}

function Test-IsWindowsFontsPath {
    # The fonts that come with Windows are licensed to this PC only and are never copied.
    param([string]$Path)
    return (Test-PathInside $Path (Join-Path $script:WindowsDir 'Fonts'))
}

function Copy-UserFonts {
    param($Paths)
    $dir = Join-Path $Paths.Local 'Microsoft\Windows\Fonts'
    if (Test-IsWindowsFontsPath $dir) {
        Write-Warn 'Not copying fonts: that folder holds the fonts that come with Windows.'
        return 0
    }
    if (-not (Test-Path -LiteralPath $dir -PathType Container)) { return 0 }
    $fonts = @(Get-ChildItem -LiteralPath $dir -File -Force -ErrorAction SilentlyContinue |
        Where-Object { $script:FontExtensions -contains $_.Extension.ToLowerInvariant() })
    if ($fonts.Count -eq 0) { return 0 }
    if ($script:DryRun) {
        Write-Info ('Would copy ' + $fonts.Count + ' font(s) you installed yourself.')
        return $fonts.Count
    }
    $target = Join-Path $script:OutDir 'fonts'
    New-Item -ItemType Directory -Path $target -Force | Out-Null
    $copied = 0
    foreach ($font in $fonts) {
        if ((Get-CloudState ([long]$font.Attributes)) -ne '') { Add-Skipped $font.FullName 'online-only'; continue }
        try {
            Copy-Item -LiteralPath $font.FullName -Destination (Join-Path $target $font.Name) -Force
            $copied++
        } catch {
            Add-Skipped $font.FullName 'could not be copied'
        }
    }
    if ($copied -eq 0) { Remove-OwnFile $target } else { Write-Good ('' + $copied + ' font(s) copied. Check that their licences allow use on another computer.') }
    return $copied
}

# ------------------------------------------------------------------------------------------------
# Wi-Fi: Microsoft's own "netsh wlan export". Passwords only with the explicit opt-in.
# ------------------------------------------------------------------------------------------------
function Test-IsAdministrator {
    try {
        $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = New-Object System.Security.Principal.WindowsPrincipal($identity)
        return $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch {
        return $false
    }
}

function Invoke-Netsh {
    param([string]$Netsh, [string]$ArgumentLine)
    $out = New-TempFile
    try {
        $proc = Start-Process -FilePath $Netsh -ArgumentList $ArgumentLine -NoNewWindow -PassThru -RedirectStandardOutput $out
        $null = $proc.Handle
        $proc.WaitForExit()
        $text = ''
        try { $text = [System.IO.File]::ReadAllText($out) } catch { $text = '' }
        return [pscustomobject]@{ ExitCode = [int]$proc.ExitCode; Output = $text }
    } finally {
        Remove-OwnFile $out
    }
}

# BEGIN OPT-IN: wifi-passwords (only reached when -IncludeWifiPasswords was confirmed)
function Invoke-WifiPasswordExport {
    # The only step of the kit that asks Windows for administrator permission: Windows puts the
    # passwords into the export only for an administrator ("key=clear").
    param([string]$Netsh, [string]$Folder)
    if ($Folder.StartsWith('\\')) {
        Write-Warn 'Windows cannot export Wi-Fi passwords into a network folder. Use a USB stick or disk.'
        return $false
    }
    $line = 'wlan export profile key=clear folder="' + $Folder + '"'
    try {
        if (Test-IsAdministrator) {
            $result = Invoke-Netsh -Netsh $Netsh -ArgumentLine $line
            return ($result.ExitCode -eq 0)
        }
        Write-Info 'Windows will now ask for administrator permission (for this Wi-Fi step only).'
        $proc = Start-Process -FilePath $Netsh -ArgumentList $line -Verb RunAs -WindowStyle Hidden -Wait -PassThru
        return ($null -ne $proc -and $proc.ExitCode -eq 0)
    } catch {
        Write-Warn 'Administrator permission was not given, so Wi-Fi passwords are not included (the network names still are).'
        return $false
    }
}
# END OPT-IN: wifi-passwords

function Update-WifiExportFiles {
    # Counts the networks whose password Windows really exported in plain text
    # (<protected>false</protected>). Windows-encrypted passwords cannot be used on another PC,
    # so their encrypted blob is removed from the copy; Lindos will ask for the password once.
    param([string]$Folder)
    $total = 0
    $clear = 0
    $protectedCount = 0
    foreach ($file in @(Get-ChildItem -LiteralPath $Folder -Filter '*.xml' -File -ErrorAction SilentlyContinue)) {
        $total++
        try {
            $doc = New-Object System.Xml.XmlDocument
            $doc.XmlResolver = $null
            $doc.PreserveWhitespace = $true
            $doc.Load($file.FullName)
            $changed = $false
            foreach ($shared in @($doc.SelectNodes("//*[local-name()='sharedKey']"))) {
                $flag = $shared.SelectSingleNode("*[local-name()='protected']")
                $isProtected = $true
                if ($null -ne $flag -and $flag.InnerText.Trim() -ieq 'false') { $isProtected = $false }
                if ($isProtected) {
                    $protectedCount++
                    foreach ($material in @($shared.SelectNodes("*[local-name()='keyMaterial']"))) {
                        [void]$shared.RemoveChild($material)
                        $changed = $true
                    }
                } else {
                    $clear++
                }
            }
            if ($changed) { $doc.Save($file.FullName) }
        } catch {
            Write-Warn ('Could not read the Wi-Fi export ' + $file.Name + ': ' + $_.Exception.Message)
        }
    }
    return [pscustomobject]@{ Total = $total; Clear = $clear; Protected = $protectedCount }
}

function Export-WifiProfiles {
    param([bool]$WithKeys)
    $netsh = Join-Path $script:WindowsDir 'System32\netsh.exe'
    if ($script:DryRun) {
        if ($WithKeys) { Write-Info 'Would export your Wi-Fi networks with their passwords (asks for administrator permission).' }
        else { Write-Info 'Would export your Wi-Fi network names (no passwords).' }
        return $null
    }
    $folder = Join-Path $script:OutDir 'wifi'
    New-Item -ItemType Directory -Path $folder -Force | Out-Null
    $exported = $false
    if ($WithKeys) { $exported = Invoke-WifiPasswordExport -Netsh $netsh -Folder $folder }
    if (-not $exported -or @(Get-ChildItem -LiteralPath $folder -Filter '*.xml' -File -ErrorAction SilentlyContinue).Count -eq 0) {
        $null = Invoke-Netsh -Netsh $netsh -ArgumentLine ('wlan export profile folder="' + $folder + '"')
    }
    $counts = Update-WifiExportFiles -Folder $folder
    if ($counts.Total -eq 0) {
        Remove-OwnFile $folder
        Write-Info 'No saved Wi-Fi networks were found on this PC.'
        return $null
    }
    $withKeys = ($WithKeys -and $counts.Clear -gt 0)
    if ($withKeys) {
        $note = @(
            'These files contain Wi-Fi passwords in plain text.',
            'Keep this folder private and delete it after importing on Lindos.',
            'Deleting from a USB stick does not securely erase it.')
        $note -join "`r`n" | Out-File -LiteralPath (Join-Path $folder 'PRIVATE - Wi-Fi passwords.txt') -Encoding ascii
        Write-Good ('' + $counts.Total + ' Wi-Fi network(s) exported, ' + $counts.Clear + ' with password.')
        if ($counts.Protected -gt 0) { Write-Info ('Windows kept ' + $counts.Protected + ' password(s) encrypted; Lindos will ask for those once.') }
    } else {
        Write-Good ('' + $counts.Total + ' Wi-Fi network name(s) exported (no passwords; Lindos asks once per network).')
        if ($WithKeys) { Write-Warn 'Windows did not include any passwords in the export.' }
    }
    return [ordered]@{ dir = 'wifi'; with_keys = [bool]$withKeys }
}

# ------------------------------------------------------------------------------------------------
# Steam: library list always; game folders only with -IncludeSteamGames
# ------------------------------------------------------------------------------------------------
function Get-VdfValue {
    param([string]$Text, [string]$Key)
    $m = [regex]::Match($Text, '"' + [regex]::Escape($Key) + '"\s+"((?:[^"\\]|\\.)*)"', 'IgnoreCase')
    if (-not $m.Success) { return '' }
    return (($m.Groups[1].Value -replace '\\\\', '\') -replace '\\"', '"')
}

function Get-SteamRoot {
    param($UserInfo)
    $candidates = New-Object System.Collections.ArrayList
    if ($UserInfo.IsCurrent) { [void]$candidates.Add([string](Get-RegValue 'HKCU:\Software\Valve\Steam' 'SteamPath')) }
    [void]$candidates.Add([string](Get-RegValue 'HKLM:\SOFTWARE\WOW6432Node\Valve\Steam' 'InstallPath'))
    [void]$candidates.Add([string](Get-RegValue 'HKLM:\SOFTWARE\Valve\Steam' 'InstallPath'))
    $pf86 = [Environment]::GetFolderPath('ProgramFilesX86')
    if ($pf86) { [void]$candidates.Add((Join-Path $pf86 'Steam')) }
    foreach ($c in $candidates) {
        if (-not $c) { continue }
        $p = $c -replace '/', '\'
        if (Test-Path -LiteralPath (Join-Path $p 'steamapps') -PathType Container) { return (Get-NormalizedPath $p) }
    }
    return ''
}

function Get-SteamLibraryPaths {
    param([string]$VdfText, [string]$SteamRoot)
    $list = New-Object System.Collections.ArrayList
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
    if ($SteamRoot -and $seen.Add((Get-NormalizedPath $SteamRoot))) { [void]$list.Add((Get-NormalizedPath $SteamRoot)) }
    $found = New-Object System.Collections.ArrayList
    foreach ($m in [regex]::Matches([string]$VdfText, '"path"\s+"((?:[^"\\]|\\.)*)"', 'IgnoreCase')) { [void]$found.Add($m.Groups[1].Value) }
    # Before 2021 the file was just "1" "D:\\SteamLibrary".
    foreach ($m in [regex]::Matches([string]$VdfText, '"\d+"\s+"([A-Za-z]:\\\\(?:[^"\\]|\\.)*)"')) { [void]$found.Add($m.Groups[1].Value) }
    foreach ($raw in $found) {
        $p = Get-NormalizedPath (($raw -replace '\\\\', '\') -replace '\\"', '"')
        if ($p -and $seen.Add($p)) { [void]$list.Add($p) }
    }
    return , $list.ToArray()
}

function Test-SafeFolderName {
    param([string]$Name)
    if (-not $Name -or $Name -eq '.' -or $Name -eq '..') { return $false }
    return ($Name -notmatch '[\\/:*?"<>|]')
}

function Get-SteamPlan {
    param($UserInfo)
    $root = Get-SteamRoot $UserInfo
    if (-not $root) { return $null }
    $vdf = Join-Path $root 'steamapps\libraryfolders.vdf'
    $text = ''
    if (Test-Path -LiteralPath $vdf -PathType Leaf) { $text = [System.IO.File]::ReadAllText($vdf) }
    $games = New-Object System.Collections.ArrayList
    foreach ($lib in (Get-SteamLibraryPaths -VdfText $text -SteamRoot $root)) {
        foreach ($acf in @(Get-ChildItem -LiteralPath (Join-Path $lib 'steamapps') -Filter 'appmanifest_*.acf' -File -ErrorAction SilentlyContinue)) {
            $acfText = ''
            try { $acfText = [System.IO.File]::ReadAllText($acf.FullName) } catch { continue }
            [void]$games.Add([pscustomobject]@{
                    Acf = $acf.FullName
                    AppId = (Get-VdfValue $acfText 'appid')
                    Name = (Get-VdfValue $acfText 'name')
                    InstallDir = (Get-VdfValue $acfText 'installdir')
                    Library = $lib
                })
        }
    }
    return [pscustomobject]@{ Root = $root; Vdf = $vdf; Games = $games.ToArray() }
}

function Copy-SteamFiles {
    param($SteamPlan)
    $target = Join-Path $script:OutDir 'steam'
    if ($script:DryRun) {
        Write-Info ('Would copy your Steam library list (' + @($SteamPlan.Games).Count + ' installed game(s)).')
        return $true
    }
    New-Item -ItemType Directory -Path $target -Force | Out-Null
    if (Test-Path -LiteralPath $SteamPlan.Vdf -PathType Leaf) { Copy-Item -LiteralPath $SteamPlan.Vdf -Destination (Join-Path $target 'libraryfolders.vdf') -Force }
    foreach ($game in @($SteamPlan.Games)) {
        Copy-Item -LiteralPath $game.Acf -Destination (Join-Path $target (Split-Path -Leaf $game.Acf)) -Force
    }
    Write-Good ('Steam library list copied (' + @($SteamPlan.Games).Count + ' installed game(s)).')
    return $true
}

# ------------------------------------------------------------------------------------------------
# Apps: names from the three "Programs and Features" lists; nothing is copied or uninstalled.
# ------------------------------------------------------------------------------------------------
function Test-KeepApp {
    param($Entry)
    $name = ([string](Get-PropValue $Entry 'DisplayName')).Trim()
    if (-not $name) { return $false }
    if (([string](Get-PropValue $Entry 'SystemComponent')).Trim() -eq '1') { return $false }
    if ([string](Get-PropValue $Entry 'ParentKeyName')) { return $false }
    if (([string](Get-PropValue $Entry 'ReleaseType')).Trim() -match '^(?i:Update|Hotfix|Security Update|Service Pack|Update Rollup)$') { return $false }
    if ($name -match '\bKB\d{5,}\b') { return $false }
    if ($name -match $script:RuntimeNamePattern) { return $false }
    return $true
}

function ConvertTo-AppRecord {
    param($Entry, [string]$Scope, [string]$Arch, [bool]$Is64)
    $location = ([string](Get-PropValue $Entry 'InstallLocation')).Trim().Trim('"')
    if (-not $Arch) {
        # Per-user installs do not say; "(x86)" in their paths is the only hint.
        $icon = [string](Get-PropValue $Entry 'DisplayIcon')
        if ($location -match '\(x86\)' -or $icon -match '\(x86\)' -or -not $Is64) { $Arch = 'x86' } else { $Arch = 'x64' }
    }
    return [ordered]@{
        name = ([string](Get-PropValue $Entry 'DisplayName')).Trim()
        version = ([string](Get-PropValue $Entry 'DisplayVersion')).Trim()
        publisher = ([string](Get-PropValue $Entry 'Publisher')).Trim()
        install_location = $location
        scope = $Scope
        arch = $Arch
    }
}

function Get-InstalledApps {
    param([bool]$IncludeCurrentUser)
    $is64 = [Environment]::Is64BitOperatingSystem
    $sources = New-Object System.Collections.ArrayList
    $nativeArch = 'x86'
    if ($is64) { $nativeArch = 'x64' }
    [void]$sources.Add(@{ Path = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'; Scope = 'machine'; Arch = $nativeArch })
    if ($is64) { [void]$sources.Add(@{ Path = 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'; Scope = 'machine'; Arch = 'x86' }) }
    if ($IncludeCurrentUser) { [void]$sources.Add(@{ Path = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall'; Scope = 'user'; Arch = '' }) }
    $apps = New-Object System.Collections.ArrayList
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($source in $sources) {
        foreach ($key in @(Get-ChildItem -LiteralPath $source.Path -ErrorAction SilentlyContinue)) {
            $props = $null
            try { $props = Get-ItemProperty -LiteralPath $key.PSPath -ErrorAction Stop } catch { continue }
            if (-not (Test-KeepApp $props)) { continue }
            $record = ConvertTo-AppRecord -Entry $props -Scope $source.Scope -Arch $source.Arch -Is64 $is64
            if ($seen.Add($record['name'] + '|' + $record['version'] + '|' + $record['scope'])) { [void]$apps.Add($record) }
        }
    }
    $sorted = @($apps.ToArray() | Sort-Object -Property @{ Expression = { $_['name'] } })
    return , $sorted
}

function Export-WingetList {
    # "winget export" gives exact package ids, which makes finding Linux equivalents easier.
    $winget = Get-Command -Name 'winget' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $winget) {
        Write-Info 'winget is not available on this PC; the app list above is enough.'
        return ''
    }
    if ($script:DryRun) {
        Write-Info 'Would ask winget for the same list with exact package names.'
        return ''
    }
    $target = Join-Path $script:OutDir 'apps-winget.json'
    $stdin = New-TempFile
    $stdout = New-TempFile
    try {
        Write-Info 'Asking winget for the same list with exact package names (this can take a minute)...'
        $line = Join-CommandLine @('export', '-o', $target, '--accept-source-agreements')
        $proc = Start-Process -FilePath $winget.Source -ArgumentList $line -NoNewWindow -PassThru -RedirectStandardInput $stdin -RedirectStandardOutput $stdout
        $null = $proc.Handle
        if (-not $proc.WaitForExit(300000)) {
            try { $proc.Kill() } catch { $null = $_ }
            Write-Warn 'winget took too long, so its list was left out (apps.json has everything needed).'
            Remove-OwnFile $target
            return ''
        }
        if ((Test-Path -LiteralPath $target -PathType Leaf) -and (Get-Item -LiteralPath $target).Length -gt 0) {
            try {
                $null = ConvertFrom-Json -InputObject ([System.IO.File]::ReadAllText($target))
                Write-Good 'winget list saved.'
                return 'apps-winget.json'
            } catch {
                Write-Warn 'winget wrote an unreadable list, so it was left out.'
            }
        } else {
            Write-Info ('winget could not export a list (exit code ' + $proc.ExitCode + '); apps.json has everything needed.')
        }
        Remove-OwnFile $target
        return ''
    } finally {
        Remove-OwnFile $stdin
        Remove-OwnFile $stdout
    }
}

# ------------------------------------------------------------------------------------------------
# Table of contents (lindos-transfer.json) and JSON files
# ------------------------------------------------------------------------------------------------
function Get-WindowsInfo {
    # Only reads. Loading the CIM module under -WhatIf would print "What if: Set Alias" noise.
    $WhatIfPreference = $false
    $caption = ''
    $version = ''
    try {
        $os = Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop
        $caption = [string]$os.Caption
        $version = [string]$os.Version
    } catch {
        $caption = ''
    }
    if (-not $caption) { $caption = [string](Get-RegValue 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' 'ProductName') }
    if (-not $version) { $version = [Environment]::OSVersion.Version.ToString() }
    return [ordered]@{ caption = $caption.Trim(); version = $version.Trim() }
}

function Get-UtcTimestamp {
    return (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss'Z'", [System.Globalization.CultureInfo]::InvariantCulture)
}

function New-TransferManifest {
    param(
        [string]$Created, [string]$Computer, $Windows, [string]$UserName, [string]$UserProfile,
        $Folders, $Browsers, $Wallpaper, $WallpaperStyle, $Fonts, $Wifi, $Apps, $WingetExport,
        $Steam, $Skipped
    )
    if ($null -eq $Folders) { $Folders = [ordered]@{} }
    # BEGIN MANIFEST (keys, order and types are the SPEC-WINDOWS 29.11 contract)
    $manifest = [ordered]@{
        schema = 1
        tool = $script:ToolName
        tool_version = $script:ToolVersion
        created = $Created
        computer = $Computer
        windows = $Windows
        user = [ordered]@{ name = $UserName; profile = $UserProfile }
        folders = $Folders
        browsers = (ConvertTo-JsonArray $Browsers)
        wallpaper = $Wallpaper
        wallpaper_style = $WallpaperStyle
        fonts = $Fonts
        wifi = $Wifi
        apps = $Apps
        winget_export = $WingetExport
        steam = $Steam
        skipped = (ConvertTo-JsonArray $Skipped)
    }
    # END MANIFEST
    return $manifest
}

function Write-JsonFile {
    # UTF-8 with a byte-order mark (Out-File -Encoding utf8 in PowerShell 5.1); Lindos reads it
    # with utf-8-sig. -InputObject keeps one-element arrays as arrays.
    param([string]$Path, $Object)
    $json = ConvertTo-Json -InputObject $Object -Depth 10
    $json | Out-File -LiteralPath $Path -Encoding utf8 -Force
}

# ------------------------------------------------------------------------------------------------
# Main
# ------------------------------------------------------------------------------------------------
function Get-FolderPlan {
    param($UserInfo, [string[]]$Categories)
    $plan = New-Object System.Collections.ArrayList
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($cat in $script:FolderCategories) {
        if ($Categories -notcontains $cat) { continue }
        if ($cat -eq 'onedrive') {
            $roots = (Get-OneDriveRoots $UserInfo)
            foreach ($r in $roots) {
                if (-not $seen.Add($r)) { continue }
                $target = 'files\onedrive'
                if ($roots.Count -gt 1) { $target = 'files\onedrive\' + (Split-Path -Leaf $r) }
                [void]$plan.Add([pscustomobject]@{ Category = $cat; Label = ('OneDrive (' + (Split-Path -Leaf $r) + ')'); Source = $r; Target = $target; Exclude = @(); Scan = $null })
            }
            continue
        }
        $path = Get-KnownFolderPath $UserInfo $cat
        if (-not (Test-Path -LiteralPath $path -PathType Container)) { continue }
        if (-not $seen.Add((Get-NormalizedPath $path))) { continue }
        [void]$plan.Add([pscustomobject]@{ Category = $cat; Label = $script:CategoryLabels[$cat]; Source = (Get-NormalizedPath $path); Target = ('files\' + $cat); Exclude = @(); Scan = $null })
    }
    return , $plan.ToArray()
}

function Set-FolderExclusions {
    # A folder inside another one (Documents inside OneDrive, the transfer folder inside
    # Downloads...) is copied once, never twice, and the transfer folder never copies itself.
    param($Plan)
    foreach ($entry in $Plan) {
        $exclude = New-Object System.Collections.ArrayList
        if ($script:OutDir -and (Test-PathInside $script:OutDir $entry.Source)) { [void]$exclude.Add($script:OutDir) }
        foreach ($other in $Plan) {
            if ($other.Source -ieq $entry.Source) { continue }
            if (Test-PathInside $other.Source $entry.Source) { [void]$exclude.Add($other.Source) }
        }
        $entry.Exclude = $exclude.ToArray()
    }
}

function Invoke-Part {
    param([string]$Label, [scriptblock]$Action)
    try {
        return (& $Action)
    } catch {
        $script:Failures++
        Write-Bad ($Label + ': ' + $_.Exception.Message)
        return $null
    }
}

function Invoke-TransferMain {
    $script:Skipped = New-Object System.Collections.ArrayList
    $script:SkippedOverflow = @{}
    $script:Failures = 0
    $script:WindowsDir = [Environment]::GetFolderPath('Windows')
    if (-not $script:WindowsDir) { $script:WindowsDir = $env:WINDIR }
    $inventoryOnly = [bool]$script:Opt.InventoryOnly

    Write-Host ''
    Write-Host 'Lindos Transfer - bring your files from this PC to Lindos' -ForegroundColor Cyan
    Write-Host '  Nothing on this PC is changed, moved or deleted: files are only copied.'
    if ($script:DryRun) { Write-Host '  TEST RUN (-WhatIf): nothing will be written anywhere.' -ForegroundColor Yellow }

    # -- what, who, where ---------------------------------------------------------------------
    $cats = (Get-SelectedCategories -Include $script:Opt.Include -WifiPasswords $script:Opt.IncludeWifiPasswords -FirefoxPasswords $script:Opt.IncludeFirefoxPasswords -SteamGames $script:Opt.IncludeSteamGames)
    if ($script:UsageError) { Write-Bad $script:UsageError; return 2 }
    $userInfo = Resolve-TransferUser -Name $script:Opt.User
    if ($null -eq $userInfo) {
        Write-Bad ('There is no user "' + $script:Opt.User + '" on this PC that the kit can copy.')
        return 2
    }
    if (-not $userInfo.IsCurrent) {
        Write-Warn ('Copying the files of ' + $userInfo.Name + ' while signed in as ' + [Environment]::UserName + '.')
        Write-Info 'Only files your account may read are copied, and OneDrive or moved folders are not detected.'
        Write-Info ('For the best result, sign in as ' + $userInfo.Name + ' and run the kit there.')
    }
    $paths = Get-UserPaths $userInfo
    $dest = Resolve-DestinationFolder -Requested $script:Opt.Destination
    if (-not $dest) { return 2 }

    $stamp = (Get-Date).ToString('yyyyMMdd-HHmm', [System.Globalization.CultureInfo]::InvariantCulture)
    $computer = [string]$env:COMPUTERNAME
    if (-not $computer) { $computer = 'PC' }
    $script:OutDir = Join-Path $dest ('LindosTransfer-' + $computer + '-' + $stamp)
    if (-not $script:DryRun) {
        try {
            New-Item -ItemType Directory -Path $script:OutDir -Force | Out-Null
        } catch {
            Write-Bad ('Cannot create the transfer folder in ' + $dest + ': ' + $_.Exception.Message)
            Write-Info 'Is the USB stick write-protected or full? Choose another folder with -Destination.'
            return 1
        }
        $script:LogPath = Join-Path $script:OutDir 'LindosTransfer.log'
        Write-LogLine ($script:ToolName + ' ' + $script:ToolVersion + ' started; categories: ' + ($cats -join ', '))
    }

    $drive = Get-DriveInfoFor $dest
    $maxFile = [long]0
    Write-Title 'Where the transfer folder goes'
    Write-Info $script:OutDir
    if ($null -ne $drive) {
        $format = [string]$drive.DriveFormat
        Write-Info ('Drive ' + $drive.Name + ' (' + $format + ', ' + (Format-Size $drive.AvailableFreeSpace) + ' free)')
        if ($format -ieq 'FAT32') { $maxFile = [long]$script:Fat32MaxFileBytes }
        elseif ($format -ieq 'FAT') { $maxFile = [long]$script:FatMaxFileBytes }
        if ($maxFile -gt 0) {
            Write-Warn ('This drive uses ' + $format + ', which cannot store files larger than ' + (Format-Size ($maxFile + 1)) + '.')
            Write-Info 'Such files are skipped and listed. To avoid that, use a drive formatted as exFAT or NTFS.'
        }
        $systemDrive = [string]$env:SystemDrive
        if ($systemDrive -and $drive.Name -ieq ($systemDrive + '\')) {
            Write-Warn 'This is the drive of this PC itself. That is fine for a test; to move to a new PC use a USB stick or external drive.'
        }
    }

    # -- opt-ins (asked now, before any long copy) ---------------------------------------------
    $script:WifiPasswords = $false
    $script:FirefoxPasswords = $false
    if (-not $inventoryOnly -and $cats -contains 'wifi' -and $script:Opt.IncludeWifiPasswords) {
        if ($script:DryRun) {
            Write-Info 'Would ask before including Wi-Fi passwords.'
        } else {
            $script:WifiPasswords = Confirm-OptIn -Title 'Include Wi-Fi passwords?' -Lines @(
                'This writes your Wi-Fi passwords in plain text into the transfer folder. Keep it private and',
                'delete it after importing - deleting from a USB stick does not securely erase it.',
                'Windows will ask for administrator permission for this step only.')
        }
    }
    if (-not $inventoryOnly -and $cats -contains 'firefox' -and $script:Opt.IncludeFirefoxPasswords) {
        if ($script:DryRun) {
            Write-Info 'Would ask before including Firefox saved passwords.'
        } else {
            $script:FirefoxPasswords = Confirm-OptIn -Title 'Include Firefox saved passwords?' -Lines @(
                'This copies the files in which Firefox keeps your saved passwords, unchanged. Without a',
                'Firefox Primary Password anyone with the transfer folder can read them. Keep it private and',
                'delete it after importing. (Firefox Sync is the safer way to move passwords.)')
        }
    }
    if (-not $inventoryOnly -and -not $script:DryRun -and $cats -contains 'firefox' -and (Test-Interactive)) {
        if (@(Get-Process -Name 'firefox' -ErrorAction SilentlyContinue).Count -gt 0) {
            Write-Warn 'Firefox is open. Please close Firefox so its bookmarks and history copy cleanly.'
            $null = Read-Host '  Press Enter when Firefox is closed (or to continue anyway)'
        }
    }

    # -- measure -------------------------------------------------------------------------------
    $plan = New-Object System.Collections.ArrayList
    foreach ($e in (Get-FolderPlan -UserInfo $userInfo -Categories $cats)) { [void]$plan.Add($e) }
    $steamPlan = $null
    if ($cats -contains 'steam-games') {
        $steamPlan = Invoke-Part 'Steam' { Get-SteamPlan $userInfo }
        if ($null -eq $steamPlan) {
            Write-Info 'Steam is not installed for this user.'
        } elseif ($script:Opt.IncludeSteamGames) {
            foreach ($game in @($steamPlan.Games)) {
                if (-not (Test-SafeFolderName $game.InstallDir)) { continue }
                $src = Join-Path $game.Library ('steamapps\common\' + $game.InstallDir)
                if (-not (Test-Path -LiteralPath $src -PathType Container)) { continue }
                $label = 'Steam game ' + $game.Name
                if (-not $game.Name) { $label = 'Steam game ' + $game.InstallDir }
                [void]$plan.Add([pscustomobject]@{ Category = 'steam-games'; Label = $label; Source = (Get-NormalizedPath $src); Target = ('steam\common\' + $game.InstallDir); Exclude = @(); Scan = $null })
            }
        }
    }
    Set-FolderExclusions $plan
    $totalBytes = [long]0
    if ($plan.Count -gt 0) {
        Write-Title 'Measuring your folders (nothing is downloaded or opened)'
        foreach ($entry in $plan) {
            Write-Progress -Activity 'Measuring your folders' -Status $entry.Label
            $entry.Scan = Measure-TransferFolder -Root $entry.Source -ExcludeDirs $entry.Exclude -MaxFileBytes $maxFile
            $totalBytes += $entry.Scan.Bytes
            $line = $entry.Label + ': ' + (Format-Count $entry.Scan.Files) + ' files, ' + (Format-Size $entry.Scan.Bytes)
            $cloud = $entry.Scan.CloudOffline.Count + $entry.Scan.CloudRecall.Count
            if ($cloud -gt 0) { $line = $line + ' (' + (Format-Count $cloud) + ' only in OneDrive/the cloud: not copied)' }
            if ($entry.Scan.TooBig.Count -gt 0) { $line = $line + ' (' + (Format-Count $entry.Scan.TooBig.Count) + ' too big for this drive: not copied)' }
            Write-Info $line
        }
        Write-Progress -Activity 'Measuring your folders' -Completed
        Write-Info ('Total: ' + (Format-Size $totalBytes))
    }

    # -- space ---------------------------------------------------------------------------------
    if (-not $inventoryOnly -and $null -ne $drive) {
        $needed = $totalBytes + 64MB
        $free = [long]$drive.AvailableFreeSpace
        if ($free -lt $needed) {
            Write-Bad ('Not enough free space: the transfer needs about ' + (Format-Size $needed) + ' but the drive has ' + (Format-Size $free) + ' free.')
            Write-Info 'Use a bigger drive, or copy fewer things, for example:'
            Write-Info '  powershell -NoProfile -ExecutionPolicy Bypass -File LindosTransfer.ps1 -Include documents,pictures'
            if (-not $script:DryRun) { return 5 }
        }
    }

    # -- copy ----------------------------------------------------------------------------------
    $folders = [ordered]@{}
    $browsers = New-Object System.Collections.ArrayList
    $wallpaper = $null
    $wallpaperStyle = $null
    $fontsRel = $null
    $wifi = $null
    $appsRel = $null
    $wingetRel = $null
    $steam = $null
    $gamesCopied = $false
    $appCount = 0
    $fontCount = 0

    if (-not $inventoryOnly) {
        if ($plan.Count -gt 0) {
            Write-Title 'Copying your files'
            foreach ($entry in $plan) {
                if ($script:DryRun) {
                    Write-Info ('Would copy ' + $entry.Label + ' to ' + $entry.Target)
                    continue
                }
                $ok = Copy-PlannedFolder -Entry $entry -MaxFileBytes $maxFile
                Add-ScanSkipped -Entry $entry -MaxFileBytes $maxFile
                if (-not $ok) { continue }
                if ($entry.Category -eq 'steam-games') { $gamesCopied = $true; continue }
                $folders[$entry.Category] = 'files/' + $entry.Category
            }
        }
        if ($cats -contains 'bookmarks') {
            Write-Title 'Browser bookmarks (never passwords, cookies or payment data)'
            foreach ($b in @(Invoke-Part 'Bookmarks' { Copy-ChromiumBookmarks $paths })) { if ($null -ne $b) { [void]$browsers.Add($b) } }
        }
        if ($cats -contains 'firefox') {
            Write-Title 'Firefox bookmarks and history'
            foreach ($b in @(Invoke-Part 'Firefox' { Copy-FirefoxData $paths })) { if ($null -ne $b) { [void]$browsers.Add($b) } }
        }
        if (($cats -contains 'bookmarks' -or $cats -contains 'firefox') -and $browsers.Count -eq 0) { Write-Info 'No browser bookmarks were found.' }
        if ($cats -contains 'wallpaper') {
            Write-Title 'Desktop wallpaper'
            $wp = Invoke-Part 'Wallpaper' { Copy-Wallpaper $userInfo $paths }
            if ($null -ne $wp) { $wallpaper = $wp.File; $wallpaperStyle = $wp.Style }
        }
        if ($cats -contains 'fonts') {
            Write-Title 'Fonts you installed yourself (never the fonts that come with Windows)'
            $fontCount = [int](Invoke-Part 'Fonts' { Copy-UserFonts $paths })
            if ($fontCount -gt 0) { $fontsRel = 'fonts' } else { Write-Info 'You have not installed any fonts yourself.' }
        }
        if ($cats -contains 'wifi') {
            Write-Title 'Wi-Fi networks'
            $wifi = Invoke-Part 'Wi-Fi' { Export-WifiProfiles $script:WifiPasswords }
        }
        if ($cats -contains 'steam-games' -and $null -ne $steamPlan) {
            Write-Title 'Steam'
            $okSteam = Invoke-Part 'Steam' { Copy-SteamFiles $steamPlan }
            if ($okSteam) { $steam = [ordered]@{ dir = 'steam'; games_copied = [bool]$gamesCopied } }
        }
    }

    if ($cats -contains 'apps') {
        Write-Title 'Your apps (a list only: programs are not copied)'
        $apps = @(Invoke-Part 'Apps' { Get-InstalledApps -IncludeCurrentUser ([bool]$userInfo.IsCurrent) })
        $apps = @($apps | Where-Object { $null -ne $_ })
        $appCount = $apps.Count
        if (-not $userInfo.IsCurrent) { Write-Info ('Apps installed only for ' + $userInfo.Name + ' are not listed (sign in as them to include those).') }
        if ($script:DryRun) {
            Write-Info ('Would write apps.json (' + $appCount + ' apps).')
        } else {
            Write-JsonFile -Path (Join-Path $script:OutDir 'apps.json') -Object (ConvertTo-JsonArray $apps)
            Write-Good ('List of ' + $appCount + ' apps saved.')
            $appsRel = 'apps.json'
        }
        $w = Invoke-Part 'winget' { Export-WingetList }
        if ($w) { $wingetRel = [string]$w }
    }

    # -- table of contents ---------------------------------------------------------------------
    $manifest = New-TransferManifest -Created (Get-UtcTimestamp) -Computer $computer -Windows (Get-WindowsInfo) `
        -UserName $userInfo.Name -UserProfile $userInfo.Profile -Folders $folders -Browsers $browsers.ToArray() `
        -Wallpaper $wallpaper -WallpaperStyle $wallpaperStyle -Fonts $fontsRel -Wifi $wifi -Apps $appsRel `
        -WingetExport $wingetRel -Steam $steam -Skipped (Get-SkippedList)
    if (-not $script:DryRun) {
        Write-JsonFile -Path (Join-Path $script:OutDir $script:ManifestName) -Object $manifest
    }

    # -- summary -------------------------------------------------------------------------------
    $somethingThere = ($folders.Count -gt 0) -or ($browsers.Count -gt 0) -or $wallpaper -or $fontsRel -or
        ($null -ne $wifi) -or ($null -ne $steam) -or ($appCount -gt 0) -or
        (($script:DryRun -or $inventoryOnly) -and $plan.Count -gt 0)
    Write-Title 'Summary'
    if ($script:DryRun) {
        Write-Info 'Test run finished: nothing was written. Run the kit without -WhatIf to copy for real.'
    }
    if ($inventoryOnly) {
        Write-Info 'Inventory only: no files were copied.'
        if ($plan.Count -gt 0) { Write-Info ('A full transfer of the folders above needs about ' + (Format-Size ($totalBytes + 64MB)) + '.') }
    }
    $cloudSkipped = 0
    $bigSkipped = 0
    foreach ($entry in $plan) {
        if ($null -eq $entry.Scan) { continue }
        $cloudSkipped += $entry.Scan.CloudOffline.Count + $entry.Scan.CloudRecall.Count
        $bigSkipped += $entry.Scan.TooBig.Count
    }
    if ($cloudSkipped -gt 0) {
        Write-Info ('' + (Format-Count $cloudSkipped) + ' file(s) are only in OneDrive (or another cloud) and were not downloaded or copied.')
        Write-Info 'Get them on Lindos from onedrive.com, or make them "Always keep on this device" and run the kit again.'
    }
    if ($bigSkipped -gt 0) { Write-Warn ('' + (Format-Count $bigSkipped) + ' file(s) were too big for this drive (FAT32) and were not copied.') }
    Write-Info 'Never copied: saved browser passwords, cookies and payment data (Windows keeps them encrypted for'
    Write-Info 'this PC only - use your browser''s sync or its password export), and the fonts and pictures that'
    Write-Info 'come with Windows.'
    if (-not $somethingThere) {
        Write-Warn 'Nothing was found to transfer.'
        return 4
    }
    if (-not $script:DryRun) {
        if ($script:Failures -gt 0) {
            Write-Warn ('Finished, but ' + $script:Failures + ' step(s) had problems. Details: ' + $script:LogPath)
        } else {
            Write-Good 'Done! Your transfer folder is ready:'
        }
        Write-Info $script:OutDir
        if ($script:WifiPasswords -and $null -ne $wifi -and $wifi['with_keys']) {
            Write-Warn 'It contains Wi-Fi passwords in plain text: keep it private and delete it after importing.'
        }
        Write-Info 'Next: plug this drive into your Lindos PC, open "Transfer from Windows" and choose'
        Write-Info '"Use a transfer folder", then pick the folder above.'
    }
    Write-LogLine ('Finished with ' + $script:Failures + ' problem(s).')
    if ($script:Failures -gt 0) { return 1 }
    return 0
}

function Invoke-LindosTransfer {
    if (-not (Test-FullLanguage)) { return 3 }
    $relaunched = Invoke-64BitRelaunch
    if ($null -ne $relaunched) { return $relaunched }
    try {
        # Take only the last value: the exit code (defends against stray pipeline output).
        $result = @(Invoke-TransferMain)
        if ($result.Count -eq 0) { return 1 }
        return [int]$result[$result.Count - 1]
    } catch {
        Write-Bad ('Something unexpected went wrong: ' + $_.Exception.Message)
        Write-LogLine ($_ | Out-String)
        if ($script:LogPath) { Write-Info ('Details: ' + $script:LogPath) }
        return 1
    }
}

# Dot-sourcing (". .\LindosTransfer.ps1") only loads the functions, for testing.
if ($MyInvocation.InvocationName -ne '.') {
    $exitCode = Invoke-LindosTransfer
    exit $exitCode
}
