# Codex Helper

[中文](README.md) | **English**

Codex Helper is a local task monitor and scheduler for Codex and ZCode. It watches running work, remembers the next prompt you meant to send, and resumes the right conversation when a task finishes or quota comes back. The Overview also tracks ZCode usage (read-only import from `~/.zcode/cli/db/`) alongside Codex: models, tokens and errors per agent.

The Chinese tagline says it best: *the bicycle you are too precious to ride gets stood-on and pedaled by Codex Helper.*

## What it does

### Follow-up prompts

Choose **Project → Task** in the Schedule tab, enter the next prompt, and send it to the same Codex conversation after the current turn finishes. If the selected task has already ended, the follow-up runs immediately.

The picker includes every unarchived local work task, including older completed tasks. Subagents and archived tasks are left out so the list stays useful.

### Resume after quota limits

> All those beautiful tokens, donated to Sam Altman. What a crime.

Resume a task after a quota interruption, or enable automatic resume for quota interruptions observed after the switch is turned on. A completed task can also be selected for an immediate manual resume.

### Schedule new work

Start a new task at a chosen time, after the next quota reset, or after another running task finishes. Pick an existing local Codex project or create a new project directory when the rule actually runs.

The Schedule tab shows waiting, running and historical rules. Waiting rules can be cancelled.

## App layout

- **Overview**: usage, quota windows, models and local monitoring data;
- **Task monitoring and scheduling**: task status, automatic resume, Project → Task pickers and the queue;
- **Queue history**: completed, failed, cancelled and attention-needed rules.

Task state is scanned every five seconds. The Overview reads live quota from the Codex account API about every 30 seconds and refreshes the display every 30 seconds, showing both remaining and used percentages with a sample time. When a quota-dependent rule is waiting, the background scheduler checks live quota about every ten seconds. The refresh button updates the current tab immediately.

## Install on macOS Apple Silicon

Download the [latest ARM64 DMG](https://github.com/Allencheng97/codex-helper/releases/latest), move Codex Helper to Applications, and open it. The current build is not signed with an Apple Developer ID; use Finder's Open action the first time.

Build locally with:

```bash
npm install
npm run dist
```

The DMG is written to `dist/Codex Helper-*.dmg`.

## Where monitoring came from

The Overview tab, local session parsing, model usage, quota windows, capacity errors and active probe are based on [ysh1112/codex-model-watch](https://github.com/ysh1112/codex-model-watch). Thanks to ysh1112 for the original local Codex log parser, usage dashboard and model probe.

Codex Helper extends that foundation with an Electron menu-bar app, task monitoring, Project → Task selection, quota resume, follow-up prompts, one-time scheduling and a visible queue. The original monitoring features remain available; they now have a scheduler to keep the work moving.

## Privacy and boundaries

Codex Helper reads local Codex session indexes and rollout logs, and imports ZCode usage read-only from `~/.zcode/cli/db/db.sqlite`. Rules are stored in `~/.codex-model-watch/state.db`. The dashboard listens on `127.0.0.1`, and prompts are executed through your existing local Codex CLI session.

This is a local helper, not a cloud queue. It cannot trigger work while the app or computer is asleep. Codex's internal rollout formats may change. The active probe uses a small request and only runs when you explicitly trigger it.

ZCode support covers usage statistics plus experimental scheduling. The task picker lists recent ZCode sessions and accepts new-task (at time / after another task), follow-up and resume rules; ZCode resume waits for the live quota windows to recover (sampled every 30s; falls back to 5-minute retries, up to 8 attempts, when quota is unreadable), and new tasks support a next-quota-reset trigger.

The "玩命蹬" (sprint) tab is ZCode-exclusive, built for off-peak free quota: define a time window (a daily window like 00:00–08:00, crossing midnight supported, or a one-off window) and queue up to 50 tasks, each with its own editable prompt and ordering. Pick an existing directory or create a new project (parent path + folder name, created when the first task launches). When the window opens, tasks run at the configured concurrency — 1 means serial, N (max 6) means up to N in parallel. When the window ends or you hit stop, running tasks are terminated (SIGTERM, then SIGKILL after 5s) and the remaining queue is marked skipped; queues survive app restarts.

ZCode's headless CLI ships without a usable default model (model selection is guarded by the desktop app). Helper decrypts the local coding-plan API key from `~/.zcode/v2/credentials.json`, writes a standalone personal provider config to `~/.codex-model-watch/zcode-provider-config.json` (mode 0600), and injects it via `ZCODE_PERSONAL_PROVIDER_CONFIG_FILE` — ZCode's own files are never modified; rotated API keys trigger an automatic refresh and retry. Point `ZCODE_BIN` at a custom CLI to override discovery. The swap probe remains Codex-only; the quota panel works for both agents (ZCode usage comes from its account quota API). By default (`--agents auto`) the monitored agents are detected from the local data directories; override with `--agents codex,zcode` and `--zcode-home`.

## License

MIT, with attribution to the original project. See [LICENSE](LICENSE).
