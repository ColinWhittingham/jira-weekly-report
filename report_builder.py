from __future__ import annotations

import random
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from jira_client import JiraClient, parse_jira_dt


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class StatusTransition:
    issue_key: str
    issue_summary: str
    issue_type: str
    assignee_name: str
    from_status: str
    to_status: str
    timestamp: datetime
    is_backward: bool


@dataclass
class ReportData:
    period: str
    period_label: str
    start_date: date
    end_date: date
    generated_at: datetime
    board_id: int
    base_url: str
    board_url: str

    transitions: list[StatusTransition]

    # Transition aggregation
    transition_counts: dict[str, int]
    transition_issues: dict[str, list[dict]]
    backward_pairs: set[str]

    # Throughput (current period)
    throughput_labels: list[str]
    throughput_current: list[int]
    throughput_prior: list[int]
    throughput_by_type: dict[str, list[int]]

    # 6-week historical trend
    weekly_trend: dict | None

    # Cycle time (all non-excluded ticket types)
    cycle_times: list[dict]
    ct_p50: float | None
    ct_p85: float | None
    ct_p95: float | None

    # Cycle time split: Support Cases vs everything else
    support_case_type: str
    ct_p50_sc: float | None
    ct_p85_sc: float | None
    ct_p95_sc: float | None
    ct_count_sc: int
    ct_p50_other: float | None
    ct_p85_other: float | None
    ct_p95_other: float | None
    ct_count_other: int

    # Stage cycle time — overall, Support Cases, and Other
    stage_ct: list[dict]
    stage_ct_sc: list[dict]
    stage_ct_other: list[dict]

    # Aging WIP
    aging_wip: list[dict]
    aging_stage_buckets: dict
    aging_top_at_risk: list[dict]
    stale_wip_count: int
    stale_wip_issues: list[dict]
    stale_threshold_days: int

    # WIP composition by type
    wip_by_type: dict[str, int]

    # Per-assignee breakdown
    assignee_stats: list[dict]

    # Tickets entering the workflow for the first time this period
    tickets_added: int
    tickets_added_issues: list[dict]

    # Monte Carlo 7-day forecast
    mc: dict | None

    # Summary stats
    total_completed: int
    prior_total_completed: int
    total_in_progress: int
    backward_transition_count: int


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _local_offset() -> timezone:
    import time as _t
    return timezone(timedelta(seconds=-_t.altzone if _t.daylight else -_t.timezone))


def _type_excl_jql(exclude_types: list[str] | None) -> str:
    """Return a JQL clause that excludes the given issue types, or empty string."""
    if not exclude_types:
        return ""
    types = ",".join(f'"{t}"' for t in exclude_types)
    return f' AND issuetype not in ({types})'


def date_window(period: str, week_start_day: int = 0) -> tuple[date, date]:
    """Return (start, end) for the reporting period.

    For 'weekly': returns the most recently completed week whose first day is
    week_start_day (0=Mon, 1=Tue, …, 6=Sun).  This means the report always
    covers a clean calendar week regardless of which day it is run on.
    """
    today = date.today()
    if period == "daily":
        y = today - timedelta(days=1)
        return y, y
    # Days elapsed since the current week started
    days_since_start = (today.weekday() - week_start_day) % 7
    this_week_start = today - timedelta(days=days_since_start)
    week_end = this_week_start - timedelta(days=1)   # last day of previous week
    week_start = week_end - timedelta(days=6)         # first day of previous week
    return week_start, week_end


def _window_datetimes(start: date, end: date) -> tuple[datetime, datetime]:
    tz = _local_offset()
    return (
        datetime(start.year, start.month, start.day, 0, 0, 0, tzinfo=tz),
        datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=tz),
    )


def _period_label(period: str, start: date, end: date) -> str:
    def fmt(d: date, yr: bool = False) -> str:
        try:
            s = d.strftime("%-d %b")
        except ValueError:
            s = d.strftime("%#d %b")
        return s + (d.strftime(" %Y") if yr else "")
    return fmt(start, yr=True) if period == "daily" else f"{fmt(start)} – {fmt(end, yr=True)}"


