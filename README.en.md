# Codex Helper

[中文](README.md) | **English**

Codex Helper is a local task monitor and scheduler for Codex. It watches running work, remembers the next prompt you meant to send, and resumes the right conversation when a task finishes or quota comes back.

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

Task state is scanned every five seconds. The Overview quota display refreshes every minute. When a quota-dependent rule is waiting, the background scheduler checks live quota about every ten seconds. The refresh button updates the current tab immediately.

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

Codex Helper reads local Codex session indexes and rollout logs. Rules are stored in `~/.codex-model-watch/state.db`. The dashboard listens on `127.0.0.1`, and prompts are executed through your existing local Codex CLI session.

This is a local helper, not a cloud queue. It cannot trigger work while the app or computer is asleep. Codex's internal rollout formats may change. The active probe uses a small request and only runs when you explicitly trigger it.

## License

MIT, with attribution to the original project. See [LICENSE](LICENSE).
