[CmdletBinding()]
param(
    [string]$Message = ""
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot ".")).Path
Set-Location $Root

function Invoke-Git {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & git "-c" "safe.directory=$Root" @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Git 명령 실패: git $($Arguments -join ' ')"
    }
}

$Top = (& git "-c" "safe.directory=$Root" rev-parse --show-toplevel).Trim()
if ($LASTEXITCODE -ne 0 -or $Top -ne $Root) {
    throw "이 스크립트는 저장소 루트에서만 실행할 수 있습니다: $Root"
}

$Remote = (& git "-c" "safe.directory=$Root" remote get-url origin).Trim()
if ($LASTEXITCODE -ne 0 -or $Remote -notmatch '^https://github\.com/pcmola/ai-master(?:\.git)?$') {
    throw "origin이 pcmola/ai-master가 아닙니다: $Remote"
}

$Branch = (& git "-c" "safe.directory=$Root" branch --show-current).Trim()
if ([string]::IsNullOrWhiteSpace($Branch)) {
    throw "현재 브랜치를 확인할 수 없습니다."
}

# .env와 실제 인증정보가 Git에 들어가지 않았는지 먼저 확인합니다.
$TrackedEnv = @(& git "-c" "safe.directory=$Root" ls-files -- .env .env.* | Where-Object { $_ -ne ".env.sample" })
if ($TrackedEnv.Count -gt 0) {
    throw "민감한 환경파일이 추적 중입니다: $($TrackedEnv -join ', ')"
}

Invoke-Git add -A
$Staged = @(& git "-c" "safe.directory=$Root" diff --cached --name-only)
if ($LASTEXITCODE -ne 0) {
    throw "스테이징된 파일 목록을 확인하지 못했습니다."
}
$SensitiveStaged = @($Staged | Where-Object { $_ -eq ".env" -or ($_ -like ".env.*" -and $_ -ne ".env.sample") })
if ($SensitiveStaged.Count -gt 0) {
    throw "민감한 환경파일이 스테이징되었습니다: $($SensitiveStaged -join ', ')"
}

if ($Staged.Count -eq 0) {
    Write-Host "커밋할 변경사항이 없습니다. 현재 브랜치를 푸시합니다: $Branch"
} else {
    if ([string]::IsNullOrWhiteSpace($Message)) {
        $Message = "Update sources $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
    }
    Write-Host "스테이징 파일 $($Staged.Count)개를 커밋합니다: $Message"
    Invoke-Git commit -m $Message
}

Invoke-Git push --set-upstream origin $Branch
Write-Host "GitHub 푸시 완료: $Remote ($Branch)"
