---
description: Read Claude's last reply aloud
disable-model-invocation: true
---

Run exactly this one bash command:

```
sh "${CLAUDE_PLUGIN_ROOT}/scripts/speak.sh" --detach --previous --turn "${CLAUDE_SESSION_ID}" --project "${CLAUDE_PROJECT_DIR}"
```

If it printed a line starting with "error:", show that line (and the "log:"
line) to the user and nothing else. Otherwise reply with only "🔊 reading
aloud", plus any line starting with "note:".
