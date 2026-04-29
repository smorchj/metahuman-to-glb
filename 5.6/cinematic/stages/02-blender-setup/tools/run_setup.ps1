param(
    [Parameter(Mandatory=$true)][string]$Char,
    [switch]$SkipPreview
)

$ErrorActionPreference = "Stop"

# Resolve workspace root from this script's location (stages/02-blender-setup/tools/ → up 3)
$ToolsDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$Workspace = Resolve-Path (Join-Path $ToolsDir "..\..\..") | Select-Object -ExpandProperty Path

# Load blender_exe from _config/pipeline.yaml (one-line grep, no YAML parser dep)
$Config = Join-Path $Workspace "_config\pipeline.yaml"
$BlenderLine = (Select-String -Path $Config -Pattern '^\s*blender_exe:' | Select-Object -First 1).Line
if (-not $BlenderLine) { throw "blender_exe not found in $Config" }
$BlenderExe = ($BlenderLine -replace '^\s*blender_exe:\s*"?([^"]+)"?\s*$', '$1').Trim()

$PyScript = Join-Path $ToolsDir "import_fbx.py"
$PvScript = Join-Path $ToolsDir "render_preview.py"

Write-Host "[run_setup] blender   = $BlenderExe"
Write-Host "[run_setup] workspace = $Workspace"
Write-Host "[run_setup] char      = $Char"

& $BlenderExe --background --python $PyScript -- --char $Char --workspace $Workspace
$code = $LASTEXITCODE
Write-Host "[run_setup] import exit code: $code"
if ($code -ne 0) { exit $code }

if (-not $SkipPreview) {
    $Blend = Join-Path $Workspace "characters\$Char\02-blend\$Char.blend"
    & $BlenderExe --background $Blend --python $PvScript -- --char $Char --workspace $Workspace --view threequarter
    $pcode = $LASTEXITCODE
    Write-Host "[run_setup] preview exit code: $pcode"
    exit $pcode
}
exit 0
