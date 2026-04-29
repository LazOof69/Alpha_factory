"""UserPromptSubmit hook — emits skill-routing reminder as additionalContext.

Reads the human-editable text in `skill-routing.txt` (next to this file) and
wraps it in the JSON envelope Claude Code expects so the harness injects it
into the model's context for the upcoming turn.

Stdin: receives `{}` for UserPromptSubmit (we don't need to inspect it).
Stdout: JSON envelope.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REMINDER_FILE = Path(__file__).resolve().parent / "skill-routing.txt"


def main() -> int:
    try:
        text = REMINDER_FILE.read_text(encoding="utf-8").rstrip()
    except OSError as e:
        # Fail silently — better to skip the reminder than to break the user's turn.
        print(json.dumps({"continue": True, "suppressOutput": True,
                          "systemMessage": f"skill-routing hook: {e}"}),
              file=sys.stderr)
        return 0

    envelope = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": text,
        }
    }
    print(json.dumps(envelope))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
