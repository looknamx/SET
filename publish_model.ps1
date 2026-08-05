param(
    [Parameter(Position = 0)]
    [string]$ModelPath = ".\best.pt",
    [string]$MinAppVersion = "",
    [string]$Repository = "looknamx/SET",
    [string]$Branch = "main"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $ModelPath -PathType Leaf)) {
    throw "Model file was not found: $ModelPath"
}

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw "GitHub CLI (gh) is not installed or this PowerShell window needs to be reopened."
}

gh auth status | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "GitHub CLI is not logged in. Run: gh auth login"
}

if (-not $MinAppVersion) {
    $versionFile = Join-Path $PSScriptRoot "version.txt"
    if (-not (Test-Path -LiteralPath $versionFile)) {
        throw "version.txt was not found. Pass -MinAppVersion explicitly."
    }
    $MinAppVersion = (Get-Content -LiteralPath $versionFile -Raw).Trim()
}

$resolvedModel = (Resolve-Path -LiteralPath $ModelPath).Path
$dateVersion = Get-Date -Format "yyyy.MM.dd"
$existingTags = @(
    gh release list --repo $Repository --limit 100 --json tagName --jq ".[].tagName"
)
$sequence = 1
foreach ($tag in $existingTags) {
    if ($tag -match "^model-$([regex]::Escape($dateVersion))\.(\d+)$") {
        $sequence = [Math]::Max($sequence, [int]$Matches[1] + 1)
    }
}

$modelVersion = "$dateVersion.$sequence"
$tagName = "model-$modelVersion"
$sha256 = (Get-FileHash -LiteralPath $resolvedModel -Algorithm SHA256).Hash.ToLowerInvariant()
$downloadUrl = "https://github.com/$Repository/releases/download/$tagName/best.pt"
$manifest = [ordered]@{
    model_version = $modelVersion
    download_url = $downloadUrl
    sha256 = $sha256
    min_app_version = $MinAppVersion
}
$manifestJson = $manifest | ConvertTo-Json

Write-Host "Publishing $tagName..." -ForegroundColor Cyan
gh release create $tagName "$resolvedModel#best.pt" `
    --repo $Repository `
    --title "AI Model $modelVersion" `
    --notes "AI model $modelVersion (minimum app version $MinAppVersion)" `
    --latest=false
if ($LASTEXITCODE -ne 0) {
    throw "Could not create the GitHub model release."
}

$manifestPath = Join-Path $PSScriptRoot "model_manifest.json"
[System.IO.File]::WriteAllText($manifestPath, $manifestJson + [Environment]::NewLine)
$encodedManifest = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($manifestJson + "`n"))

$remoteSha = $null
try {
    $remoteSha = gh api "repos/$Repository/contents/model_manifest.json?ref=$Branch" --jq ".sha" 2>$null
    if ($LASTEXITCODE -ne 0) { $remoteSha = $null }
} catch {
    $remoteSha = $null
}

$apiArguments = @(
    "api", "repos/$Repository/contents/model_manifest.json",
    "--method", "PUT",
    "-f", "message=Update AI model manifest to $modelVersion",
    "-f", "content=$encodedManifest",
    "-f", "branch=$Branch"
)
if ($remoteSha) {
    $apiArguments += @("-f", "sha=$remoteSha")
}
& gh @apiArguments | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "The release was created, but model_manifest.json could not be updated."
}

Write-Host "Model published successfully." -ForegroundColor Green
Write-Host "Version : $modelVersion"
Write-Host "SHA256  : $sha256"
Write-Host "Download: $downloadUrl"
