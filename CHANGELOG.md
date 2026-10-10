# Changelog

Versions follow [Semantic Versioning](https://semver.org).

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
