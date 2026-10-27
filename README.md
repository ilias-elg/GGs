# Fire Nation Bot

The Grand Archivist is a conversational Discord agent for the Fire Nation with:

- natural follow-up conversations for 20 minutes after he is addressed;
- short-term channel context plus persistent user/server memories;
- live Roblox monitoring, web search, webpage reading, vision, voice, and Discord tools;
- an automatic and manual merit system with current and all-time leaderboards;
- safe deterministic tools for calculations and current time;
- confirmation-gated Discord mutations and optional local workspace tasks.

He answers to **"archivist"** or **"grand archivist"** (set with `BOT_CALL_NAMES`), @mentions and DMs — and only from the Owner, the Fire Lord and his access list. "Dismissed" from any of them ends the conversation at once, even mid-reply.

## Layout

```
main.py            entry point the host runs — only starts bob/app.py
config.py          every setting, read from .env
requirements.txt   exact versions (the host reinstalls on every start)
bob/
  app.py           the Discord client: startup, and deciding which messages he answers
  owner_alerts.py  errors are DMed to the Owner instead of shown in the channel
  ai/              model providers (Gemini, Groq, OpenAI, Anthropic)
  conversation/    the chat pipeline: prompt building, tool loop, memory
  tools/           the functions the AI can call
  voice/           the voice connection and speech queue
  features/
    fire_nation/     merits, conclusions, ranks, knowledge base, standing orders, voice, tone, diagnostics
      commands/      slash commands, one cog per area: merits.py, admin.py, voice.py
    roblox_monitor/  Roblox group tracking and the live dashboard
docs/              reference material (damage calculator spreadsheet)
data/              runtime state and the local database, created on first run — not in git
```

To add a new area of slash commands, add a cog in `bob/features/fire_nation/commands/` that subclasses `FireNationCog` (it gets `self.bot`, `self.voice_manager` and the shared error handler) and list it in `COGS` in that folder's `__init__.py`.

## Optional local workspace tasks

Local execution is disabled by default. To enable it, add these values to `.env`:

```env
LOCAL_TASKS_ENABLED=true
LOCAL_TASK_USER_IDS=123456789012345678
LOCAL_TASK_WORKSPACE=C:\path\to\the\workspace
```

Only the listed Discord user IDs, or a server administrator, can request local tasks. Commands run without a shell, stay inside `LOCAL_TASK_WORKSPACE`, have a timeout, and require a follow-up `yes` confirmation from the requesting user. With this enabled, he also gets workspace tools for listing, reading, searching, writing, and deleting files; writes and deletes require confirmation.

## Conversation controls

```env
BOT_CALL_NAMES=grand archivist,archivist
AUTO_FOLLOW_UPS=true
CONVERSATION_TTL_SECONDS=1200
MAX_CONTEXT_MESSAGES=5
```

Set `AUTO_FOLLOW_UPS=false` if he should only answer DMs, mentions, or messages containing one of his call names.

`FULL_DISCORD_TOOLS=true` gives him the complete Discord toolset in server conversations, so natural requests such as “make a new VC” are routed to the real channel-management tools. `FULL_TOOLSET=true` exposes every built-in tool instead of choosing them by intent. Both are off by default; Discord permissions and confirmation gates apply either way.

## Merits, ranks, knowledge and voice

Slash commands:

| Command | Who | What |
| --- | --- | --- |
| `/merits` | everyone | A member's current and all-time totals, or the leaderboard |
| `/leaderboard board:` | everyone | The **Current** leaderboard (since the last reset) or the **All Time** one |
| `/addmerit exam` / `training` / `event` | HR+ | +1 to the host and the counted pings in the pasted conclusion; a co-host gets an extra 0.5; spectators get 1 |
| `/addmerit raid` / `bonus`, `/removemerit` | Advisor+ | +3 per raid participant (spectators 1); 0.1–50 bonus with a reason, or removal |
| `/merithistory`, `/addknowledge`, `/reloadknowledge` | HR+ | Ledger history; knowledge base entries |
| `/createhr`, `/createadvisor` | Royalty+ | Create the rank roles (no Discord permissions) |
| `/createroyalty`, `/resetdata`, `/diagnostics` | Owner / Fire Lord | Role, reset the current leaderboard (backup posted first), system check |
| `/access add` / `remove` / `list` | Owner / Fire Lord | Who he holds conversations with (also: "archivist, talk to @user") |
| `/cleanup` | Owner / Fire Lord | Delete his own messages from the last 10 minutes (or `minutes:`) in a channel, or with `server:` in every channel of any server he is in |
| `/voice join` / `leave` / `say` / `status` / `greeting …` | Owner, Fire Lord, access list | Voice — works from any server he shares with you |

