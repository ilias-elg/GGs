# Fire Nation Bot

Bob is a conversational Discord agent with:

- natural follow-up conversations for 20 minutes after he is addressed;
- short-term channel context plus persistent user/server memories;
- live Roblox monitoring, web search, webpage reading, vision, voice, and Discord tools;
- safe deterministic tools for calculations and current time;
- confirmation-gated Discord mutations and optional local workspace tasks.

## Optional local workspace tasks

Local execution is disabled by default. To enable it, add these values to `.env`:

```env
LOCAL_TASKS_ENABLED=true
LOCAL_TASK_USER_IDS=123456789012345678
LOCAL_TASK_WORKSPACE=C:\path\to\the\workspace
```

Only the listed Discord user IDs can request local tasks. Commands run without a shell, stay inside `LOCAL_TASK_WORKSPACE`, have a timeout, and require a follow-up `yes` confirmation from the requesting user.

## Conversation controls

```env
AUTO_FOLLOW_UPS=true
CONVERSATION_TTL_SECONDS=1200
MAX_CONTEXT_MESSAGES=30
```

Set `AUTO_FOLLOW_UPS=false` if Bob should only answer DMs, mentions, or messages containing his name.
