#!/usr/bin/env python3
"""Stand-in for `claude -p --output-format json`. Behavior comes from the --model value:

  ok              one result object, exit 0
  slow-<sec>      sleep, then ok
  hang            sleep forever (killed by signal)
"""
import json
import sys
import time

args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("0.0.0 (Claude Code fake)")
    sys.exit(0)
model = args[args.index("--model") + 1] if "--model" in args else "ok"
sys.stdin.read()


def ok():
    print(json.dumps({"type": "result", "is_error": False,
                      "result": "Looks fine. `a.py:1` — low — nit.",
                      "usage": {"input_tokens": 10, "output_tokens": 5}}))
    sys.exit(0)


if model == "ok":
    ok()
elif model.startswith("slow-"):
    time.sleep(float(model.split("-", 1)[1]))
    ok()
elif model == "hang":
    while True:
        time.sleep(1)
else:
    print(f"unknown fake model {model}", file=sys.stderr)
    sys.exit(3)
