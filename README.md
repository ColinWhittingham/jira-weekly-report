# Jira Board Digest

Generates a self-contained HTML report summarising daily or weekly activity on a Jira Kanban board.
Metrics include throughput, cycle time, aging WIP, Monte Carlo forecasts, and per-assignee stats.

## Prerequisites

- Python 3.10 or later
- A Jira API token — create one at <https://id.atlassian.com/manage-profile/security/api-tokens>

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Create your config file from the template
copy config.example.json config.json   # Windows
# cp config.example.json config.json   # macOS / Linux
```

Edit `config.json` and fill in your Jira instance URL, board ID, and status names
(see [Configuration](#configuration) below).

```bash
# 3. Create your credentials file
copy .env.example .env   # Windows
# cp .env.example .env   # macOS / Linux
```

Edit `.env` and fill in your values:

```
JIRA_EMAIL=your.email@example.com
JIRA_API_TOKEN=your-api-token-here
```

## Running manually

```bash
# Yesterday's activity
python generate_report.py --period daily

# Previous Thu–Wed week (standard)
python generate_report.py --period weekly

# One-off week with a non-standard start date
python generate_report.py --period weekly --start-date 2026-07-21
```

The report is written to `reports/daily_YYYY-MM-DD.html` or `reports/weekly_YYYY-MM-DD.html`.
Open it in any browser — no server or internet connection required to view it (charts use CDN
links; a "no internet" warning will appear in the footer only if those CDNs are unreachable).

## Automating with Windows Task Scheduler

### Daily report (runs every weekday at 08:00)

1. Open **Task Scheduler** → **Create Task**
2. **General** tab → Name: `Jira Board Daily Report`
3. **Triggers** tab → New → Daily, Start: 08:00, Recur every 1 day
   - For Mon–Fri only: use a **Weekly** trigger and select Mon Tue Wed Thu Fri
4. **Actions** tab → New → Program: full path to `launch_daily.bat`
5. **Settings** tab → check "Run task as soon as possible after a scheduled start is missed"
6. Click OK

### Weekly report (runs every Thursday at 08:00)

Repeat the steps above with:
- Name: `Jira Board Weekly Report`
- Triggers: Weekly, every **Thursday** at 08:00
- Action: full path to `launch_weekly.bat`

Logs are written to `logs\daily.log` and `logs\weekly.log`.

## Troubleshooting

| Error | Fix |
|---|---|
| `config.json not found` | Copy `config.example.json` to `config.json` and edit it |
| `JIRA_EMAIL and JIRA_API_TOKEN must be set` | Copy `.env.example` to `.env` and fill in both values |
| `401 Unauthorized` | API token is wrong or expired — regenerate at id.atlassian.com |
| `404` on board issues | Board ID has changed — update `board_id` in `config.json` |
| Sankey chart is blank | Browser needs internet to load Google Charts CDN (`www.gstatic.com`) |
| No transitions shown | The period had no status changes on the board — this is a valid empty report |

## Configuration

All non-secret settings live in `config.json` (copy from `config.example.json` to get started):

| Key | Default | Purpose |
|---|---|---|
| `jira.base_url` | — | Jira instance URL, e.g. `https://your-org.atlassian.net` |
| `jira.board_id` | — | Numeric ID of the Kanban board |
| `jira.board_url` | — | Full board URL (used as link in the report header) |
| `jira.done_statuses` | `["Done"]` | Status names counted as "completed" |
| `jira.active_status` | `"In Progress"` | Status name for active WIP |
| `jira.non_wip_statuses` | `[]` | Statuses excluded from WIP calculations |
| `jira.support_case_type` | `"Support Case"` | Issue type label for support work |
| `jira.exclude_types` | `["Sub-task"]` | Issue types excluded entirely |
| `jira.max_wip_age_days` | `180` | Upper bound (days) for aging WIP chart |
| `jira.week_start_day` | `3` | First day of the reporting week (0 = Mon … 6 = Sun); `3` = Thursday for Thu–Wed weeks |
| `output.directory` | `reports` | Where HTML files are written |
| `output.filename_pattern` | `{period}_{date}.html` | Output filename format |
