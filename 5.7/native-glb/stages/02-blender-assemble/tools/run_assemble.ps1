<#
.SYNOPSIS
  Stage 02 launcher (5.7 native-glb) — headless Blender import of stage 01's
  per-mesh GLBs + LSE FBX, produces characters/<id>/02-blend/<id>.blend with
  51 ARKit shape keys baked onto the face mesh + propagated onto eyebrow /
  beard / mustache card meshes.

.USAGE
  ./run_assemble.ps1 -Char ada
#>
param(
    [Parameter(Mandatory=$true)][string]$Char
)

$ErrorActionPreference = "Stop"

# Resolve workspace root from this script's location (stages/02-blender-assemble/tools/ -> up 3)
$ToolsDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$Workspace = Resolve-Path (Join-Path $ToolsDir "..\..\..") | Select-Object -ExpandProperty Path

# Load blender_exe from _config/pipeline.yaml. Try in-pipeline first, fall
# back to worktree-root location (config is shared across pipelines).
$Config = Join-Path $Workspace "_config\pipeline.yaml"
if (-not (Test-Path $Config)) {
    $Config = Join-Path (Join-Path $Workspace "..\..\") "_config\pipeline.yaml"
    $Config = (Resolve-Path $Config).Path
}
$BlenderLine = (Select-String -Path $Config -Pattern '^\s*blender_exe:' | Select-Object -First 1).Line
if (-not $BlenderLine) { throw "blender_exe not found in $Config" }
$BlenderExe = ($BlenderLine -replace '^\s*blender_exe:\s*"?([^"]+)"?\s*$', '$1').Trim()

$PyScript = Join-Path $ToolsDir "import_glb.py"
$InRoot   = Join-Path $Workspace "characters\$Char\01-glb"

if (-not (Test-Path $InRoot)) { throw "stage 01 outputs not found: $InRoot (run stage 01 first)" }
if (-not (Test-Path $PyScript)) { throw "import_glb.py not found: $PyScript" }

Write-Host "[run_assemble] blender   = $BlenderExe"
Write-Host "[run_assemble] workspace = $Workspace"
Write-Host "[run_assemble] char      = $Char"
Write-Host "[run_assemble] in_root   = $InRoot"

& $BlenderExe --background --python $PyScript -- --char $Char --workspace $Workspace
$code = $LASTEXITCODE
Write-Host "[run_assemble] exit code: $code"
exit $code
