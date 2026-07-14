"""Bug intake domain — policy constants and pure report helpers (no SQL)."""
from .policy import (
    BUG_FAILURE_CLASSES,
    BUG_INTAKE_POLICY,
    BUG_REPORT_REQUIRED_FIELDS,
    BUG_SEVERITIES,
    FAIL_FIX_FAILURE_CLASSES,
    FAIL_FIX_REQUIRED_FIELDS,
    bug_intake_policy,
    bug_report_description,
    bug_report_value_present,
    bug_title,
    fail_fix_signal_schema,
    failure_class_detail,
)

__all__ = [
    "BUG_FAILURE_CLASSES",
    "BUG_INTAKE_POLICY",
    "BUG_REPORT_REQUIRED_FIELDS",
    "BUG_SEVERITIES",
    "FAIL_FIX_FAILURE_CLASSES",
    "FAIL_FIX_REQUIRED_FIELDS",
    "bug_intake_policy",
    "bug_report_description",
    "bug_report_value_present",
    "bug_title",
    "fail_fix_signal_schema",
    "failure_class_detail",
]
