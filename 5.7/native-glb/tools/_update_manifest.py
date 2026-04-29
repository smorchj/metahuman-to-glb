"""Atomic per-stage manifest update — invoked by every stage launcher
after the underlying tool returns.

Writing the manifest from the launcher (instead of relying on the
agent or the inner tool to do it) makes stage completion resilient:

  - If the inner tool (UE-Cmd, Blender, Python) crashes, the launcher
    still writes a "failed" status with the exit code captured in
    errors[].
  - If Claude Desktop / the dispatcher crashes after the launcher
    returned but before the agent's manifest-write tool call, the
    manifest is already written.
  - If the agent itself fails to follow the contract, the manifest is
    still authoritative.

Invoked as:
    python _update_manifest.py --char <id> --workspace <abs> \
                               --stage <key> --exit <code>           \
                               [--errors "msg1" "msg2" ...]

`<key>` is the manifest stage key (e.g. `00_unreal_assemble`,
`02_blender_assemble`).

On `--exit 0` the stage's status becomes `"done"`. On non-zero, it
becomes `"failed"` and `--errors` lines (plus a default exit-code
note) populate `errors[]`. `started_at` is set on first invocation;
`completed_at` is set on every invocation.

Pure stdlib. No deps. Safe to run idempotently.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path


def _iso_now() -> str:
    return (
        _dt.datetime.now(_dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--char", required=True)
    p.add_argument("--workspace", required=True,
                   help="absolute path to the pipeline root (e.g. .../5.7/native-glb)")
    p.add_argument("--stage", required=True,
                   help="stage key in manifest.stages, e.g. 00_unreal_assemble")
    p.add_argument("--exit", type=int, required=True,
                   help="exit code of the underlying launcher (0 = done)")
    p.add_argument("--errors", nargs="*", default=[],
                   help="optional error messages to append on failure")
    args = p.parse_args()

    char_root = Path(args.workspace) / "characters" / args.char
    manifest_path = char_root / "manifest.json"
    if not manifest_path.exists():
        # No manifest — nothing to update. This usually means bootstrap
        # never ran for this character. Fail loud so the operator
        # notices.
        print(f"[update_manifest] ERROR: manifest not found at {manifest_path}",
              file=sys.stderr)
        return 1

    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    stages = data.setdefault("stages", {})
    if args.stage not in stages:
        print(f"[update_manifest] ERROR: stage '{args.stage}' missing from "
              f"manifest. Known: {sorted(stages.keys())}",
              file=sys.stderr)
        return 1

    block = stages[args.stage]
    now = _iso_now()
    if not block.get("started_at"):
        block["started_at"] = now
    block["completed_at"] = now

    if args.exit == 0:
        block["status"] = "done"
        block["errors"] = []
    else:
        block["status"] = "failed"
        existing = list(block.get("errors") or [])
        existing.append(f"launcher exit code {args.exit}")
        existing.extend(args.errors)
        block["errors"] = existing

    manifest_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"[update_manifest] {args.char}.{args.stage} = {block['status']} "
          f"(launcher exit {args.exit})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
