---
description: Stop reading aloud
disable-model-invocation: true
---

Run exactly this one bash command, then reply with only "🔇 stopped":

```
sh "${CLAUDE_PLUGIN_ROOT}/scripts/speak.sh" --stop --turn "${CLAUDE_SESSION_ID}"
```
