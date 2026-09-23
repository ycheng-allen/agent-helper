# Agent Helper

[中文](README.md) | **English**

> The bicycle you're too precious to ride gets stood on and pedaled — Agent Helper pedals it for you!

Agent Helper is a local **task monitor and scheduler for both Codex and ZCode**. It watches running work, remembers the next prompt you meant to send, and delivers it to the right conversation when quota returns, a task finishes, or a scheduled time arrives. The Overview tracks both agents' model usage, tokens, errors and cost side by side.

In short: you set the ambitious goal, Codex writes the code, and Agent Helper picks the work back up right when everyone else has forgotten about it.

## Features at a glance

| Feature | In one line |
|---|---|
| 📊 Dual-agent monitoring | Sessions, turns, tokens, duration and errors for Codex & ZCode, read from local logs |
| ⏱ Task scheduling | Trigger at a time / after quota reset / after another task — new task, follow-up, or resume |
| 🔁 Resume after quota | A quota-interrupted task waits for its window to recover, then continues in place |
| 🚴 Sprint queue | ZCode off-peak queue: time window + up to 50 tasks + 1–6 concurrency |
| 📶 Live quota | Dual 5-hour / weekly windows for Codex & ZCode, sampled every 30s |
| 💰 Monthly cost | Month-to-date spend at public list prices (CN & US markets), tunable rates |
| 🕵️ Model-swap probe | One tiny request to verify the requested model is the served model |
| 🌐 Bilingual UI | EN / CH toggle; the language also picks the cost currency |

## Layout: three spaces

| Space | Contents |
|---|---|
| **Overview** | Cross-agent stats: month cost (total + per agent), per-model cost table, daily token/cost trends, live quota |
| **Codex** | Codex-only task list, scheduling, probe, quota and cost |
| **ZCode** | ZCode-only task list, scheduling, sprint queue, quota and cost |

Local task state is scanned every 5 seconds; the Overview refreshes every 30 seconds, or immediately via the refresh button.

## Task scheduling

Three rule types, shared by Codex and ZCode:

| Rule | Trigger | Typical use |
|---|---|---|
| **New task** | At a time / after next quota reset / after another task | Start on schedule, off-peak starts, pipelines |
| **Follow-up** | Append a prompt to the same conversation after the current turn | The next step without relying on memory |
| **Resume** | Wait for the live quota window to recover, then continue | Work interrupted by rate limits picks itself back up |

Scheduling behavior:

- Tasks are picked **Project → Task**, covering every unarchived local task (older finished ones included); archived tasks and subagents are excluded;
- If the target task has already ended, the rule runs on save; otherwise it waits for the current turn;
- The queue shows waiting / running / history; waiting rules can be cancelled;
- ZCode resume gates on live quota windows (sampled every 30s; falls back to 5-minute retries, up to 8 attempts, when quota is unreadable);
- Nothing runs while the app or the computer is asleep; after a restart, unfinished rules are flagged for review.

## Sprint queue (ZCode off-peak)

> All those beautiful tokens, donated to Sam Altman — free off-peak quota shouldn't go to waste.

- Define a time window: a daily window (e.g. 00:00–08:00, crossing midnight supported) or a one-off window;
- Queue up to **50 tasks**, each with its own prompt, individually editable and drag-reorderable;
- Target an existing directory or a new project (created when the first task launches);
- Concurrency 1–6: 1 is serial, N means up to N tasks at once;
- Window end or manual stop: running tasks get SIGTERM then SIGKILL after 5s; the rest are skipped;
- Queues survive app restarts; manual start, stop-all and per-task delete included.

## Live quota

- **Codex**: live quota from the account API, sampled about every 30s; ~10s while a quota-gated rule is waiting;
- **ZCode**: quota windows (5-hour + weekly) from the account usage API, with remaining/used percentages and reset times;
- Quota panels appear on the Overview and in each agent space.

## Usage stats and monthly cost

