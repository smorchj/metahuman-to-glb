param(
    [Parameter(Mandatory=$true)][string]$Char
)

$ErrorActionPreference = "Stop"

# Resolve workspace root from this script's location (stages/03-export-to-glb/tools/ -> up 3)
$ToolsDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$Workspace = Resolve-Path (Join-Path $ToolsDir "..\..\..") | Select-Object -ExpandProperty Path

# Load blender_exe from _config/pipeline.yaml. The config lives at the
# worktree root (../../ from the pipeline root), so try the in-pipeline
# location first (legacy) then fall back to the worktree-root location.
$Config = Join-Path $Workspace "_config\pipeline.yaml"
if (-not (Test-Path $Config)) {
    $Config = Join-Path (Join-Path $Workspace "..\..\") "_config\pipeline.yaml"
    $Config = (Resolve-Path $Config).Path
}
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

# Write the per-character manifest before exiting so stage completion
# survives a Claude/dispatcher crash. The launcher is the source of
# truth for stage state — agents only verify, they don't write.
$UpdateScript = Join-Path $Workspace "tools\_update_manifest.py"
if (Test-Path $UpdateScript) {
    & python $UpdateScript --char $Char --workspace $Workspace `
        --stage "03_glb_export" --exit $code 2>&1 |
        ForEach-Object { Write-Host $_ }
}
exit $code
