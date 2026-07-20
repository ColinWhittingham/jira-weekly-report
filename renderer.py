import json
from datetime import date, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from report_builder import ReportData


def _tojson(value) -> str:
    def default(obj):
        if isinstance(obj, (date, datetime)):
            return obj.isoformat()
        raise TypeError(f"Object of type {type(obj)} is not JSON serializable")
    return json.dumps(value, default=default)


def render_report(report: ReportData, template_dir: Path) -> str:
    env = Environment(loader=FileSystemLoader(str(template_dir)), autoescape=False)
    env.filters["tojson"] = _tojson
    template = env.get_template("report.html.j2")
    return template.render(report=report)
