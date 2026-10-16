[CmdletBinding()]
param(
    [string]$Message = ""
)

$ErrorActionPreference = "Stop"
$Utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $Utf8
[Console]::OutputEncoding = $Utf8
$OutputEncoding = $Utf8

$Root = (Resolve-Path (Join-Path $PSScriptRoot ".")).Path
Set-Location $Root

function Invoke-Git {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    & git "-c" "safe.directory=$Root" @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Git command failed: git $($Arguments -join ' ')"
    }
}

$Top = (& git "-c" "safe.directory=$Root" rev-parse --show-toplevel).Trim()
$NormalizedRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
$NormalizedTop = if ($Top) { [System.IO.Path]::GetFullPath($Top).TrimEnd('\', '/') } else { "" }
if ($LASTEXITCODE -ne 0 -or $NormalizedTop -ine $NormalizedRoot) {
    throw "This script must be run from the repository root: $Root"
}

$Remote = (& git "-c" "safe.directory=$Root" remote get-url origin).Trim()
if ($LASTEXITCODE -ne 0 -or $Remote -notmatch '^https://github\.com/pcmola/ai-master(?:\.git)?$') {
    throw "origin is not pcmola/ai-master: $Remote"
}

$Branch = (& git "-c" "safe.directory=$Root" branch --show-current).Trim()
if ([string]::IsNullOrWhiteSpace($Branch)) {
    throw "The current branch could not be determined."
}

# Do not commit real environment files.
$TrackedEnv = @(& git "-c" "safe.directory=$Root" ls-files -- .env .env.* | Where-Object { $_ -ne ".env.sample" })
if ($TrackedEnv.Count -gt 0) {
    throw "Sensitive environment files are already tracked: $($TrackedEnv -join ', ')"
}

Invoke-Git -Arguments @("add", "-A")
$Staged = @(& git "-c" "safe.directory=$Root" diff --cached --name-only)
if ($LASTEXITCODE -ne 0) {
    throw "Could not inspect staged files."
}
$SensitiveStaged = @($Staged | Where-Object { $_ -eq ".env" -or ($_ -like ".env.*" -and $_ -ne ".env.sample") })
if ($SensitiveStaged.Count -gt 0) {
    throw "Sensitive environment files are staged: $($SensitiveStaged -join ', ')"
}

if ($Staged.Count -eq 0) {
    Write-Host "No changes to commit. Pushing current branch: $Branch"
} else {
    if ([string]::IsNullOrWhiteSpace($Message)) {
        $Message = "Update sources $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
    }
    Write-Host "Committing $($Staged.Count) staged file(s): $Message"
    Invoke-Git -Arguments @("commit", "-m", $Message)
}

Invoke-Git -Arguments @("push", "--set-upstream", "origin", $Branch)
Write-Host "GitHub push completed: $Remote ($Branch)"
