import os
import re
import time
from pathlib import Path
from typing import Generator

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")


class ConfigError(Exception):
    pass


class JiraClient:
    def __init__(self, base_url: str):
        email = os.getenv("JIRA_EMAIL")
        token = os.getenv("JIRA_API_TOKEN")
        if not email or not token:
            raise ConfigError(
                "JIRA_EMAIL and JIRA_API_TOKEN must be set in .env\n"
                "Copy .env.example to .env and fill in your credentials."
            )
        self.base_url = base_url.rstrip("/")
        self._session = requests.Session()
        self._session.auth = (email, token)
        self._session.headers.update({"Accept": "application/json"})

    def _get(self, path: str, params: dict = None) -> dict:
        url = f"{self.base_url}{path}"
        for attempt in range(3):
            resp = self._session.get(url, params=params or {})
            if resp.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            return resp.json()
        resp.raise_for_status()  # raise after final retry

    def _get_paginated(self, path: str, params: dict, items_key: str) -> Generator[dict, None, None]:
        start_at = 0
        max_results = 100
        while True:
            data = self._get(path, {**params, "startAt": start_at, "maxResults": max_results})
            items = data.get(items_key, [])
            yield from items
            if not items:
                break
            start_at += len(items)
            # REST API uses "total"; Agile API uses "isLast"
            is_last = data.get("isLast", False)
            total = data.get("total")
            if is_last or (total is not None and start_at >= total):
                break

    def get_board_issues(
        self,
        board_id: int,
        jql: str = None,
        fields: str = "status,summary,issuetype",
    ) -> Generator[dict, None, None]:
        path = f"/rest/agile/1.0/board/{board_id}/issue"
        params = {"fields": fields}
        if jql:
            params["jql"] = jql
        yield from self._get_paginated(path, params, "issues")

    def get_status_column_order(self, board_id: int) -> dict[str, int]:
        """Return {status_name: column_index} from the board's column configuration.

        Used to detect backward transitions: a move is backward when the source
        column index is greater than the destination column index.
        """
        data = self._get(f"/rest/agile/1.0/board/{board_id}/configuration")
        columns = data.get("columnConfig", {}).get("columns", [])
        order: dict[str, int] = {}
        for col_idx, col in enumerate(columns):
            for status in col.get("statuses", []):
                name = status.get("name", "")
                if name:
                    order[name] = col_idx
        return order

    def get_status_categories(self) -> dict[str, str]:
        """Return {status_name: category_name} for every status in this Jira instance.

        Category names are: "To Do", "In Progress", "Done".
        Used by _compute_stage_cycle_times to reliably exclude pre-flow stages
        (To Do category) without relying on board column configuration, which
        may not map every status and causes the active_col sentinel to fail.
        """
        data = self._get("/rest/api/3/status")
        if not isinstance(data, list):
            return {}
        return {
            s.get("name", ""): s.get("statusCategory", {}).get("name", "")
            for s in data
            if s.get("name")
        }

    def get_created_in_period(self, board_id: int, days: int = 42, extra_jql: str = "") -> list[dict]:
        """Return board issues created in the last `days` days (created field only)."""
        return list(self.get_board_issues(
            board_id, jql=f'created >= -{days}d{extra_jql}', fields="created",
        ))

    def get_resolved_in_period(
        self,
        board_id: int,
        done_statuses: list[str],
        days: int = 42,
        extra_jql: str = "",
    ) -> list[dict]:
        """Return board issues that reached a done status in the last `days` days.

        Filters on `updated >= -Xd` rather than `resolutiondate >= -Xd` because
        resolutiondate is not set by all Jira workflows — using it silently drops
        completions and causes the weekly trend chart to miss the most recent week.
        Callers should prefer resolutiondate when present and fall back to updated.
        """
        status_list = ",".join(f'"{s}"' for s in done_statuses)
        jql = f'status in ({status_list}) AND updated >= -{days}d{extra_jql}'
        return list(self.get_board_issues(board_id, jql=jql, fields="resolutiondate,updated,status"))

    def get_issue_changelog(self, issue_key: str) -> list[dict]:
        """Return all status-field changelog entries for a single issue."""
        path = f"/rest/api/3/issue/{issue_key}/changelog"
        entries = []
        start_at = 0
        max_results = 100
        while True:
            data = self._get(path, {"startAt": start_at, "maxResults": max_results})
            values = data.get("values", [])
            for history in values:
                created = history.get("created", "")
                for item in history.get("items", []):
                    if item.get("field") == "status":
                        entries.append({
                            "created": created,
                            "fromString": item.get("fromString") or "",
                            "toString": item.get("toString") or "",
                        })
            total = data.get("total", 0)
            start_at += len(values)
            if not values or start_at >= total:
                break
        return entries


def parse_jira_dt(iso_str: str):
    """Parse a Jira ISO-8601 timestamp into a timezone-aware datetime.

    Handles Jira's format "2026-07-13T10:30:00.000+0200" on Python 3.10+.
    """
    from datetime import datetime
    s = re.sub(r"\.\d+", "", iso_str)           # strip milliseconds
    s = re.sub(r"([+-])(\d{2})(\d{2})$", r"\1\2:\3", s)  # +0200 → +02:00
    return datetime.fromisoformat(s)
