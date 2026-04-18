# CLAUDE.md — Pipeline Agent Orientation

You are an agent operating inside an **Interpretable Context Methodology (ICM)** workspace
(Van Clief / Model Workspace Protocol). This file is Layer 0: system orientation.

## What this workspace does

Converts UE MetaHuman characters to web-ready GLB files via three deterministic stages:
  01 → export FBX + textures from UE
  02 → assemble + clean up in Blender
  03 → export GLB with web constraints

## How the workspace is organized

- `CONTEXT.md` (root)          — Layer 1: task routing. Read this first.
- `_config/pipeline.yaml`       — Layer 3: config shared across all stages
- `skills/*.md`                 — Layer 3: stable reference material (MH asset layout, FBX rules, etc.)
- `stages/<NN>-<name>/`         — one stage per numbered folder, strict boundary
  - `CONTEXT.md`                — Layer 2: the stage contract (Inputs / Process / Outputs)
  - `tools/`                    — scripts the stage runs
  - `references/`               — stage-specific reference files
- `characters/<id>/`            — Layer 4: per-character working artifacts
  - `manifest.json`             — per-character status, one record per stage
  - `source/`, `01-fbx/`, `02-blend/`, `03-glb/` — stage outputs

## Context discipline (the rule)

When working on stage N, **only load** that stage's `CONTEXT.md` + files it names in its
Inputs table + the current character's `characters/<id>/` folder. Do not load other stages.
This keeps total context low enough for Haiku to execute reliably.

Opus designs and edits the contracts. Haiku runs them.

## Spawning a Haiku agent for one stage

From the root `CONTEXT.md`, the orchestration pattern is:

  For character <id> at stage <NN>:
    prompt = stages/<NN>-*/CONTEXT.md + characters/<id>/ + stage's Inputs files
    tools  = only tools in stages/<NN>-*/tools/
    model  = claude-haiku-4-5

Haiku's job is narrow: read Inputs table, invoke the stage's one launcher script,
verify outputs match the Outputs table, update `characters/<id>/manifest.json`.

## Rules

- Scripts are deterministic Python/PowerShell. LLMs glue, they don't transform.
- Every stage writes a machine-readable manifest. No stage reads another stage's internals.
- Fail loud with actionable messages. Never silently skip.
- Version-pinned stages (e.g. `01-metahuman-engine-export/5.6.1/`) — do not mix versions.