Ranks: Owner and Fire Lord come from the user IDs below; Royalty, Advisor and HR are Discord roles with exactly those names. Merits live in Postgres (`DATABASE_URL`, table `merit_awards`). Every award, removal and reset is posted to the owner log channel.

**Conclusion posts.** Posts titled "... Exam/Training/Event/Raid Concluded" in the channels listed in `MERIT_CONCLUSION_CHANNEL_IDS` are counted automatically: he posts an Approve / Reject card in the owner log channel and records it once someone with the rank approves. The author is the host. What counts:

| Post | Earns the merit | Gets nothing |
| --- | --- | --- |
| Exam / induction (1) | `Co-host:`, `Guards:` | `Passed:` |
| Training (1) | `Co-host:`, `Attendees:` | winners, teams |
| Event (1), Raid (3) | every ping | — |

Any post can also have a `Spectators:` line (or "thanks @a @b for spectating"): those people get **1 merit** whatever the activity pays. The person pinged after "Co-host" gets +0.5 on top. Anyone who is not a member of the home server is left out. `/addmerit` reads the pasted conclusion the same way, and its `spectated_by` option adds spectators that are not on the post.

**Leaderboards.** `/resetdata` starts the current leaderboard over; nothing is deleted — the rows are stamped with the reset time and stay on the all-time leaderboard. Once a day at midnight UTC, anyone who is no longer in the home server (`MERIT_HOME_GUILD_ID`, Fire Nation Military by default) is taken off both; their records move to `merit_awards_archive`.

The same merit actions work in conversation ("archivist, give narek 2 bonus merits for carrying the raid"), with the same rank checks. The Owner and Fire Lord can also give him standing orders ("from now on keep replies short"), which are saved and applied to every later conversation.

`bob/features/fire_nation/fire-nation-knowledge.txt` is the knowledge base; sections whose `ALIASES` match a message are added to that reply's context. Entries added with `/addknowledge` are stored in the data directory.

**Voice.** He greets the Owner, Fire Lord, access-list members and anyone given a custom greeting when they join his channel, reads announcement-channel posts aloud, and leaves after 15 minutes alone. `/voice say` picks its tone from how the text is written (capitals, `!!`, `...`, emoji, a leading `[whisper]` tag) unless a `tone:` is chosen. "archivist, join vc", "leave vc", "say that out loud" and "voice status" work from chat. "archivist, go to sleep" / "archivist, wake up" take him offline and back. Voice needs discord.py 2.7+ with `davey` (Discord's DAVE encryption) and libopus on the host — `/diagnostics` checks both.

```env
DISCORD_OWNER_USER_IDS=123456789012345678
DISCORD_SECOND_IN_COMMAND_USER_IDS=123456789012345678
DISCORD_OWNER_LOG_CHANNEL_ID=123456789012345678
DATABASE_URL=postgresql://user:password@host/dbname   # Neon, Supabase, Railway…
MERIT_CONCLUSION_CHANNEL_IDS=                # channels whose "... Concluded" posts are counted
# Optional
MERIT_HOME_GUILD_ID=                         # default: Fire Nation Military
DISCORD_HR_ROLE_IDS=                         # extra role IDs that count as HR
DISCORD_TEST_GUILD_ID=                       # register slash commands to one server instantly
GOOGLE_API_KEY=                              # Gemini chat and text-to-speech
AI_PROVIDER=gemini
GEMINI_MODEL=
GEMINI_FALLBACK_MODELS=                      # comma-separated; tried in order when a model is busy or out of quota
TTS_VOICE=Algenib                            # audition others with /diagnostics tts:True voice:<name>
TTS_MODEL=                                   # default: the first flash TTS model the key can see
TTS_DELIVERY=                                # default style direction placed before each spoken line
ANNOUNCEMENT_CHANNEL_IDS=                    # ordinary text channels to read aloud like announcement channels
AVATAR_URLS=                                 # image URLs to rotate the avatar through every 4 hours
BOT_DATA_DIR=                                # default: ./data
```

Runtime state lives in `data/`: `roblox_monitor.db` (Roblox scans, chat history, memories — moved here automatically from the project folder by older versions), `bot-access.txt` (one user ID per line), `standing-orders.json`, `voice-greetings.json`, `knowledge-additions.txt` and cached voice clips. Optional artwork goes in `assets/`: `avatar-online.gif|png`, `avatar-offline.png` (shown while asleep or shut down) and `banner.gif|png`.
