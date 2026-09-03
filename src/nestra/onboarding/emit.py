"""Extract a site configuration candidate from a probe report."""

from typing import Any

from nestra.core.models import ProbeReport


def emit_config(report: ProbeReport, *, tagset_group: str | None = None) -> dict[str, Any]:
    finding = report.get("config_candidate")
    if finding is None:
        raise ValueError("probe report has no config candidate")
    candidate = dict(finding.value)
    candidate["config"] = dict(candidate["config"])
    if tagset_group is not None:
        candidate["tagset_group"] = tagset_group
    return candidate
