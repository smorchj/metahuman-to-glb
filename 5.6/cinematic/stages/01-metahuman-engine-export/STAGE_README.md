# Stage 01 — MetaHuman Engine Export (Version Router)

This stage is **version-routed**. The active UE version is declared in
`_config/pipeline.yaml` → `ue_version`.

## Route

| `ue_version` | Subfolder |
|---|---|
| `5.6.1` | `5.6.1/` |
| `5.7.0` | `5.7.0/` (future) |

Load **only** the matching subfolder's `CONTEXT.md`. Do not mix tools across versions —
MetaHuman asset structure and Python APIs shift between UE point releases.

## If your version isn't here

1. Copy the closest existing version folder (usually the newest below yours).
2. Rename it to your version.
3. Read that subfolder's `CONTEXT.md` and update anything marked with a version caveat.
4. Test on one character before batching.

Do not edit a different version's folder to fit your version — that breaks reproducibility
for anyone else on that version.
