# CLAUDE.md — Pipeline Agent Orientation

You are an agent in an **Interpretable Context Methodology (ICM)** workspace
(Van Clief / Model Workspace Protocol). This file is Layer 0: system orientation.

## What this workspace does

Converts a UE MetaHuman character into a web-ready GLB via 5 deterministic stages.
The default pipeline is **`5.7/native-glb/`** (UE 5.7 + native-GLB output, ARKit-52
blendshapes baked via UE Sequencer). 5.6/cinematic is the legacy reference.

## When the operator asks you to export a character

If the operator's request is anything like *"export this character"*, *"please run
the pipeline on `<asset>`"*, or just sends a UE asset path, **read
`5.7/native-glb/RUN.md` and follow it exactly**. RUN.md is fully self-contained:
it tells you to bootstrap the character folder and then dispatch one Haiku
sub-agent per stage in sequence.

You do not need to read any other CONTEXT.md, the per-stage Python sources, or
the operator's UE project. RUN.md handles all of that.

## How the workspace is organized

```
<worktree>/
  CONTEXT.md                           ← root task routing
  CLAUDE.md                            ← this file (auto-loaded)
  _config/pipeline.yaml                ← shared config (UE + Blender paths)
  5.7/native-glb/                      ← active pipeline
    RUN.md                             ← operator entry point ★
    tools/bootstrap_character.py       ← character-folder scaffolder
    stages/00-unreal-assemble/
      CONTEXT.md                       ← stage contract (Haiku reads only this)
      tools/run_assemble.ps1           ← stage launcher
    stages/01-unreal-glb-export/
    stages/02-blender-assemble/
    stages/03-export-to-glb/
    stages/04-webview-build/
    characters/_template/              ← copied per character
    characters/<id>/                   ← per-character working artifacts
      manifest.json                    ← per-stage status
      source/, 01-glb/, 02-blend/, 03-glb/   ← stage outputs (gitignored)
```

## Stage isolation (the hard rule)

When running a single stage, **only load** that stage's `CONTEXT.md` + the files
it names in its Inputs table + the current character's `characters/<id>/`.
Do not load other stages' contracts, other characters' manifests, or pipeline
code outside `stages/<NN>/tools/`.

A stage Haiku updates **only** its own `stages.<NN>_<key>` block in the
character manifest — never another stage's status, even if state looks
inconsistent. The orchestrator (RUN.md flow) owns cross-stage state.

## Roles

| Model | Job |
|---|---|
| Haiku (sub-agent per stage) | Read one stage's CONTEXT.md, run its launcher, verify outputs, update its manifest block. |
| Haiku (orchestrator) | Read RUN.md, run bootstrap, spawn 5 stage sub-agents in sequence, report. |
| Opus (you, when invoked) | Edit contracts, add stages, add new pipelines, debug failures the orchestrator escalates. Don't run stages — that's Haiku's job. |

## Rules

- Scripts (`*.py`, `*.ps1`) are deterministic. LLMs glue and verify; scripts transform.
- Every stage writes a machine-readable manifest. Stages don't read each other.
- Fail loud with actionable messages. Never silently skip.
- Per-version + per-pipeline boundary is hard. `5.7/native-glb/` does not import
  from `5.6/cinematic/`.
