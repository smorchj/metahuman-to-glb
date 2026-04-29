<#
.SYNOPSIS
  Stage 00 launcher — drive UE build_meta_human on an unrigged
  MetaHumanCharacter asset so stage 01 has assembled /Game/<Name>/
  content to FBX-export.

.USAGE
  ./run_assemble.ps1 -Char gabo -Pipeline cinematic

.NOTES
  - UE editor must be CLOSED for the project.
  - UE's -ExecCmds parser mangles paths with spaces. We copy the
    Python script into C:\tmp\mh\ before launching.
  - Runs with -unattended so modal dialogs (Restore Packages etc.)
    auto-dismiss.
#>
param(
    [Parameter(Mandatory=$true)][string]$Char,
    [string]$Pipeline = "cinematic"
)

$ErrorActionPreference = "Stop"

$ToolsDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$Workspace = (Resolve-Path (Join-Path $ToolsDir "..\..\..")).Path   # <pipeline root>

$ConfigPath = Join-Path $Workspace "_config\pipeline.yaml"
if (-not (Test-Path $ConfigPath)) {
    # Pipeline-local config not present — fall back to repo-root _config/
    $ConfigPath = Join-Path (Join-Path $Workspace "..\..\") "_config\pipeline.yaml"
    $ConfigPath = (Resolve-Path $ConfigPath).Path
}

function Get-YamlValue([string]$Path, [string]$Key) {
    $line = (Get-Content $Path | Where-Object { $_ -match "^\s*${Key}:" } | Select-Object -First 1)
    if (-not $line) { throw "missing key '$Key' in $Path" }
    $val = ($line -replace "^\s*${Key}:\s*","").Trim()
    if ($val.StartsWith('"') -and $val.EndsWith('"')) { $val = $val.Substring(1, $val.Length-2) }
    return $val
}

# For MetaHumanCharacter plugin assemble, we need the GUI editor (not -Cmd),
# because build_meta_human relies on Slate ticks.
$UEEditor  = Get-YamlValue $ConfigPath "ue_editor_cmd"
$UEEditor  = $UEEditor -replace "UnrealEditor-Cmd\.exe$", "UnrealEditor.exe"

$CharManifest = Join-Path $Workspace "characters\$Char\manifest.json"
if (-not (Test-Path $CharManifest)) { throw "character manifest not found: $CharManifest" }
$Manifest = Get-Content $CharManifest | ConvertFrom-Json
$MhFolder = $Manifest.mh_folder
if (-not $MhFolder) { throw "mh_folder not set in $CharManifest" }
$AssetPath = "$MhFolder.$(Split-Path $MhFolder -Leaf)"

# Per-character override: if the character's manifest declares
# ue_project_path, use it. Lets one pipeline serve characters in
# different .uproject files (e.g. MetaHumans.uproject for old imports
# vs MetaHumans3.uproject with the MetaHumanCharacter plugin).
$UEProject = Get-YamlValue $ConfigPath "ue_project_path"
if ($Manifest.ue_project_path) { $UEProject = $Manifest.ue_project_path }

# Pre-clean state
if (-not (Test-Path "C:\tmp\mh")) { New-Item -ItemType Directory -Path "C:\tmp\mh" | Out-Null }
$StatusPath = "C:/tmp/mh/status.json"
$OutputDir  = "C:/tmp/mh/out"
if (Test-Path $StatusPath) { Remove-Item $StatusPath }
if (Test-Path $OutputDir)  { Remove-Item $OutputDir -Recurse -Force }
New-Item -ItemType Directory -Path $OutputDir | Out-Null

# Copy the Python script to a spaceless path — UE -ExecCmds splits on whitespace.
$PyScript = Join-Path $ToolsDir "build_metahuman.py"
Copy-Item -Force $PyScript "C:\tmp\mh\build_mh.py"

$ExecCmd = "py C:/tmp/mh/build_mh.py -- --asset=$AssetPath --status=$StatusPath --output-dir=$OutputDir --name=$Char --pipeline=$Pipeline --timeout=1800"

Write-Host "[stage00] UE project : $UEProject"
Write-Host "[stage00] asset      : $AssetPath"
Write-Host "[stage00] pipeline   : $Pipeline"
Write-Host "[stage00] status     : $StatusPath"

& $UEEditor $UEProject -ExecCmds="$ExecCmd" -unattended -nosplash
$code = $LASTEXITCODE
Write-Host "[stage00] UE exit code: $code"
if (Test-Path $StatusPath) {
    Write-Host "[stage00] final status:"
    Get-Content $StatusPath | Write-Host
}
exit $code