- Codex: parsed from local session indexes and rollout logs — models, turns, tokens, duration, errors;
- ZCode: read-only incremental import from `~/.zcode/cli/db/db.sqlite`;
- Overview: month-to-date cost (total + per agent), per-model cost table, daily token/cost trends (last 30 days), cache-hit stats;
- Costs are computed at public API list prices covering OpenAI and Zhipu's CN/US markets; override rates via `~/.agent-helper/pricing.json`.

## ZCode headless execution (automatic unlock)

ZCode's headless CLI ships without a usable default model (model selection is guarded by the desktop app). Agent Helper:

1. decrypts the coding-plan API key from `~/.zcode/v2/credentials.json`;
2. writes a standalone personal provider config to `~/.agent-helper/zcode-provider-config.json` (mode 0600);
3. injects it via the `ZCODE_PERSONAL_PROVIDER_CONFIG_FILE` env var — ZCode's own files are never modified;
4. auto-refreshes the config and retries when an API key rotation breaks execution.

## Install

### macOS Apple Silicon

Download the [latest ARM64 DMG](https://github.com/ycheng-allen/agent-helper/releases/latest), move Agent Helper to Applications and launch. The build is not signed with an Apple Developer ID; use Finder's right-click → Open the first time.

### Ubuntu / Linux x64

Download the [latest AppImage or deb](https://github.com/ycheng-allen/agent-helper/releases/latest):

```bash
# AppImage
chmod +x "Agent Helper-*.AppImage" && ./"Agent Helper-*.AppImage"

# or deb (Ubuntu 22.04+, libappindicator3-1 needed for the tray icon)
sudo dpkg -i agent-helper_*_amd64.deb
```

Requires system `python3` (preinstalled on Ubuntu) and the [ZCode desktop app for Linux](https://zcode.z.ai/cn/docs/install); ZCode installed via deb/rpm is auto-detected at `/opt/ZCode`, for the AppImage build point `ZCODE_BIN` at its bundled `zcode.cjs`. The tray works on stock Ubuntu GNOME (with the AppIndicator extension); clicking the tray icon opens a menu.

### Build from source

```bash
git clone https://github.com/ycheng-allen/agent-helper.git
cd agent-helper
npm install
npm run dist          # macOS: dist/Agent Helper-*.dmg
npm run dist:linux    # Linux: dist/*.AppImage and dist/*.deb
```

### Run from source directly

Requires Python 3.8+ and Node.js:

```bash
npm install
npm start
```

In development you can also run the local service directly:

```bash
python3 agent_helper.py --no-open
```

Everything listens on `127.0.0.1` only. `ZCODE_BIN` overrides ZCode CLI discovery; `--agents codex,zcode` and `--zcode-home` pin the monitoring scope.

## Data and privacy

| Path | Contents |
|---|---|
| `~/.agent-helper/state.db` | Schedule rules, history and saved projects |
| `~/.agent-helper/pricing.json` | Optional price / FX-rate overrides |
| `~/.agent-helper/zcode-provider-config.json` | Provider config for ZCode headless runs (0600) |
| `~/.zcode/cli/db/db.sqlite` | ZCode usage database (read-only) |
| `~/.codex/sessions/` etc. | Codex session logs (read-only) |

The data directory migrated automatically from the old `~/.codex-model-watch/`. Projects, prompts and task titles never leave your machine; there is no cloud queue — prompts are executed through your existing local CLI logins.

## Known limits

- A local helper, not a cloud queue: nothing triggers while the app or computer is asleep;
- **ZCode desktop display**: turns submitted by any headless scheduler persist to ZCode's database, but the desktop renders an open conversation from its in-memory runtime — the new turns appear once that session is reloaded (e.g. after restarting ZCode); execution and history are unaffected;
- The model-swap probe is Codex-only;
- Codex rollout/state formats are internal and may change between versions;
- The probe only sends when you click it; past swaps can't be recovered from history logs.

## Acknowledgements

The Overview, usage parsing, quota windows, capacity errors and the active probe are based on [ysh1112/codex-model-watch](https://github.com/ysh1112/codex-model-watch). Thanks to ysh1112 for the original local Codex log parser, usage dashboard and model probe.

## License

MIT, with attribution to the original project. See [LICENSE](LICENSE).
