# 缁勮鍗曟枃浠?exe锛歭auncher.exe + payload.zip + 24 瀛楄妭灏鹃儴绱㈠紩
param(
    [string]$Launcher = "$PSScriptRoot\launcher_icon.exe",
    [string]$Payload  = "$PSScriptRoot\payload.zip",
    [string]$Out      = "$PSScriptRoot\..\鏅鸿兘鑰冨満AI宸¤€冪郴缁?exe"
)

$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $Launcher)) { throw "缂哄皯鍚姩鍣? $Launcher" }
if (-not (Test-Path -LiteralPath $Payload))  { throw "缂哄皯 payload: $Payload" }

Copy-Item -LiteralPath $Launcher -Destination $Out -Force
$off = (Get-Item -LiteralPath $Out).Length

$outFs = [System.IO.File]::Open($Out, [System.IO.FileMode]::Append,
                                [System.IO.FileAccess]::Write)
try {
    $inFs = [System.IO.File]::OpenRead($Payload)
    try { $inFs.CopyTo($outFs) } finally { $inFs.Dispose() }

    $len = (Get-Item -LiteralPath $Payload).Length
    $trailer = New-Object byte[] 24
    [System.Text.Encoding]::ASCII.GetBytes('ZKSFX001').CopyTo($trailer, 0)
    [BitConverter]::GetBytes([int64]$off).CopyTo($trailer, 8)
    [BitConverter]::GetBytes([int64]$len).CopyTo($trailer, 16)
    $outFs.Write($trailer, 0, 24)
} finally {
    $outFs.Dispose()
}

$f = Get-Item -LiteralPath $Out
$sha = (Get-FileHash -LiteralPath $Out -Algorithm SHA256).Hash
Write-Host ("宸茬敓鎴? {0}" -f $f.FullName)
Write-Host ("澶у皬:   {0:N1} MB" -f ($f.Length / 1MB))
Write-Host ("SHA256: {0}" -f $sha)

