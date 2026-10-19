---
description: Toggle reading every reply aloud automatically
argument-hint: on | off
disable-model-invocation: true
---

If the user gave no argument, ask whether they want auto-read `on` or `off`
instead of guessing. Otherwise run exactly this one bash command and show the
user its output:

```
sh "${CLAUDE_PLUGIN_ROOT}/scripts/speak.sh" --auto $ARGUMENTS --turn "${CLAUDE_SESSION_ID}"
```

Warning to relay when turning it on: replies arrive frequently — most people
prefer on-demand `/speak` after trying auto for a day. Sending a new prompt
stops the reading of the previous reply.
