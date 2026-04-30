<#
.SYNOPSIS
  Stage 00 launcher - drive UE build_meta_human on an unrigged
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

# Single exit point that ALSO writes characters/<char>/manifest.json
# before exiting. Makes stage completion resilient to Claude/dispatcher
# crashes — even if the agent dies after the launcher returned, the
# manifest is already authoritative.
function Exit-Stage([int]$Code) {
    $UpdateScript = Join-Path $Workspace "tools\_update_manifest.py"
    if (Test-Path $UpdateScript) {
        & python $UpdateScript `
            --char $Char `
            --workspace $Workspace `
            --stage "00_unreal_assemble" `
            --exit $Code 2>&1 | ForEach-Object { Write-Host $_ }
    }
    exit $Code
}

$ConfigPath = Join-Path $Workspace "_config\pipeline.yaml"
if (-not (Test-Path $ConfigPath)) {
    # Pipeline-local config not present - fall back to repo-root _config/
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

# The pipeline folder path tells us which UE version to use. Workspace
# resolves to e.g. "C:/.../5.7/native-glb" so the grandparent folder
# name ("5.7") is the UE version. This avoids depending on the root
# ue_version flag in _config/pipeline.yaml, which is shared across all
# pipelines and can point at a different UE.
$PipelineVer = Split-Path -Leaf (Split-Path -Parent $Workspace)

# Pull per-version UE paths out of the ue_by_version block in the yaml.
# Minimal inline parser - lines are indented under the version key.
# State is a small int to avoid PowerShell's "$true -eq 'found' => True"
# coercion pitfall.
#   0 = outside ue_by_version block
#   1 = inside the block, looking for the target version key
#   2 = inside the target version's sub-block, scanning keys
function Get-YamlVersionPath([string]$Path, [string]$Version, [string]$Key) {
    $state = 0
    $verPat = "^\s*""?" + [regex]::Escape($Version) + """?:\s*$"
    $keyPat = "^\s+${Key}:\s*(.*)$"
    $nextVerPat = "^  ""?[^""]+""?:\s*$"
    foreach ($line in Get-Content $Path) {
        switch ($state) {
            0 {
                if ($line -match "^ue_by_version:\s*$") { $state = 1 }
            }
            1 {
                if ($line -match $verPat) { $state = 2 }
                elseif ($line -match "^[^\s#]") { $state = 0 }  # left block
            }
            2 {
                if ($line -match $keyPat) {
                    $val = $matches[1].Trim()
                    if ($val.StartsWith('"') -and $val.EndsWith('"')) {
                        $val = $val.Substring(1, $val.Length - 2)
                    }
                    return $val
                }
                if ($line -match $nextVerPat) { $state = 1 }  # moved to next version
                elseif ($line -match "^[^\s#]") { $state = 0 }  # left block entirely
            }
        }
    }
    throw "missing ue_by_version['$Version'].$Key in $Path"
}

$UEProject = Get-YamlVersionPath $ConfigPath $PipelineVer "project_path"
$UEEditor  = Get-YamlVersionPath $ConfigPath $PipelineVer "editor_cmd"
# For MetaHumanCharacter plugin assemble we need the GUI editor (not -Cmd),
# because build_meta_human relies on Slate ticks.
$UEEditor  = $UEEditor -replace "UnrealEditor-Cmd\.exe$", "UnrealEditor.exe"

$CharManifest = Join-Path $Workspace "characters\$Char\manifest.json"
if (-not (Test-Path $CharManifest)) { throw "character manifest not found: $CharManifest" }
$Manifest = Get-Content $CharManifest | ConvertFrom-Json
$MhFolder = $Manifest.mh_folder
if (-not $MhFolder) { throw "mh_folder not set in $CharManifest" }
$AssetPath = "$MhFolder.$(Split-Path $MhFolder -Leaf)"

# Per-character override if the manifest declares ue_project_path.
# Lets one pipeline serve characters in different .uproject files
# (e.g. an older MetaHumans.uproject vs a new one with the
# MetaHumanCharacter plugin).
if ($Manifest.ue_project_path) { $UEProject = $Manifest.ue_project_path }

# --- Thumbnail review (pre-build) ------------------------------------
# Extract the Content Browser thumbnail embedded in the .uasset so
# the operator can eyeball the character before the expensive UE
# build cycle begins. Runs standalone Python (no UE needed).
$ContentDir = Join-Path (Split-Path -Parent $UEProject) "Content"
$MhRelative = ($MhFolder -replace "^/Game/", "").Replace("/", "\")
$UassetPath = Join-Path $ContentDir "$MhRelative.uasset"

$ThumbOut = Join-Path $Workspace "characters\$Char\source\thumbnail.jpg"
$ThumbScript = Join-Path $ToolsDir "extract_thumbnail.py"

if (Test-Path $UassetPath) {
    Write-Host "[stage00] extracting thumbnail from $UassetPath"
    & python $ThumbScript --uasset $UassetPath --out $ThumbOut
    if ($LASTEXITCODE -eq 0) {
        $kb = [math]::Round((Get-Item $ThumbOut).Length / 1KB)
        Write-Host "[stage00] thumbnail ready: $ThumbOut (${kb} KB)"
    } else {
        Write-Host "[stage00] thumbnail extraction failed (non-fatal, continuing)"
    }
} else {
    Write-Host "[stage00] .uasset not found at $UassetPath - skipping thumbnail"
}
# ---------------------------------------------------------------------

# Pre-clean state
if (-not (Test-Path "C:\tmp\mh")) { New-Item -ItemType Directory -Path "C:\tmp\mh" | Out-Null }
$StatusPath = "C:/tmp/mh/status.json"
$OutputDir  = "C:/tmp/mh/out"
if (Test-Path $StatusPath) { Remove-Item $StatusPath }
if (Test-Path $OutputDir)  { Remove-Item $OutputDir -Recurse -Force }
New-Item -ItemType Directory -Path $OutputDir | Out-Null

# Copy the Python script to a spaceless path - UE -ExecCmds splits on whitespace.
$PyScript = Join-Path $ToolsDir "build_metahuman.py"
Copy-Item -Force $PyScript "C:\tmp\mh\build_mh.py"

# Pipeline workspace path has spaces ("Metahuman to GLB"), so we can't
# pass it through -ExecCmds positional args (the parser splits on
# whitespace and breaks argparse). Set it as an env var; build_mh.py
# reads it via os.environ to know where to write the reference screenshot.
$env:MH_PIPELINE_WORKSPACE = $Workspace

$ExecCmd = "py C:/tmp/mh/build_mh.py -- --asset=$AssetPath --status=$StatusPath --output-dir=$OutputDir --name=$Char --pipeline=$Pipeline --timeout=1800"

Write-Host "[stage00] UE version  : $PipelineVer"
Write-Host "[stage00] UE project  : $UEProject"
Write-Host "[stage00] UE editor   : $UEEditor"
Write-Host "[stage00] asset       : $AssetPath"
Write-Host "[stage00] pipeline    : $Pipeline"
Write-Host "[stage00] status      : $StatusPath"

# Launch UE and wait for it to exit. `&` returns immediately for GUI
# apps (that's why earlier runs looked "done" while UE was still
# baking). Start-Process -PassThru gives us a handle so we can poll
# status.json and force-kill if quit_editor() hangs.
#
# Path has spaces ("Unreal Projects"), so ArgumentList MUST be built
# as a single pre-quoted string; the array form doesn't quote paths
# for us and UE silently ignores the project arg, opening the Home
# screen instead.
$argString = '"' + $UEProject + '" -ExecCmds="' + $ExecCmd + '" -unattended -nosplash'
$proc = Start-Process -FilePath $UEEditor -ArgumentList $argString -PassThru
Write-Host "[stage00] UE pid=$($proc.Id) - waiting on status.json + process exit"

$terminalPhases = @("DONE", "FAILED")
$maxWait = 1800   # seconds - hard upper bound
$pollInterval = 2
$elapsed = 0
$lastPhase = ""
while (-not $proc.HasExited -and $elapsed -lt $maxWait) {
    Start-Sleep -Seconds $pollInterval
    $elapsed += $pollInterval
    if (Test-Path $StatusPath) {
        try {
            $s = Get-Content $StatusPath -Raw | ConvertFrom-Json
            if ($s.phase -ne $lastPhase) {
                Write-Host "[stage00] phase=$($s.phase)  (t=${elapsed}s)"
                $lastPhase = $s.phase
            }
            if ($terminalPhases -contains $s.phase) {
                # Python hit DONE/FAILED. Give quit_editor() up to 60s
                # to close the editor cleanly, then force.
                $quitDeadline = $elapsed + 60
                while (-not $proc.HasExited -and $elapsed -lt $quitDeadline) {
                    Start-Sleep -Seconds $pollInterval
                    $elapsed += $pollInterval
                }
                if (-not $proc.HasExited) {
                    Write-Host "[stage00] editor did not self-quit; terminating"
                    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
                }
                break
            }
        } catch {
            # file may be mid-write; ignore and retry
        }
    }
}

if (-not $proc.HasExited) {
    Write-Host "[stage00] timeout ${maxWait}s - terminating editor"
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
}

# Status.json is the source of truth, NOT $proc.ExitCode. UE returns
# -1 when force-killed even after a clean DONE write, which mis-reports
# success as failure. Derive the launcher's exit code from the phase.
$ueCode = $proc.ExitCode
Write-Host "[stage00] UE process exit code: $ueCode (informational)"
if (Test-Path $StatusPath) {
    Write-Host "[stage00] final status:"
    Get-Content $StatusPath | Write-Host

    # Copy UE's reference screenshot from <UEProject>/Saved/Screenshots/
    # Windows/ to characters/<char>/source/reference.png. The Python
    # side (build_metahuman.py) queues HighResShot before quit_editor
    # but can't wait for the file (would block UE's tick). We pick up
    # here, after UE has exited and the file has actually landed.
    try {
        $UEProjDir = Split-Path -Parent $UEProject
        $ShotsDir = Join-Path $UEProjDir "Saved\Screenshots\Windows"
        if (Test-Path $ShotsDir) {
            $newest = Get-ChildItem $ShotsDir -Filter "HighresScreenshot*.png" -ErrorAction SilentlyContinue |
                      Where-Object { $_.LastWriteTime -gt $proc.StartTime } |
                      Sort-Object LastWriteTime -Descending |
                      Select-Object -First 1
            if ($newest) {
                $RefDir = Join-Path $Workspace "characters\$Char\source"
                if (-not (Test-Path $RefDir)) {
                    New-Item -ItemType Directory -Path $RefDir -Force | Out-Null
                }
                $RefPath = Join-Path $RefDir "reference.png"
                Copy-Item -Force $newest.FullName $RefPath
                $kb = [math]::Round($newest.Length / 1KB)
                Write-Host "[stage00] reference.png copied: $($newest.Name) -> $RefPath (${kb} KB)"
            } else {
                Write-Host "[stage00] no new screenshot found in $ShotsDir (HighResShot may have failed)"
            }
        } else {
            Write-Host "[stage00] UE screenshot dir not found: $ShotsDir"
        }
    } catch {
        Write-Host "[stage00] reference screenshot copy failed: $_"
    }

    try {
        $final = Get-Content $StatusPath -Raw | ConvertFrom-Json
        switch ($final.phase) {
            "DONE"   { Exit-Stage 0 }
            "FAILED" { Exit-Stage 2 }
            default  {
                Write-Host "[stage00] non-terminal phase '$($final.phase)' - treating as failure"
                Exit-Stage 3
            }
        }
    } catch {
        Write-Host "[stage00] could not parse status.json: $_"
        Exit-Stage 4
    }
}
Write-Host "[stage00] no status.json was written - UE never ran the script"
Exit-Stage 5