def _is_backward(from_s: str, to_s: str, col_order: dict[str, int]) -> bool:
    fi, ti = col_order.get(from_s, -1), col_order.get(to_s, -1)
    return fi >= 0 and ti >= 0 and fi > ti


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    sv = sorted(values)
    n = len(sv)
    rank = p / 100 * (n - 1)
    lo = int(rank)
    hi = min(lo + 1, n - 1)
    return sv[lo] + (rank - lo) * (sv[hi] - sv[lo])


def _fmt_label(d: date) -> str:
    try:
        return d.strftime("%-d %b")
    except ValueError:
        return d.strftime("%#d %b")


# ---------------------------------------------------------------------------
# Transition + cycle-time fetch
# ---------------------------------------------------------------------------

def _fetch_transitions_in_window(
    client: JiraClient,
    board_id: int,
    start_dt: datetime,
    end_dt: datetime,
    done_statuses: list[str],
    active_status: str,
    col_order: dict[str, int],
    exclude_types: list[str] | None = None,
) -> tuple[list[StatusTransition], list[dict], list[dict]]:
    """Return (window_transitions, added_issues, cycle_time_records)."""
    since_str = start_dt.date().isoformat()
    excl = _type_excl_jql(exclude_types)
    issues = list(client.get_board_issues(
        board_id,
        jql=f'updated >= "{since_str}"{excl}',
        fields="summary,assignee,issuetype",
    ))

    transitions: list[StatusTransition] = []
    added_issues: list[dict] = []
    cycle_time_records: list[dict] = []

    def _fetch(issue: dict) -> tuple[list, dict | None, dict | None]:
        key = issue["key"]
        f = issue.get("fields") or {}
        summary = f.get("summary", "")
        issue_type = (f.get("issuetype") or {}).get("name", "")
        assignee = ((f.get("assignee") or {}).get("displayName") or "Unassigned")

        raw = client.get_issue_changelog(key)
        changes: list[tuple[datetime, str, str]] = []
        for e in raw:
            try:
                ts = parse_jira_dt(e["created"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                changes.append((ts, e["fromString"], e["toString"]))
            except Exception:
                pass
        changes.sort(key=lambda x: x[0])

        window = [
            StatusTransition(
                issue_key=key, issue_summary=summary, issue_type=issue_type,
                assignee_name=assignee, from_status=fs, to_status=ts_,
                timestamp=ts, is_backward=_is_backward(fs, ts_, col_order),
            )
            for ts, fs, ts_ in changes if start_dt <= ts <= end_dt
        ]

        first_active_ever = next(
            (ts for ts, _, to_ in changes if to_ == active_status), None
        )
        new_issue = None
        if first_active_ever and start_dt <= first_active_ever <= end_dt:
            new_issue = {"key": key, "summary": summary,
                         "issue_type": issue_type, "assignee_name": assignee}

        completion_ts = next(
            (ts for ts, _, to_ in changes
             if start_dt <= ts <= end_dt and to_ in done_statuses),
            None,
        )
        ct_record = None
        if completion_ts and first_active_ever and completion_ts >= first_active_ever:
            ct_days = (completion_ts - first_active_ever).total_seconds() / 86400
            stage_times: dict[str, float] = {}
            for i, (ts, _, to_) in enumerate(changes):
                if ts < first_active_ever or ts > completion_ts:
                    continue
                nxt = changes[i + 1][0] if i + 1 < len(changes) else completion_ts
                nxt = min(nxt, completion_ts)
                dur = (nxt - ts).total_seconds() / 86400
                if dur > 0:
                    stage_times[to_] = stage_times.get(to_, 0) + dur
            ct_record = {
                "issue_key": key,
                "issue_summary": summary,
                "issue_type": issue_type,
                "cycle_time_days": round(ct_days, 1),
                "completion_ts_ms": int(completion_ts.timestamp() * 1000),
                "stage_times": stage_times,
            }

        return window, new_issue, ct_record

    with ThreadPoolExecutor(max_workers=8) as pool:
        for fut in as_completed({pool.submit(_fetch, iss): iss for iss in issues}):
            try:
                w, ni, ct = fut.result()
                transitions.extend(w)
                if ni:
                    added_issues.append(ni)
                if ct:
                    cycle_time_records.append(ct)
            except Exception:
                pass

    return transitions, added_issues, cycle_time_records


# ---------------------------------------------------------------------------
# Aging WIP
# ---------------------------------------------------------------------------

def _fetch_aging_wip(
    client: JiraClient,
    board_id: int,
    done_statuses: list[str],
    active_status: str,
    now_dt: datetime,
    ct_p50: float | None,
    ct_p85: float | None,
    max_wip_age_days: int = 180,
    exclude_types: list[str] | None = None,
    non_wip_statuses: list[str] | None = None,
) -> tuple[list[dict], dict, list[dict], dict[str, int], int, list[dict]]:
    """Return (aging_wip, stage_buckets, at_risk, wip_by_type, stale_count, stale_top).

    Uses `statusCategory != "To Do"` rather than `statusCategory = "In Progress"`
    so that statuses Jira places in the "Done" category but which the team uses as
    intermediate workflow gates (e.g. "Resolved" = PR merged, awaiting QA) are
    correctly counted as active WIP.

    Explicitly excluded: done_statuses (Closed, Abandoned — truly finished work)
    and non_wip_statuses (Classified — pre-flow despite Jira's In Progress category).
    Pre-flow To Do stages (Draft, Accepted, Backlog, Triage, etc.) are excluded by
    the statusCategory != "To Do" clause without enumerating them.
    """
    type_excl = _type_excl_jql(exclude_types)
    all_excl = list(done_statuses) + list(non_wip_statuses or [])
    excl_str = ",".join(f'"{s}"' for s in all_excl)
    wip_jql = f'statusCategory != "To Do" AND status not in ({excl_str}){type_excl}'

    wip_issues = list(client.get_board_issues(
        board_id,
        jql=wip_jql,
        fields="status,issuetype,summary",
    ))

    wip_by_type: dict[str, int] = {}
    for iss in wip_issues:
        t = ((iss.get("fields") or {}).get("issuetype") or {}).get("name", "Other")
        wip_by_type[t] = wip_by_type.get(t, 0) + 1

    all_aging: list[dict] = []

    def _fetch_age(issue: dict) -> dict | None:
        key = issue["key"]
        f = issue.get("fields") or {}
        summary = f.get("summary", "")
        issue_type = (f.get("issuetype") or {}).get("name", "")
        current_status = (f.get("status") or {}).get("name", "")

        raw = client.get_issue_changelog(key)
        changes: list[tuple[datetime, str]] = []
        for e in raw:
            try:
                ts = parse_jira_dt(e["created"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                changes.append((ts, e["toString"]))
            except Exception:
                pass
        changes.sort(key=lambda x: x[0])

        first_active = next((ts for ts, to_ in changes if to_ == active_status), None)
        if not first_active:
            return None

        age_days = (now_dt - first_active.astimezone(now_dt.tzinfo)).total_seconds() / 86400
        return {
            "key": key, "summary": summary, "issue_type": issue_type,
            "status": current_status, "age_days": round(age_days, 1),
        }

    with ThreadPoolExecutor(max_workers=8) as pool:
        for fut in as_completed({pool.submit(_fetch_age, iss): iss for iss in wip_issues}):
            try:
                r = fut.result()
                if r:
                    all_aging.append(r)
            except Exception:
                pass

    all_aging.sort(key=lambda x: x["age_days"], reverse=True)

    stale = [x for x in all_aging if x["age_days"] > max_wip_age_days]
    active_aging = [x for x in all_aging if x["age_days"] <= max_wip_age_days]

    stage_counts: dict[str, dict] = {}
    for item in active_aging:
        st = item["status"]
        if st not in stage_counts:
            stage_counts[st] = {"green": 0, "amber": 0, "red": 0}
        age = item["age_days"]
        if ct_p85 is not None and age >= ct_p85:
            stage_counts[st]["red"] += 1
        elif ct_p50 is not None and age >= ct_p50:
            stage_counts[st]["amber"] += 1
        else:
            stage_counts[st]["green"] += 1

    stages = list(stage_counts.keys())
    aging_stage_buckets = {
        "stages": stages,
        "green": [stage_counts[s]["green"] for s in stages],
        "amber": [stage_counts[s]["amber"] for s in stages],
        "red":   [stage_counts[s]["red"]   for s in stages],
    }

    threshold = ct_p85 or 0
    at_risk = [x for x in active_aging if x["age_days"] >= threshold][:10]

    return active_aging, aging_stage_buckets, at_risk, wip_by_type, len(stale), stale[:5]


# ---------------------------------------------------------------------------
# Throughput helpers
# ---------------------------------------------------------------------------

def _throughput_series(
    transitions: list[StatusTransition],
    done_statuses: list[str],
    start: date,
    end: date,
) -> tuple[list[str], list[int], dict[str, list[int]]]:
    days: list[date] = []
    cur = start
    while cur <= end:
        days.append(cur)
        cur += timedelta(days=1)

    totals: dict[date, int] = {d: 0 for d in days}
    by_type: dict[str, dict[date, int]] = defaultdict(lambda: {d: 0 for d in days})
    tz = _local_offset()

    for t in transitions:
        if t.to_status in done_statuses:
            d = t.timestamp.astimezone(tz).date()
            if d in totals:
                totals[d] += 1
                by_type[t.issue_type][d] += 1

    labels = [_fmt_label(d) for d in days]
    total_list = [totals[d] for d in days]
    type_series = {tp: [by_type[tp][d] for d in days] for tp in by_type}
    return labels, total_list, type_series


# ---------------------------------------------------------------------------
# 6-week trend
# ---------------------------------------------------------------------------

def _resolve_date(fields: dict, tz: timezone) -> date | None:
    for key in ("resolutiondate", "updated"):
        val = fields.get(key, "")
        if val:
            try:
                return parse_jira_dt(val).astimezone(tz).date()
            except Exception:
                pass
    return None


def _compute_weekly_trend(
    resolved_issues: list[dict],
    created_issues: list[dict],
    tz: timezone,
    week_start_day: int = 0,
    exact_current: tuple[int, int] | None = None,
    exact_prior: tuple[int, int] | None = None,
) -> dict | None:
    """Return {labels, completed, added, exact_weeks} for the 6 most recent complete weeks.

    Weeks are aligned to week_start_day (0=Mon … 6=Sun).

    The field-based approach (resolutiondate/updated for completed, created for
    added) is fast but imprecise: resolutiondate is often unset so updated is
    used as a fallback, and "added" counts ticket creation rather than first
    workflow entry.  Callers can supply exact changelog-based counts for the two
    most recent weeks via exact_current and exact_prior — these override the
    field-based values so the trend aligns with the stat cards.

    exact_current / exact_prior: (completed_count, added_to_board_count)
    """
    today = date.today()
    days_since_start = (today.weekday() - week_start_day) % 7
    this_week_start = today - timedelta(days=days_since_start)
    last_week_end = this_week_start - timedelta(days=1)

    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    start_name = day_names[week_start_day]

    weeks: list[tuple[date, date, str]] = []
    for i in range(5, -1, -1):
        w_end = last_week_end - timedelta(weeks=i)
        w_start = w_end - timedelta(days=6)
        try:
            label = f"{w_start.strftime('%-d %b')}–{w_end.strftime('%-d %b')}"
        except ValueError:
            label = f"{w_start.strftime('%#d %b')}–{w_end.strftime('%#d %b')}"
        weeks.append((w_start, w_end, label))

    completed = [0] * 6
    added = [0] * 6

    for iss in resolved_issues:
        d = _resolve_date(iss.get("fields") or {}, tz)
        if d:
            for i, (ws, we, _) in enumerate(weeks):
                if ws <= d <= we:
                    completed[i] += 1
                    break

    for iss in created_issues:
        val = (iss.get("fields") or {}).get("created", "")
        if val:
            try:
                d = parse_jira_dt(val).astimezone(tz).date()
                for i, (ws, we, _) in enumerate(weeks):
                    if ws <= d <= we:
                        added[i] += 1
                        break
            except Exception:
                pass

    # Override the two most recent weeks with exact changelog-based counts.
    # Index 5 = most recent (= current report period), 4 = prior period.
    exact_week_indices: list[int] = []
    if exact_current is not None:
        completed[5], added[5] = exact_current
        exact_week_indices.append(5)
    if exact_prior is not None:
        completed[4], added[4] = exact_prior
        exact_week_indices.append(4)

    return {
        "labels": [w[2] for w in weeks],
        "completed": completed,
        "added": added,
        "week_start": start_name,
        "exact_indices": exact_week_indices,  # chart uses these to style bars differently
    }


# ---------------------------------------------------------------------------
# Stage cycle time
# ---------------------------------------------------------------------------

def _compute_stage_cycle_times(
    cycle_time_records: list[dict],
    done_statuses: list[str],
    status_categories: dict[str, str],
    non_wip_statuses: list[str] | None = None,
) -> list[dict]:
    """Aggregate per-stage dwell times across completed tickets.

    Uses Jira's native status category to distinguish in-flow stages from
    pre-flow ones — far more reliable than the previous col_order approach,
    which silently failed when active_status was not mapped in the board
    column configuration (causing active_col to default to -1 and the
    pre-flow guard to never fire, which is why Backlog/Triage/Accepted
    appeared in the stage breakdown despite being regression stages).

    Inclusion rule:
      - "In Progress" category stages (In Progress, In Review, Testing, etc.)
      - "Done" category stages that are NOT in done_statuses — these are
        intermediate gates (e.g. "Resolved" = PR merged, awaiting QA).

    Exclusion rules:
      - "To Do" category stages (Backlog, Accepted, Draft, Triage, etc.)
      - done_statuses (Closed, Abandoned — truly finished; ~0 dwell anyway)
      - non_wip_statuses (Classified — pre-flow despite "In Progress" Jira category)
      - Jira migration artefacts whose name contains "(migrated)"
      - Unknown category (status not returned by the statuses API) — excluded
        conservatively to avoid showing noise.
    """
    excl = set(done_statuses) | set(non_wip_statuses or [])

    stage_times: dict[str, list[float]] = defaultdict(list)
    for rec in cycle_time_records:
        for stage, days in rec["stage_times"].items():
            if stage in excl:
                continue
            if "(migrated)" in stage.lower():
                continue
            cat = status_categories.get(stage, "")
            # Include In Progress category + Done category (intermediate gates)
            if cat not in ("In Progress", "Done"):
                continue
            stage_times[stage].append(days)

    result = [
        {
            "stage": stage,
            "count": len(times),
            "p50": round(_percentile(times, 50) or 0, 1),
            "p85": round(_percentile(times, 85) or 0, 1),
        }
        for stage, times in stage_times.items()
    ]
    result.sort(key=lambda x: x["p85"], reverse=True)
    return result


# ---------------------------------------------------------------------------
# Monte Carlo
# ---------------------------------------------------------------------------

def _monte_carlo_forecast(
    historical_daily: dict[str, int],
    forecast_days: int = 7,
    n_sims: int = 10_000,
) -> dict | None:
    counts = list(historical_daily.values())
    if len(counts) < 10:
        return None

    rng = random.Random(42)
    totals = sorted(
        sum(rng.choice(counts) for _ in range(forecast_days))
        for _ in range(n_sims)
    )

    def _pct(p: float) -> int:
        return totals[int(n_sims * p / 100)]

    at_least_85 = _pct(15)
    at_least_70 = _pct(30)
    at_least_50 = _pct(50)

    freq: dict[int, int] = defaultdict(int)
    for v in totals:
        freq[v] += 1
    lo, hi = min(freq), max(freq)
    mc_labels = list(range(lo, hi + 1))
    mc_probs = [round(freq.get(v, 0) / n_sims * 100, 2) for v in mc_labels]

    return {
        "at_least_85": at_least_85,
        "at_least_70": at_least_70,
        "at_least_50": at_least_50,
        "labels": mc_labels,
        "probs": mc_probs,
        "forecast_days": forecast_days,
        "data_days": len(counts),
    }


# ---------------------------------------------------------------------------
# Assignee stats
# ---------------------------------------------------------------------------

def _compute_assignee_stats(
    transitions: list[StatusTransition],
    added_issues: list[dict],
    done_statuses: list[str],
) -> list[dict]:
    stats: dict[str, dict] = {}

    def _row(name: str) -> dict:
        return stats.setdefault(name, {"name": name, "completed": 0, "added": 0, "backward": 0})

    for t in transitions:
        row = _row(t.assignee_name)
        if t.to_status in done_statuses:
            row["completed"] += 1
        if t.is_backward:
            row["backward"] += 1
    for iss in added_issues:
        _row(iss.get("assignee_name", "Unassigned"))["added"] += 1

    return sorted(stats.values(), key=lambda r: r["completed"], reverse=True)


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------

def build_report_data(
    period: str,
    client: JiraClient,
    board_id: int,
    done_statuses: list[str],
    active_status: str,
    base_url: str,
    board_url: str,
    max_wip_age_days: int = 180,
    exclude_types: list[str] | None = None,
    support_case_type: str = "Support Case",
    week_start_day: int = 0,
    non_wip_statuses: list[str] | None = None,
) -> ReportData:
    tz = _local_offset()
    now = datetime.now(tz=tz)
    start, end = date_window(period, week_start_day)
    start_dt, end_dt = _window_datetimes(start, end)

    delta = (end - start).days + 1
    prior_end = start - timedelta(days=1)
    prior_start = prior_end - timedelta(days=delta - 1)
    prior_start_dt, prior_end_dt = _window_datetimes(prior_start, prior_end)

    print("  Fetching board column order + status categories…")
    col_order = client.get_status_column_order(board_id)
    status_categories = client.get_status_categories()

    print(f"  Fetching transitions — current period ({start} – {end})…")
    transitions, added_issues, cycle_time_records = _fetch_transitions_in_window(
        client, board_id, start_dt, end_dt, done_statuses, active_status,
        col_order, exclude_types,
    )

    print(f"  Fetching transitions — prior period ({prior_start} – {prior_end})…")
    prior_transitions, prior_added_issues, _ = _fetch_transitions_in_window(
        client, board_id, prior_start_dt, prior_end_dt, done_statuses, active_status,
        col_order, exclude_types,
    )

    # Overall cycle time percentiles
    ct_values = [r["cycle_time_days"] for r in cycle_time_records]
    ct_p50 = _percentile(ct_values, 50)
    ct_p85 = _percentile(ct_values, 85)
    ct_p95 = _percentile(ct_values, 95)

    # Support Case vs Other cycle time split
    sc_recs   = [r for r in cycle_time_records if r["issue_type"] == support_case_type]
    other_recs = [r for r in cycle_time_records if r["issue_type"] != support_case_type]
    sc_vals    = [r["cycle_time_days"] for r in sc_recs]
    other_vals = [r["cycle_time_days"] for r in other_recs]

    def _rnd(v: float | None) -> float | None:
        return round(v, 1) if v is not None else None

    print("  Fetching aging WIP…")
    aging_wip, aging_stage_buckets, aging_top_at_risk, wip_by_type, stale_count, stale_top = \
        _fetch_aging_wip(
            client, board_id, done_statuses, active_status,
            now, ct_p50, ct_p85, max_wip_age_days, exclude_types, non_wip_statuses,
        )
    total_in_progress = sum(wip_by_type.values())

    print("  Fetching 6-week trend + Monte Carlo history…")
    excl_jql = _type_excl_jql(exclude_types)
    resolved_42d = client.get_resolved_in_period(board_id, done_statuses, days=42, extra_jql=excl_jql)
    created_42d = client.get_created_in_period(board_id, days=42, extra_jql=excl_jql)
    # weekly_trend is built after throughput series so we can inject exact counts (see below)

    historical_daily: dict[str, int] = {}
    for iss in resolved_42d:
        d = _resolve_date(iss.get("fields") or {}, tz)
        if d:
            ds = d.isoformat()
            historical_daily[ds] = historical_daily.get(ds, 0) + 1
    mc = _monte_carlo_forecast(historical_daily, forecast_days=delta)

    labels, current_counts, throughput_by_type = _throughput_series(
        transitions, done_statuses, start, end,
    )
    _, prior_counts, _ = _throughput_series(
        prior_transitions, done_statuses, prior_start, prior_end,
    )

    # Build 6-week trend now that exact counts are available.
    # The two most recent weeks are overridden with changelog-based numbers so
    # the trend bars agree with the stat cards, which use the same source.
    # Older weeks use the faster resolutiondate/created-date approximation.
    weekly_trend = _compute_weekly_trend(
        resolved_42d, created_42d, tz, week_start_day,
        exact_current=(sum(current_counts), len(added_issues)),
        exact_prior=(sum(prior_counts), len(prior_added_issues)),
    )

    # Stage cycle times — overall, SC, and Other
    stage_ct = _compute_stage_cycle_times(cycle_time_records, done_statuses, status_categories, non_wip_statuses)
    stage_ct_sc = _compute_stage_cycle_times(sc_recs, done_statuses, status_categories, non_wip_statuses)
    stage_ct_other = _compute_stage_cycle_times(other_recs, done_statuses, status_categories, non_wip_statuses)

    # Transition aggregation
    transition_counts: dict[str, int] = {}
    transition_issues: dict[str, list[dict]] = {}
    for t in transitions:
        pair = f"{t.from_status} → {t.to_status}"
        transition_counts[pair] = transition_counts.get(pair, 0) + 1
        transition_issues.setdefault(pair, []).append({
            "key": t.issue_key, "summary": t.issue_summary,
            "issue_type": t.issue_type, "is_backward": t.is_backward,
        })

    backward_pairs = {
        p for p in transition_counts
        if " → " in p and _is_backward(*p.split(" → ", 1), col_order)
    }

    return ReportData(
        period=period,
        period_label=_period_label(period, start, end),
        start_date=start,
        end_date=end,
        generated_at=now,
        board_id=board_id,
        base_url=base_url,
        board_url=board_url,
        transitions=transitions,
        transition_counts=transition_counts,
        transition_issues=transition_issues,
        backward_pairs=backward_pairs,
        throughput_labels=labels,
        throughput_current=current_counts,
        throughput_prior=prior_counts,
        throughput_by_type=throughput_by_type,
        weekly_trend=weekly_trend,
        cycle_times=cycle_time_records,
        ct_p50=_rnd(ct_p50), ct_p85=_rnd(ct_p85), ct_p95=_rnd(ct_p95),
        support_case_type=support_case_type,
        ct_p50_sc=_rnd(_percentile(sc_vals, 50)),
        ct_p85_sc=_rnd(_percentile(sc_vals, 85)),
        ct_p95_sc=_rnd(_percentile(sc_vals, 95)),
        ct_count_sc=len(sc_recs),
        ct_p50_other=_rnd(_percentile(other_vals, 50)),
        ct_p85_other=_rnd(_percentile(other_vals, 85)),
        ct_p95_other=_rnd(_percentile(other_vals, 95)),
        ct_count_other=len(other_recs),
        stage_ct=stage_ct,
        stage_ct_sc=stage_ct_sc,
        stage_ct_other=stage_ct_other,
        aging_wip=aging_wip,
        aging_stage_buckets=aging_stage_buckets,
        aging_top_at_risk=aging_top_at_risk,
        stale_wip_count=stale_count,
        stale_wip_issues=stale_top,
        stale_threshold_days=max_wip_age_days,
        wip_by_type=wip_by_type,
        assignee_stats=_compute_assignee_stats(transitions, added_issues, done_statuses),
        tickets_added=len(added_issues),
        tickets_added_issues=added_issues,
        mc=mc,
        total_completed=sum(1 for t in transitions if t.to_status in done_statuses),
        prior_total_completed=sum(1 for t in prior_transitions if t.to_status in done_statuses),
        total_in_progress=total_in_progress,
        backward_transition_count=sum(1 for t in transitions if t.is_backward),
    )
