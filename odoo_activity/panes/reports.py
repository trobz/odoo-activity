"""Reports tab body (Diagnostics half) -- renders odoo-db's `reports`
audit plus two host-level facts odoo-db has no access to, into a RichLog,
same section-table-per-source convention panes/mail.py and
panes/neutralization.py already use: the sections don't share columns, so
flattening them into one generic DataTable reads as mostly blank cells.

Capture (arm/disarm, the captured-report list) lives under the same tab but
renders through the generic row-list DataTable instead of this module -- see
panes/detail.py's `_fetch_reports_diagnostics` docstring for why the two
halves are kept as separate fetch/render paths.
"""

from __future__ import annotations

from rich.console import Group, RenderableType
from rich.text import Text
from textual.widgets import RichLog

from odoo_activity.panes.mail import section_table


def render_reports_diagnostics(
    body: RichLog, audit: dict, wk_version: str | None, pip_pdf_packages: list[str] | None
) -> None:
    """Populate `body` with odoo-db's `reports` audit (report.url/
    report.delay, report_wkhtmltopdf_param install state + its
    per-paperformat overrides) plus the two facts only the host itself can
    answer: the actual wkhtmltopdf binary version running, and PDF-related
    pip packages in the instance's venv.
    """
    body.clear()

    renderables: list[RenderableType] = [
        Text(f"wkhtmltopdf --version: {wk_version or '(not found on PATH)'}"),
        Text(
            "PDF-related pip packages (venv): "
            + (", ".join(pip_pdf_packages) if pip_pdf_packages else "(none found, or instance not running)")
        ),
    ]

    params = audit.get("config_parameters") or []
    if params:
        renderables.append(
            section_table(
                "Config parameters",
                ["key", "value"],
                [
                    [
                        p["key"] + (f" ({p['explanation']})" if p.get("explanation") else ""),
                        "(not defined)" if p["value"] is None else p["value"],
                    ]
                    for p in params
                ],
            )
        )

    modules = audit.get("modules") or []
    if modules:
        renderables.append(
            section_table("Relevant modules", ["module", "state"], [[m["name"], m["state"]] for m in modules])
        )

    # None: report_wkhtmltopdf_param isn't installed (see
    # db.get_wkhtmltopdf_paperformat_params). [] vs a real list: installed
    # but no overrides configured, vs installed with per-paperformat
    # overrides that take precedence over report.url/report.delay above.
    paperformat_params = audit.get("paperformat_params")
    if paperformat_params is None:
        renderables.append(Text("report_wkhtmltopdf_param not installed -- no per-paperformat overrides.", style="dim"))
    elif paperformat_params:
        renderables.append(
            section_table(
                "report_wkhtmltopdf_param overrides (take precedence over report.url/report.delay above)",
                ["paperformat", "param", "value"],
                [[p["paperformat_name"], p["param_name"], p["param_value"] or ""] for p in paperformat_params],
            )
        )
    else:
        renderables.append(Text("report_wkhtmltopdf_param installed, no overrides configured.", style="dim"))

    body.write(Group(*renderables))
