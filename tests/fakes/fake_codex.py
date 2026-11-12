#!/usr/bin/env python3
"""Stand-in for `codex exec --json`. Behavior comes from the --model value:

  ok              one agent_message and usage, exit 0
  slow-<sec>      sleep, then ok
  hang            sleep forever (ignores nothing; killed by signal)
  ratelimit       error event + stderr, exit 1
  auth            "not logged in" on stderr, exit 1
  empty           exit 0 without a message
  envdump         the review text is the process environment as JSON
  orphan          start a background child that outlives us, then ok
  stdin           the review text is the prompt read from stdin
"""
import json
import os
import sys
import time

args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("codex-cli 0.0.0-fake")
    sys.exit(0)
model = args[args.index("--model") + 1] if "--model" in args else "ok"


def ok(text="Looks fine. `a.py:1` — low — nit."):
    print(json.dumps({"type": "thread.started"}), flush=True)
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}))
    print(json.dumps({"type": "turn.completed", "usage": {
        "input_tokens": 1000, "cached_input_tokens": 400, "output_tokens": 50,
        "reasoning_output_tokens": 20}}))
    sys.exit(0)


if model == "ok":
    ok()
elif model.startswith("slow-"):
    time.sleep(float(model.split("-", 1)[1]))
    ok()
elif model == "hang":
    while True:
        time.sleep(1)
elif model == "ratelimit":
    print(json.dumps({"type": "error", "message": "You've hit your usage limit. Try again later."}))
    print("stream error: 429 Too Many Requests", file=sys.stderr)
    sys.exit(1)
elif model == "auth":
    print("Error: Not logged in. Please run codex login", file=sys.stderr)
    sys.exit(1)
elif model == "empty":
    sys.exit(0)
elif model == "orphan":
    import subprocess
    p = subprocess.Popen(["sleep", "300"])
    open("orphan.pid", "w").write(str(p.pid))   # in the working directory
    ok()
elif model == "stdin":
    ok(sys.stdin.read())
elif model == "envdump":
    ok(json.dumps(dict(os.environ)))
else:
    print(f"unknown fake model {model}", file=sys.stderr)
    sys.exit(3)
