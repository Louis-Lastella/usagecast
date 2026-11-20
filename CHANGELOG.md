# Changelog

Versions follow [Semantic Versioning](https://semver.org).

## 3.9.3 - 2026-11-20

- Several accounts: an account Hermes was told to prefer (pool_preference.json, e.g. from a timer that switches
  accounts) now counts as the next and shown one, so the account really in use is on top again. After a switch the
  old account drops its "In use" badge at the next reading instead of keeping it for 30 minutes.

## 3.9.2 - 2026-11-18

- Telegram menu button: the use comes first ("W 47% · 5h 32% · claude-second"), so a narrow phone button cuts the
  account name instead of the numbers.

## 3.9.1 - 2026-11-18

- Telegram menu button: the account name is no longer cut hard at 12 characters ("claude-secon"); names up to 16
  characters stay whole, longer ones end in "…".

## 3.9.0 - 2026-11-17

- Several accounts: the limits card opens with all of them as one ("All accounts"): the mean use of the week and the
  5-hour window (100 % = all full), the next reset, a week forecast that averages each account's own forecast and a
  daily budget that counts each account up to its own reset. The mascot follows the pooled 5-hour window. "2 accounts"
  folds out today's per-account view with the trend; the device remembers whether it was open. One account: unchanged.

## 3.8.2 - 2026-11-17

- The mascot's mood now comes from its window alone: calm below 50 %, busy from 50 %, tight from 85 %, asleep at the
  limit. Before, it looked busy whenever an agent was running, even with the 5-hour window at 0 %.

## 3.8.1 - 2026-11-17

- The mascot follows one window: the 5-hour one on plans that have it, else the week. Its mood, the terminal's warning,
  the coffee level and the bot battery all come from that window, so the number it shows matches its mood. A full
  window or an unavailable account still puts it to sleep.

## 3.8.0 - 2026-11-17

- Mascots: an animated companion sits beside the big weekly number (terminal, rubber duck, server rack, coffee or bot). It
  shows four moods from the account in use: calm, an agent is working, tight (a window at 85 % or a tight week forecast)
  and at the limit. Coffee level and bot battery follow what is left of the week. It switches between calm and working
  without a reload, follows light and dark mode, and stops moving with reduced motion. Pick one or turn it off in
  Settings > Appearance.
- Settings rebuilt like iOS Settings: a short list (Appearance, Alerts, Notifications, Budget guard, Advanced) that shows
  the current state of each, one sub-page per section. Every change saves at once with a short "Saved" note; an invalid
  value is pointed out at its field and keeps the old one. Without JavaScript the save button stays.
- Each sub-page only saves its own settings, so switches on other pages are never turned off by a save.
- Push and Telegram share one Notifications page; the channel is a checkmark list that fits a phone screen.
  Explanations are one line each; technical details moved to Advanced and the README.

## 3.7.0 - 2026-11-17

- Models: every session and subagent now names the model it runs on ("Opus 5.5", "Haiku 4.5"), in Right now, in the
  session list, in the session header and in a session's subagent table. A session that switched models lists all of
  them, the one that cost most first.

## 3.6.0 - 2026-11-17

- Several accounts: when Hermes' credential pool holds more than one Claude account, Usagecast reads the limits of each
  one with its own login and tells which is in use (its usage or request count rose in the last 30 minutes) and which
  Hermes picks next (fill_first, round_robin, least_used; random has no next pick). Before, the limit series silently
  jumped to whichever account came first, so forecast, alerts and history mixed accounts.
- Limits card: one block per account with a badge (in use, next, limit until ...); the active account keeps the full card.
- Alerts run per account and name it; the budget guard pauses cron jobs only when every available account is tight and
  resumes them as soon as one is free.
- Telegram menu button and pin, `/api/summary` (`accounts`) and the widget name the active account. `--demo` has two.
- One account: everything looks and behaves as before.

## 3.5.1 - 2026-11-11

- Tips: "Turn off unused tools" now counts every tool the agent ran in the last 30 days, whatever the period shown.
  On a 24 h view it used to suggest turning off tools that are used every week (clarify, memory, browser).
- Faster pages: the limit history file is parsed once and then only its new lines are read; the overview used to
  parse the whole file up to four times per load, and the file keeps growing.
- Settings on phones: the source picker no longer sticks out on the right (the page could shift sideways), and no
  id is used twice on the page (the budget guard and source cards had the same id as their controls).

## 3.5.0 - 2026-11-09

- Bots & chats: one overview card replaces "Where it happens" and "By agent". One row per bot (an agent on one
  platform, named by the bot itself, e.g. "Atlas · Telegram"), plus cron jobs and subagents per agent. Tapping a row
  folds out its chats: Telegram topics, DMs and groups, Discord DMs, channels and threads, Slack threads and the like;
  cron folds out its jobs, subagents the chats that started them. Every chat leads to its sessions.
- A topic without a stored name takes it from another session of the same topic, else it shows as "Topic 13897".
  Bot names come from Telegram `getMe`, Discord `users/@me` or Slack `auth.test` once a day (`data/bots.json`); without
  a token the agent's name is used. Older Hermes databases without the chat columns still work.
- Sessions: chips per bot (the cron chip still opens the job table, old `src=` links still filter), a chat filter with
  a clear link, and "bot · chat" under every title, in the session header and in "Right now".

## 3.4.0 - 2026-11-06

- Long context is learned per agent: a step counts as long when it reads more history than about 3 of 4 new chats need
  for their first task (last 30 days, cron jobs and subagents left out), kept between 100,000 and 200,000 tokens. While
  the weekly limit is at 80 % or heading for a tight week the line stays at 100,000. Details names the line in force.
- Tips no longer show twice with several agents: tips that read only the analysis (start fresh, cache breaks, one tool)
  run once on the whole, and the same config tip of several agents becomes one card with a line and command per agent.

## 3.3.0 - 2026-10-23

- Subagents are measured per chat: a session page lists the subagents it started (their own subagents included) with
  steps, real cost and share, adds them as a "Subagents" part to what ate the session and shows the cost with them in
  the header. A subagent's page and its row in the session list name the session that started it, with a link; the
  list shows how many subagents a session started and what they cost in the period.

## 3.2.2 - 2026-10-19

- Telegram: back to one pinned status in the chat itself. The per-topic pins of 3.2.1 are gone; they were not wanted
  and do not bring the menu button into topics.

## 3.2.1 - 2026-10-19

- Telegram: the pinned status with its "Open" button now sits in every topic of the chat, not only in the chat itself.
  Telegram shows the menu button only outside topics. Topics come from the home topic, `dm_topics` in config.yaml and
  the sessions; a deleted topic is skipped from then on.

## 3.2.0 - 2026-10-19

Every agent counted, who is working right now, and Usagecast as a Telegram Mini App.

- All Hermes profiles by default: each home is analysed with its own logs, prompt snapshot and config, then added up
  (setting "All agents"; one home can still be chosen). A "By agent" card on the overview, the agent under each session
  title, `hermes -p <profile> --resume` on session pages, tips with the profile's command, cron jobs and the budget
  guard across profiles.
- "Right now" card on the overview: the agents at work with their current step, how long the turn has run and what the
  session cost so far, subagents under their parent, a warning when the gateway or a platform is down, the next cron
  runs. `/live` serves the card and it refreshes every 5 s while in view; `/api/summary` gains `agents`.
- Telegram Mini App: Telegram's haptics and back button, header colours that match the page, no swipe-to-close while
  scrolling, full screen on phones, "Add to home screen" in the settings (`static/app.js`).
- Telegram through a Hermes bot (settings card "Telegram"): alerts as messages with an "Open" button into the Mini App,
  the limits on the bot's menu button, and one pinned status message edited in place every 10 minutes.

## 3.1.0 - 2026-10-11

UX pass from a review of every page on desktop and phone.

- Pace verdict has three states: "Tight" (orange) from an 85 % forecast for the week and the 5-hour window, "Too fast"
  above 100 %. The forecast was 16-47 % off in backtests, so 99 % no longer reads "On track".
- Overview trend shows only the current limit week, with a 100 % line and the forecast dashed up to the reset, instead
  of seven days of 5-hour teeth without a scale.
- Sessions: one filter row (the cron chip opens the per-job table), origin and last activity under every title, so
  sessions with the same name can be told apart on a phone too.
- Phones: settings moved to an icon in the header, the tab bar keeps five tabs.
- Method texts ("What eats the most", projects, logged cache numbers) fold into "How it's calculated".
- Savings tips: one comparable "saves ~X %" per tip, sorted by it; titles say what to do; the cache tip shows only the
  command instead of a broken YAML snippet.
- Settings: alerts in three groups (limits, costs, summary and quiet hours).
- The year calendar starts at the first week with data. The models card hides with only one model, empty skill and
  plugin tables on Details hide too.
- Fixed "31 active days out of 30"; the average is labelled per active day.

## 3.0.2 - 2026-10-09

- Tips: "Cron: ... is expensive" only for jobs that will run again. Finished one-shot jobs (a build that ran once)
  no longer show up there, they can't be run less often.

## 3.0.1 - 2026-10-09

- Design pass over the parts added in 3.0.0. Settings: the install command for other machines is one readable block,
  reporting machines are rows like the cron jobs, every fold uses the same chevron, card titles get room above their
  first row, larger switches on phones.
- Phones: the custom range sits in the same row as the period tabs (very narrow screens keep the second row).
- History: the three earlier weeks are readable next to this week.

## 3.0.0 - 2026-10-09

- Alerts when a chat has become expensive (its last three replies above a share of the week, with what a new chat
  costs) and when a cron run or a single reply costs far more than usual.
- Extra credits per month: every limits reading stores them, earlier months fold out under the limits, an estimate of
  what the week would cost in credits when the forecast is above 100 %, alerts at chosen credit steps.
- Budget guard: pauses the cron jobs you mark while the week gets tight and resumes them afterwards.
- Week forecast by weekly rhythm, used when a backtest over the last weeks finds it more accurate than a straight line.
- Lock Screen widgets: circular, rectangular and inline.
- Claude Code from other machines: `POST /api/ingest` and `tools/cc-report.py` (Mac LaunchAgent or Linux crontab).
- MIT license, CI on GitHub Actions, the version in the page footer.

## 2.0.0 - 2026-10-09

- Renamed to Usagecast, English first with a German locale.
- Settings page: push setup with ntfy, euro amounts, theme, costs as money or % of the week, the analysed Hermes
  profile.
- Limits: pace verdicts, daily budget, who used the weekly limit (Hermes, Claude Code, other), status page incidents.
- Alerts: 5-hour window free again, weekly limit at 80 and 90 %, Sunday digest.
- History: activity calendar, custom date range, this week over the three before, Hermes updates and config changes
  marked with a before/after cost per step.
- Details: a likely cause per cache break, the most expensive single tool results.
- `/api/summary` and a Scriptable widget for the iOS home screen.
- New look (stone neutrals, one sans, foldable explanations, bottom tab bar on phones), haptics, `--demo` mode.

## 1.0.0 - 2026-10-08

- First version: a dashboard of what eats the Claude usage in Hermes, with sessions, projects, the subscription limits
  with a forecast and ntfy alerts.
