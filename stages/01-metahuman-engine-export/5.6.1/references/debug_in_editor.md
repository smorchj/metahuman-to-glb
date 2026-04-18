# Debugging `export_mh.py` from inside the UE editor

When the headless run fails with an opaque error, it's faster to iterate inside an open
editor where you can see logs live and poke at assets.

## Setup

1. Open the UE project (5.6.1) in the editor.
2. `Window → Developer Tools → Output Log`.
3. In the Output Log, switch the command-line drop-down from `Cmd` to `Python`.

## Run the script

```python
exec(open(r"C:/Users/smorc/Metahuman to GLB/stages/01-metahuman-engine-export/5.6.1/tools/export_mh.py").read(), {"__name__": "not_main"})
```

Then:

```python
import sys, importlib, os
sys.path.insert(0, r"C:/Users/smorc/Metahuman to GLB/stages/01-metahuman-engine-export/5.6.1/tools")
import export_mh
importlib.reload(export_mh)
export_mh.main(char="ada", workspace=r"C:/Users/smorc/Metahuman to GLB")
```

Iterate: edit `export_mh.py`, re-run the `importlib.reload` + `main(...)` block.

## When to switch back to headless

Once the script runs clean in-editor, verify the headless path works by closing the
editor and running `tools/run_export.ps1 -Char ada`. The same script, same args, must
also succeed there — that's the real pipeline target.

## Common traps when running in-editor

- The script creates output under `characters/<char>/01-fbx/`. If that folder already
  contains artifacts from a prior run, `replace_identical=True` will overwrite FBXs.
- `unreal.EditorAssetLibrary.load_asset(...)` triggers a synchronous load; large MH
  packages take a beat.
- `unreal.Exporter.run_asset_export_task(task)` returns True on success. If it returns
  False the UE log usually explains why — check the Output Log *above* the Python line.
