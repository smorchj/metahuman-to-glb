param(
    [Parameter(Mandatory=$true)][string]$Char
)

$ErrorActionPreference = "Stop"

# Resolve workspace root from this script's location (stages/04-webview-build/tools/ -> up 3)
$ToolsDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$Workspace = Resolve-Path (Join-Path $ToolsDir "..\..\..") | Select-Object -ExpandProperty Path

$PyScript = Join-Path $ToolsDir "build_site.py"

# Prefer system python; fall back to py launcher; last resort Blender's bundled Python.
$Py = $null
foreach ($cand in @("python", "py")) {
    $null = & $cand --version 2>$null
    if ($LASTEXITCODE -eq 0) { $Py = $cand; break }
}
if (-not $Py) {
    $Config = Join-Path $Workspace "_config\pipeline.yaml"
    $BlenderLine = (Select-String -Path $Config -Pattern '^\s*blender_exe:' | Select-Object -First 1).Line
    if ($BlenderLine) {
        $BlenderExe = ($BlenderLine -replace '^\s*blender_exe:\s*"?([^"]+)"?\s*$', '$1').Trim()
        $BlenderDir = Split-Path -Parent $BlenderExe
        $BlPython = Get-ChildItem -Path $BlenderDir -Recurse -Filter "python.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($BlPython) { $Py = $BlPython.FullName }
    }
}
if (-not $Py) { throw "No Python found (tried python, py, and Blender's bundled python.exe)" }

Write-Host "[run_site] python    = $Py"
Write-Host "[run_site] workspace = $Workspace"
Write-Host "[run_site] char      = $Char"

& $Py $PyScript --char $Char --workspace $Workspace
$code = $LASTEXITCODE
Write-Host "[run_site] exit code: $code"
exit $code
