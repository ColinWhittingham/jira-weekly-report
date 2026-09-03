import argparse
import json
import sys
from datetime import date
from pathlib import Path

from jira_client import ConfigError, JiraClient
from renderer import render_report
from report_builder import build_report_data

ROOT = Path(__file__).parent


def load_config() -> dict:
    config_path = ROOT / "config.json"
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(
            f"ERROR: config.json not found at {config_path}\n"
            "Copy config.example.json to config.json and fill in your Jira instance details.",
            file=sys.stderr,
        )
        sys.exit(1)
    except json.JSONDecodeError as exc:
        print(f"ERROR: Invalid config.json — {exc}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Jira board digest report")
    parser.add_argument(
        "--period",
        choices=["daily", "weekly"],
        required=True,
        help="Report period: 'daily' (yesterday) or 'weekly' (trailing 7 days)",
    )
    parser.add_argument(
        "--start-date",
        type=date.fromisoformat,
        default=None,
        metavar="YYYY-MM-DD",
        help="Override the start date for a weekly report (e.g. for an unusually long week).",
    )
    args = parser.parse_args()

    config = load_config()
    jira_cfg = config["jira"]
    output_cfg = config["output"]

    try:
        client = JiraClient(base_url=jira_cfg["base_url"])
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Generating {args.period} report…")
    try:
        report = build_report_data(
            period=args.period,
            client=client,
            board_id=jira_cfg["board_id"],
            done_statuses=jira_cfg["done_statuses"],
            active_status=jira_cfg.get("active_status", "In Progress"),
            base_url=jira_cfg["base_url"],
            board_url=jira_cfg.get("board_url", ""),
            max_wip_age_days=jira_cfg.get("max_wip_age_days", 180),
            exclude_types=jira_cfg.get("exclude_types", []),
            support_case_type=jira_cfg.get("support_case_type", "Support Case"),
            week_start_day=jira_cfg.get("week_start_day", 0),
            non_wip_statuses=jira_cfg.get("non_wip_statuses", []),
            override_start=args.start_date,
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"ERROR fetching data from Jira: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        html = render_report(report, ROOT / "templates")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"ERROR rendering report: {exc}", file=sys.stderr)
        sys.exit(1)

    output_dir = ROOT / output_cfg["directory"]
    output_dir.mkdir(exist_ok=True)
    filename = output_cfg["filename_pattern"].format(
        period=args.period,
        date=report.generated_at.date().isoformat(),
    )
    output_path = output_dir / filename
    output_path.write_text(html, encoding="utf-8")
    print(f"Done. Report written to: {output_path}")


if __name__ == "__main__":
    main()
