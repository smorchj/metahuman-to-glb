param(
    [Parameter(Mandatory=$true)][string]$Char
)

$ErrorActionPreference = "Stop"

# Resolve workspace root from this script's location (stages/03-export-to-glb/tools/ -> up 3)
$ToolsDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$Workspace = Resolve-Path (Join-Path $ToolsDir "..\..\..") | Select-Object -ExpandProperty Path

# Load blender_exe from _config/pipeline.yaml
$Config = Join-Path $Workspace "_config\pipeline.yaml"
$BlenderLine = (Select-String -Path $Config -Pattern '^\s*blender_exe:' | Select-Object -First 1).Line
if (-not $BlenderLine) { throw "blender_exe not found in $Config" }
$BlenderExe = ($BlenderLine -replace '^\s*blender_exe:\s*"?([^"]+)"?\s*$', '$1').Trim()

$PyScript = Join-Path $ToolsDir "export_glb.py"
$Blend    = Join-Path $Workspace "characters\$Char\02-blend\$Char.blend"

if (-not (Test-Path $Blend)) { throw "stage 02 blend not found: $Blend" }

Write-Host "[run_export] blender   = $BlenderExe"
Write-Host "[run_export] workspace = $Workspace"
Write-Host "[run_export] char      = $Char"
Write-Host "[run_export] blend     = $Blend"

& $BlenderExe --background $Blend --python $PyScript -- --char $Char --workspace $Workspace
$code = $LASTEXITCODE
Write-Host "[run_export] exit code: $code"
exit $code
