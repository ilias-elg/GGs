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

Only the listed Discord user IDs, or a server administrator, can request local tasks. Commands run without a shell, stay inside `LOCAL_TASK_WORKSPACE`, have a timeout, and require a follow-up `yes` confirmation from the requesting user. With this enabled, Bob also gets workspace tools for listing, reading, searching, writing, and deleting files; writes and deletes require confirmation.

## Conversation controls

```env
AUTO_FOLLOW_UPS=true
CONVERSATION_TTL_SECONDS=1200
MAX_CONTEXT_MESSAGES=30
```

Set `AUTO_FOLLOW_UPS=false` if Bob should only answer DMs, mentions, or messages containing his name.

`FULL_DISCORD_TOOLS=true` is enabled by default. It gives Bob the complete Discord toolset in server conversations, so natural requests such as “make a new VC” and “delete the raid voice channel” are routed to the real channel-management tools. Discord permissions and confirmation gates still apply.

`FULL_TOOLSET=true` is enabled by default. It exposes all built-in web, Roblox, Discord, voice, vision, calculation, and workspace tools to the model so natural language is not blocked by intent keywords. External services such as Spotify, Trello API actions, email, or other apps still require their credentials and an adapter; the local task bridge can run an authorized integration script for those services.

## Merits, ranks, knowledge and voice (ported from Jarvis)

Bob only converses with the Owner, the Fire Lord and the people listed in `data/bot-access.txt`; messages from anyone else are ignored. Slash commands are open to whoever holds the rank each one needs.

Slash commands:

| Command | Who | What |
| --- | --- | --- |
| `/merits`, `/leaderboard` | everyone | A member's total, or the paged leaderboard |
| `/addmerit exam` / `event` | HR+ | +1 to every @mention in the pasted announcement and the host; a co-host gets an extra 0.5 |
| `/addmerit raid` / `bonus`, `/removemerit` | Advisor+ | +3 per raid participant; 0.1–50 bonus or removal |
| `/merithistory`, `/addknowledge`, `/reloadknowledge` | HR+ | Ledger history; knowledge base entries |
| `/createhr`, `/createadvisor` | Royalty+ | Create the rank roles (no Discord permissions) |
| `/createroyalty`, `/resetdata`, `/diagnostics` | Owner / Fire Lord | Role, wipe-with-backup, system check |
| `/voice join` / `leave` / `say` / `status` / `greeting …` | Owner, Fire Lord, access list | Spoken voice lines |

Ranks: Owner and Fire Lord come from the user IDs below; Royalty, Advisor and HR are Discord roles with exactly those names. Merits live in Postgres (`DATABASE_URL`), in the same `merit_awards` table Jarvis uses, so pointing Bob at Jarvis's database carries the existing ledger over. Every award, removal and reset is posted to the owner log channel. Once a day at midnight UTC, anyone who is no longer in the home server (`MERIT_HOME_GUILD_ID`, Fire Nation Military by default) is taken off the leaderboard; their records move to the `merit_awards_archive` table rather than being deleted.

The same merit actions work in conversation ("bob, give narek 2 bonus merits"), with the same rank checks. The Owner and Fire Lord can also give Bob standing orders ("from now on keep replies short"), which are saved and applied to every later conversation.

`fire_nation/fire-nation-knowledge.txt` is the knowledge base; sections whose `ALIASES` match a message are added to that reply's context. Entries added with `/addknowledge` are stored in the data directory.

In voice, Bob greets the Owner, Fire Lord and access-list members when they join, reads announcement-channel posts aloud, and leaves after 15 minutes alone. "bob, join vc", "leave vc", "say that out loud" and "voice status" work from chat. "bob, go to sleep" / "bob, wake up" take him offline and back.

```env
DISCORD_OWNER_USER_IDS=123456789012345678
DISCORD_SECOND_IN_COMMAND_USER_IDS=123456789012345678
DISCORD_OWNER_LOG_CHANNEL_ID=123456789012345678
DATABASE_URL=postgresql://user:password@host/dbname   # Neon, Supabase, Railway…
# Optional
DISCORD_HR_ROLE_IDS=                 # extra role IDs that count as HR
DISCORD_TEST_GUILD_ID=               # register slash commands to one server instantly
GOOGLE_API_KEY=                      # Gemini text-to-speech; without it Bob joins voice but can't speak
AI_PROVIDER=gemini                   # optional: chat with Gemini (what Jarvis used) on that same key
GEMINI_MODEL=                        # default: gemini-3.8-flash
GEMINI_FALLBACK_MODELS=              # comma-separated; used in order when a model's daily quota runs out
TTS_VOICE=Algenib                    # audition others with /diagnostics tts:True voice:<name>
TTS_MODEL=                           # default: the first flash TTS model the key can see
TTS_DELIVERY=                        # style direction placed before each spoken line
ANNOUNCEMENT_CHANNEL_IDS=            # ordinary text channels to read aloud like announcement channels
AVATAR_URLS=                         # image URLs to rotate the avatar through every 4 hours
BOT_DATA_DIR=                        # default: ./data
```

Runtime state lives in `data/`: `bot-access.txt` (one user ID per line — people Bob will talk to, besides the Owner and Fire Lord), `standing-orders.json`, `voice-greetings.json`, `knowledge-additions.txt` and cached voice clips. Optional artwork goes in `assets/`: `avatar-online.gif|png`, `avatar-offline.png` (shown while asleep or shut down) and `banner.gif|png`.
