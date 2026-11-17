# Pi Agent for Android (v0.1.1)

pi (pi.dev, `@earendil-works/pi-coding-agent`) on the phone in one app, no Termux needed: its own
application id (`com.jeep.piagent`), name and icon. No WebView: the interface is plain Android
views (Java), so scrolling and text selection are the system's own. The app is ClaudeCode-Android
with pi in place of Claude Code: the interface is the same, the bridge speaks pi's RPC mode.

## How it works

- The app carries its own Linux userland: Alpine (bash, Python, git, ripgrep, fd, Node.js, npm),
  packed as `assets/runtime/rootfs.tar.xz` by `runtime/build-rootfs.sh`, and Termux's proot
  (`runtime/fetch-proot.sh`). Both are built in CI (cached by the scripts' hash). pi is not in it:
  on first launch the app installs the latest from npm with the userland's `pi-update`
  (npm's registry, then npmmirror), which also updates it from the terminal (as does pi's own
  `pi update self`). On first launch, or after an update that brings a new userland, the app
  unpacks it into its files (over what is there: `/root` and packages added with `apk add` stay).
  targetSdk is 28 so the app may run programs from its own files, as Termux does.
- `BridgeService`, a foreground service holding a wake lock, runs `assets/bridge.py` under proot as
  root of the userland; it listens on a Unix socket, `~/.piapp/bridge.sock` in the userland (in this
  app's own files, mode 0600), not on a port: no other app can reach it, so requests carry no token.
  The app speaks HTTP to it over a `LocalSocket`. The service starts it again when it dies on its
  own (at most three times a minute). To stop or replace it, the service sends SIGTERM to the
  bridge itself (proot ignores SIGTERM): the bridge ends its conversations and exits.
- The app checks the bridge, has the service start it when it is gone or of another version, and
  gives it 30 s to answer. The event stream resumes where it left off with the same bridge; events
  dropped meanwhile (it keeps the last 4000) make the app read the conversations again from their
  session files. The bridge logs its starts, stops, requests, each pi process (start, retries, turn
  results, exit code and stderr) to `~/.piapp/bridge.log`; Settings → 关于 → 日志 shows it.
- Each conversation is a `pi --mode rpc` process (`--session-id <id>` for a new one, so its session
  id is known from the start; `--session <file>` for a saved one), kept alive between turns. A
  saved conversation's process starts only when a message is sent; a new conversation's is made
  ready ahead (the spare, with its own session id). pi's events are translated by the bridge into
  the stream-json messages the app's `Chat` reads (as Claude Code's were): `agent_start` is a turn's
  init, `message_update`'s text, thinking and tool-call deltas are content blocks,
  `tool_execution_end` a tool result, `auto_retry_start` an API retry, `compaction_end` a compaction
  boundary, `agent_settled` the result (stopped, failed with the model's error, or done). A prompt
  pi refuses (no API key, an unknown model) ends the turn with its error.
- A message written while a reply runs waits below it. With 插入 (the default; Settings → 对话 →
  回复中发消息) it is sent as soon as the reply runs a tool, as a `prompt` with
  `streamingBehavior: "steer"`: pi gives it to the model after the tools running. Until the model
  has it (pi's user message for it, told to the app as Claude Code's `command_lifecycle` started),
  it can be taken back: pi only clears its whole queue (`clear_queue`), so the others are given to
  it again in their order. Stop sends `abort`, and ends the process if the run has not settled 8 s
  later. A message sent while pi is busy with a run the app does not know of goes as a follow-up.
- Model and thinking level apply from the next message, changed in the running pi (`set_model`,
  `set_thinking_level`); a new pi gets `--model provider/id` and `--thinking`. The models listed
  are those pi can sign in to (`get_available_models` of a short pi of the bridge's own, with its
  extensions, so providers an extension adds are listed; asked again when pi, `auth.json`,
  `models.json`, `settings.json` or the extensions folder change).
- Settings → 模型服务 keeps a provider's API key in pi's `~/.pi/agent/auth.json` (the bridge's
  `/auth`, mode 0600); subscriptions sign in with pi's `/login` in the terminal; `models.json`
  and `settings.json` are edited whole. Settings → 对话 sets text for `--append-system-prompt` and
  extra arguments (kept in `~/.piapp/launch.json`; arguments saying which session runs or how pi
  talks, `-c`, `--session`, `--mode` and the like, or text beginning with no option, are refused,
  and ones saved earlier left out, the row saying why), and edits the global `~/.pi/agent/AGENTS.md`
  and the working folder's `AGENTS.md`; Settings → 扩展: skills (`~/.pi/agent/skills/<name>/SKILL.md`),
  prompt templates (`~/.pi/agent/prompts/<name>.md`) and tools left out (`--exclude-tools`).
- The conversation list and history come from pi's own session files in `~/.pi/agent/sessions`
  (the current branch of each: from its last entry back to its root). A title is the session's name
  (`/name`, or renaming in the list: pi's `session_info` record), else its first message. A saved
  conversation's pictures are read from its file when shown. `/tree` cuts the file before the user's
  message picked; 删除 takes a message (or a reply with its tool results) out and links what came
  after to what came before. `/compact [instructions]` and `/name <name>` are pi's `compact` and
  `set_session_name` (sent as prompts they would go to the model as they are).
- `Bridge` talks to the bridge (HTTP, event stream); `Chat` turns the stream-json messages (or a
  session's history) into messages and blocks; `ThreadAdapter` shows them in a RecyclerView; `Md`
  renders Markdown with Markwon; `DrawerFrame` is the drawer that a horizontal swipe anywhere opens.
  `MainActivity` keeps the conversations open and is made of parts (`Part`): `Link`, `ChatScreen`,
  `Box` (with `SlashCommands` and `Attachments`), `Sender`, `Finder`, `MessageActions`, `Drawer`,
  `Settings` with its pages (`LookPage`, `PiPage`, `ExtPage` with `Skills`, `AppPage`), `Pickers`,
  `Sheets`, `UsageSheet`, `Voice` (the phone's recognizer) and `Agent` (what is known of pi).

## Tests

- `ChatTest` applies scripts of stream-json messages (and a saved history) to a `Chat` and compares
  the conversation after each with `src/test/resources/chat/*.txt` (`-Drecord=true` records again).
- `src/test/python/test_bridge_http.py` starts the bridge's HTTP handler with a fake `pi` (pi's RPC
  mode and session files, as far as the bridge uses them) and compares every route's status and body,
  and the events of a few conversations, with `bridge_http.golden.json` (`--record` writes it again).
  Run it with `python3 -I`.
- `ScreensTest` and `ThemesTest` render the main screens with Robolectric's native graphics:
  `gradle :code:testDebugUnitTest --tests com.jeep.piagent.ScreensTest -Dshots=<dir>`, or the Screens
  workflow on GitHub.
