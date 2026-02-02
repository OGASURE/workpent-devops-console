from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure repo root is on sys.path when running as a script:
#   python3 hyperdev/hyperdef_cli.py ...
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from backend.app.hyperdef_spec_v01 import validate_spec_v01


def cmd_validate(args: argparse.Namespace) -> int:
    spec_yaml = Path(args.file).read_text(encoding="utf-8")
    out = validate_spec_v01(spec_yaml, strict=True)
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        if out.get("ok"):
            print("OK")
            print("spec_hash=" + out.get("spec_hash", ""))
        else:
            print("INVALID")
            for e in out.get("errors", []):
                print(f"- {e.get('path')}: {e.get('message')}")
    return 0 if out.get("ok") else 2


def main() -> int:
    p = argparse.ArgumentParser(prog="hyperdef")
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate", help="Validate HyperDef spec YAML (strict)")
    v.add_argument("-f", "--file", required=True, help="Path to spec YAML")
    v.add_argument("--json", action="store_true", help="Emit JSON result")
    v.set_defaults(fn=cmd_validate)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
