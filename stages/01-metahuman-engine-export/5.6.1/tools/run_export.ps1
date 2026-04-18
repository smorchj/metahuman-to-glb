<#
.SYNOPSIS
  Launch UE 5.6.1 headless and run export_mh.py for one character.

.USAGE
  ./run_export.ps1 -Char ada

.NOTES
  UE's `-script=` parameter interprets backslashes as C-style escapes (\0, \5, \t, ...)
  inside the quoted value. Any path containing these sequences gets mangled. We sidestep
  that entirely by converting all paths we pass to UE into forward-slash form — Windows,
  Python, and UE all accept forward-slash paths.

  UE editor must be CLOSED on the target project.
#>

param(
    [Parameter(Mandatory=$true)][string]$Char
)

$ErrorActionPreference = "Stop"

# Resolve workspace root = four levels up from this script
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$Workspace  = (Resolve-Path (Join-Path $ScriptDir "..\..\..\..")).Path

$ConfigPath = Join-Path $Workspace "_config\pipeline.yaml"
if (-not (Test-Path $ConfigPath)) {
    throw "pipeline.yaml not found at $ConfigPath"
}

# --- minimal YAML parse: only the top-level keys we need ---
function Get-YamlValue([string]$Path, [string]$Key) {
    $line = (Get-Content $Path | Where-Object { $_ -match "^\s*${Key}:" } | Select-Object -First 1)
    if (-not $line) { throw "missing key '$Key' in $Path" }
    $val = ($line -replace "^\s*${Key}:\s*","").Trim()
    if ($val.StartsWith('"') -and $val.EndsWith('"')) { $val = $val.Substring(1, $val.Length-2) }
    return $val
}

$UEProject = Get-YamlValue $ConfigPath "ue_project_path"
$UEEditor  = Get-YamlValue $ConfigPath "ue_editor_cmd"

if (-not (Test-Path $UEEditor))  { throw "UnrealEditor-Cmd.exe not found: $UEEditor" }
if (-not (Test-Path $UEProject)) { throw "UE project not found: $UEProject" }

$PyScript = Join-Path $ScriptDir "export_mh.py"
if (-not (Test-Path $PyScript)) { throw "export_mh.py missing: $PyScript" }

# Convert all paths we pass to UE into forward-slash form so the -script= parser
# doesn't treat backslash sequences like \0, \5, \t as escape codes.
function ToFwd([string]$p) { return ($p -replace '\\','/') }

$WorkspaceFwd = ToFwd $Workspace
$PyScriptFwd  = ToFwd $PyScript

# Build the -script= value. Note: the workspace value must NOT be quoted inside the
# already-quoted -script= string, and we rely on forward slashes so there are no
# escape-code surprises. We avoid spaces in the *inner* args by using = with no spaces.
$ScriptArg = "$PyScriptFwd -- --char=$Char --workspace=$WorkspaceFwd"

Write-Host "[run_export] UE     = $UEEditor"
Write-Host "[run_export] proj   = $UEProject"
Write-Host "[run_export] script = $PyScriptFwd"
Write-Host "[run_export] wspace = $WorkspaceFwd"
Write-Host "[run_export] char   = $Char"
Write-Host ""

# The workspace path contains spaces ("Metahuman to GLB"). UE's -script= parser takes
# the whole quoted value and then splits the inner value on whitespace to separate the
# script path from its args. Our script path contains spaces too, which means we can't
# rely on that split. Workaround: pass workspace via environment variable instead of
# as a CLI arg. Keep --char as CLI (safe, no spaces).
$env:MH_PIPELINE_WORKSPACE = $WorkspaceFwd

# NOTES on flags:
#  -nullrhi:                  DO NOT USE — crashes skeletal mesh FBX export (MeshObject null)
#  -AllowCommandletRendering: required for skeletal mesh export paths (RHI/skinning access)
#  -RenderOffScreen:          init renderer without opening a window
#  -unattended -nosplash:     no prompts, no splash screen
& $UEEditor $UEProject `
    -run=pythonscript `
    -script="$PyScriptFwd -- --char=$Char" `
    -AllowCommandletRendering -RenderOffScreen `
    -unattended -nop4 -nosplash -stdout -FullStdOutLogOutput

$code = $LASTEXITCODE
Write-Host ""
Write-Host "[run_export] UE exit code: $code"
exit $code
