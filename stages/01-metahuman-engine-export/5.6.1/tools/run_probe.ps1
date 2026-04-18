param(
    [Parameter(Mandatory=$true)][string]$Char
)

$ErrorActionPreference = "Stop"

$ToolsDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$Workspace = (Resolve-Path (Join-Path $ToolsDir "..\..\..\..")).Path
$WorkspaceFwd = $Workspace -replace '\\','/'

$Config = Join-Path $Workspace "_config\pipeline.yaml"
$UEEditor  = ((Select-String -Path $Config -Pattern '^\s*ue_editor_cmd:' | Select-Object -First 1).Line `
    -replace '^\s*ue_editor_cmd:\s*"?([^"]+)"?\s*$', '$1').Trim()
$UEProject = ((Select-String -Path $Config -Pattern '^\s*ue_project_path:' | Select-Object -First 1).Line `
    -replace '^\s*ue_project_path:\s*"?([^"]+)"?\s*$', '$1').Trim()

$PyScript = Join-Path $ToolsDir "probe_bp.py"
$PyScriptFwd = $PyScript -replace '\\','/'

$env:MH_PIPELINE_WORKSPACE = $WorkspaceFwd

Write-Host "[run_probe] UE       = $UEEditor"
Write-Host "[run_probe] project  = $UEProject"
Write-Host "[run_probe] char     = $Char"

& $UEEditor $UEProject `
    -run=pythonscript `
    -script="$PyScriptFwd -- --char=$Char" `
    -AllowCommandletRendering -RenderOffScreen `
    -unattended -nop4 -nosplash -stdout -FullStdOutLogOutput
$code = $LASTEXITCODE
Write-Host "[run_probe] UE exit code: $code"
exit $code
