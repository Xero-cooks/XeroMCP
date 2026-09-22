# ==============================================================================
# ocr_winrt.ps1 - Zero-dependency Windows OCR.
# Windows.Media.Ocr ships in Windows 10/11; this wrapper calls the precompiled
# modules/winocr_helper.dll (built from winocr_helper.cs via csc.exe - see the
# .cs header for the build command). No pip packages, no admin rights.
#
# Usage: powershell -NoProfile -File ocr_winrt.ps1 <image_path> [lang]
# Output: single-line JSON { engine, lines: [{text,x,y,w,h}], error? }
# ==============================================================================
param(
    [Parameter(Mandatory = $true)] [string]$ImagePath,
    [string]$Lang = "en-US"
)

$ErrorActionPreference = "Stop"
$dll = Join-Path $PSScriptRoot "winocr_helper.dll"
$out = [ordered]@{ engine = "windows_ocr"; lines = @() }

if (-not (Test-Path $dll)) {
    $out.engine = "none"
    $out.error = "winocr_helper.dll missing - run its csc.exe build (see winocr_helper.cs header)"
    $out | ConvertTo-Json -Depth 4 -Compress
    exit 0
}

try {
    $asm = [System.Reflection.Assembly]::LoadFrom($dll)
    $type = $asm.GetType("WinOcr")
    $method = $type.GetMethod("Recognize")
    $dtos = $method.Invoke($null, @($ImagePath, $Lang))
    foreach ($d in $dtos) {
        $out.lines += [ordered]@{
            text = $d.Text
            x    = [int][Math]::Round($d.X)
            y    = [int][Math]::Round($d.Y)
            w    = [int][Math]::Round($d.W)
            h    = [int][Math]::Round($d.H)
        }
    }
} catch {
    $out.error = "recognition failed: $($_.Exception.Message)"
    if ($_.Exception.InnerException) { $out.error += " | inner: $($_.Exception.InnerException.Message)" }
}

$out | ConvertTo-Json -Depth 4 -Compress
