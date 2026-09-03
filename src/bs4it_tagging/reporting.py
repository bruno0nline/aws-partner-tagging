from __future__ import annotations

import csv
import logging
from datetime import UTC, datetime
from pathlib import Path

from .models import ResourceRecord

logger = logging.getLogger(__name__)

# Fixed columns always present in the report
_BASE_FIELDS = [
    "timestamp",
    "account_id",
    "account_name",
    "region",
    "service",
    "resource_type",
    "resource_arn",
    "aws_apn_id",
    "status",
    "action",
    "error",
]


def _csv_safe(value: object) -> object:
    """Prevent spreadsheet formula execution while preserving ordinary values."""
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def _additional_tag_columns(records: list[ResourceRecord], required_tags: dict[str, str]) -> list[str]:
    """Return sorted list of additional tag keys (everything except aws-apn-id)."""
    keys = sorted(k for k in required_tags if k != "aws-apn-id")
    return keys


def generate_report(
    records: list[ResourceRecord],
    reports_dir: str = "reports",
    required_tags: dict[str, str] | None = None,
) -> Path:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    out_dir = Path(reports_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"tagging-report-{ts}.csv"

    required_tags = required_tags or {}
    extra_cols = _additional_tag_columns(records, required_tags)
    fieldnames = _BASE_FIELDS + extra_cols

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in records:
            row: dict = {
                "timestamp": ts,
                "account_id": r.account_id,
                "account_name": r.account_name,
                "region": r.region,
                "service": r.service,
                "resource_type": r.resource_type,
                "resource_arn": r.resource_arn,
                "aws_apn_id": r.aws_apn_id,
                "status": r.status.value,
                "action": r.action.value,
                "error": r.error or "",
            }
            for col in extra_cols:
                row[col] = r.get_tag(col)
            writer.writerow({key: _csv_safe(value) for key, value in row.items()})

    logger.info("Report written to %s", out_path)
    return out_path


def print_summary(records: list[ResourceRecord]) -> None:
    from collections import Counter

    counts = Counter(r.status.value for r in records)
    total = len(records)
    print(f"\n{'=' * 50}")
    print(f"  PRM TAGGING SUMMARY — {total} resource(s) analysed")
    print(f"{'=' * 50}")
    for status, count in sorted(counts.items()):
        print(f"  {status:<15} {count:>6}")
    print(f"{'=' * 50}\n")
