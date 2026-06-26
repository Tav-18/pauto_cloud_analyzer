from __future__ import annotations

import re
from collections import Counter
from typing import Any


EXPECTED_DETAIL_COLUMNS = [
    "No. Issue",
    "Flow",
    "Rule ID",
    "Rule",
    "Severity",
    "Internal Path",
    "Target",
    "Impact Area",
    "Finding",
    "Suggestion",
    "Manual Review Required",
    "Reasoning",
]


def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _severity_to_int(severity: str) -> int:
    match = re.search(r"(\d+)", severity or "")
    if not match:
        return 0
    return int(match.group(1))


def _short_rule_name(rule: str) -> str:
    value = _safe_str(rule)

    match = re.match(r"^Rule\s+\d+\s*-\s*(.+)$", value, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip()

    return value


def _row_to_dict(columns: list[str], row: list[Any]) -> dict[str, Any]:
    mapped: dict[str, Any] = {}

    for index, column in enumerate(columns):
        mapped[column] = row[index] if index < len(row) else ""

    return mapped


def _clean_visible_name(value: str) -> str:
    """
    Convierte nombres técnicos a algo más visible:
    - reemplaza guiones bajos por espacios
    - limpia espacios repetidos
    """
    text = _safe_str(value).replace("_", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _normalize_internal_path(flow: str, internal_path: str, target: str) -> str:
    """
    Queremos que Internal Path quede estilo:
    - Flow / Action
    - Flow / Scope / Action
    - Flow / Parameters / Parameter[/Subfield]
    """
    flow = _safe_str(flow)
    raw_internal = _safe_str(internal_path)
    raw_target = _safe_str(target)

    # Si el internal_path ya viene legible tipo "Flow / Activity", respetarlo
    if raw_internal and " / " in raw_internal and raw_internal.startswith(flow):
        return raw_internal

    # Si parece ruta técnica de parameters
    if "parameters." in raw_internal.lower():
        after = raw_internal.split("parameters.", 1)[1]
        after = after.replace(".defaultValue.", ".")
        after = after.replace(".defaultValue", "")
        parts = [p for p in after.split(".") if p]
        parts = [_clean_visible_name(p) for p in parts]
        if flow and parts:
            return f"{flow} / Parameters / " + " / ".join(parts)

    # Si parece ruta técnica de actions
    if "actions." in raw_internal.lower():
        after = raw_internal.split("actions.", 1)[1]
        parts = [p for p in after.split(".actions.") if p]
        parts = [segment.split(".inputs")[0].split(".runAfter")[0] for segment in parts]
        parts = [_clean_visible_name(p) for p in parts if p]
        if flow and parts:
            return f"{flow} / " + " / ".join(parts)

    # Si no hay internal_path técnico usable, usar Flow / Target
    clean_target = _clean_visible_name(raw_target)
    if flow and clean_target:
        return f"{flow} / {clean_target}"
    if flow:
        return flow
    return clean_target


def _normalize_target(target: str, internal_path: str) -> str:
    """
    Target corto = elemento puntual:
    - actividad
    - parámetro
    - subcampo
    """
    raw_target = _safe_str(target)
    raw_internal = _safe_str(internal_path)

    if raw_target:
        return _clean_visible_name(raw_target)

    if "parameters." in raw_internal.lower():
        after = raw_internal.split("parameters.", 1)[1]
        after = after.replace(".defaultValue.", ".")
        after = after.replace(".defaultValue", "")
        parts = [p for p in after.split(".") if p]
        if parts:
            return _clean_visible_name(parts[-1])

    if "actions." in raw_internal.lower():
        after = raw_internal.split("actions.", 1)[1]
        parts = [p for p in after.split(".actions.") if p]
        parts = [segment.split(".inputs")[0].split(".runAfter")[0] for segment in parts]
        if parts:
            return _clean_visible_name(parts[-1])

    return _clean_visible_name(raw_internal)


def normalize_review_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("El payload de Claude no es un objeto JSON válido.")

    detail_columns = payload.get("detail_columns") or EXPECTED_DETAIL_COLUMNS
    detail_rows = payload.get("detail_rows") or []

    if not isinstance(detail_columns, list):
        detail_columns = EXPECTED_DETAIL_COLUMNS

    if not isinstance(detail_rows, list):
        detail_rows = []

    raw_findings: list[dict[str, Any]] = []

    for index, row in enumerate(detail_rows, start=1):
        if isinstance(row, list):
            mapped = _row_to_dict(detail_columns, row)
        elif isinstance(row, dict):
            mapped = row
        else:
            continue

        no_issue = _safe_str(mapped.get("No. Issue")) or str(index)
        flow = _safe_str(mapped.get("Flow"))
        rule_id = _safe_str(mapped.get("Rule ID"))
        rule_full_name = _safe_str(mapped.get("Rule"))
        rule_name = _short_rule_name(rule_full_name)
        severity = _safe_str(mapped.get("Severity"))
        severity_level = _severity_to_int(severity)

        raw_internal_path = _safe_str(mapped.get("Internal Path"))
        raw_target = _safe_str(mapped.get("Target"))

        normalized_internal_path = _normalize_internal_path(
            flow=flow,
            internal_path=raw_internal_path,
            target=raw_target,
        )
        normalized_target = _normalize_target(
            target=raw_target,
            internal_path=raw_internal_path,
        )

        impact_area = _safe_str(mapped.get("Impact Area"))
        finding = _safe_str(mapped.get("Finding"))
        suggestion = _safe_str(mapped.get("Suggestion"))
        manual_review_required = _safe_str(mapped.get("Manual Review Required"))
        reasoning = _safe_str(mapped.get("Reasoning"))

        group_key = f"{rule_id}||{normalized_internal_path}||{normalized_target}||{impact_area}||{suggestion}"

        raw_findings.append(
            {
                "no_issue": no_issue,
                "flow_name": flow,
                "rule_id": rule_id,
                "rule_full_name": rule_full_name,
                "rule_name": rule_name,
                "severity": severity,
                "severity_level": severity_level,
                "internal_path": normalized_internal_path,
                "target": normalized_target,
                "target_pretty": normalized_target,
                "impact_area": impact_area,
                "reason": finding,
                "suggestion": suggestion,
                "manual_review_required": manual_review_required,
                "reasoning": reasoning,
                "group_key": group_key,
            }
        )

    counts = Counter(item["group_key"] for item in raw_findings)

    findings: list[dict[str, Any]] = []
    for item in raw_findings:
        item["repeat_count"] = counts[item["group_key"]]
        item["target_key"] = item["group_key"]
        findings.append(item)

    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    review_scope = (
        payload.get("review_scope") if isinstance(payload.get("review_scope"), dict) else {}
    )
    errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []

    return {
        "schema_version": _safe_str(payload.get("schema_version")),
        "project_id": _safe_str(payload.get("project_id")),
        "review_scope": review_scope,
        "summary": summary,
        "errors": errors,
        "findings": findings,
        "detail_columns": detail_columns,
        "detail_rows": detail_rows,
    }


def _finding_dedup_key(finding: dict[str, Any]) -> str:
    """
    La Skill define la deduplicación por:
    Flow + Rule ID + Internal Path + Target.
    """
    flow_name = _safe_str(finding.get("flow_name")).casefold()
    rule_id = _safe_str(finding.get("rule_id")).casefold()
    internal_path = _safe_str(finding.get("internal_path")).casefold()
    target = _safe_str(finding.get("target")).casefold()

    return "||".join(
        [
            flow_name,
            rule_id,
            internal_path,
            target,
        ]
    )


def merge_normalized_reviews(
    normalized_reviews: list[dict[str, Any]],
    *,
    project_id: str = "",
) -> dict[str, Any]:
    """
    Combina los resultados de varios JSON analizados por separado.

    También vuelve a deduplicar los hallazgos porque cada petición de Claude
    desconoce las incidencias obtenidas en las peticiones anteriores.
    """
    combined_errors: list[Any] = []
    combined_detail_rows: list[Any] = []
    combined_findings: dict[str, dict[str, Any]] = {}

    schema_version = "pauto_cloud_rows_v3"
    platform = "Power Automate Cloud"
    active_rules = 0
    flow_names: set[str] = set()

    for review in normalized_reviews:
        if not isinstance(review, dict):
            continue

        current_schema = _safe_str(review.get("schema_version"))
        if current_schema:
            schema_version = current_schema

        current_scope = review.get("review_scope")
        if isinstance(current_scope, dict):
            current_platform = _safe_str(current_scope.get("platform"))
            if current_platform:
                platform = current_platform

            try:
                active_rules = max(
                    active_rules,
                    int(current_scope.get("active_rules", 0) or 0),
                )
            except (TypeError, ValueError):
                pass

        current_errors = review.get("errors")
        if isinstance(current_errors, list):
            combined_errors.extend(current_errors)

        current_rows = review.get("detail_rows")
        if isinstance(current_rows, list):
            combined_detail_rows.extend(current_rows)

        current_findings = review.get("findings")
        if not isinstance(current_findings, list):
            continue

        for finding in current_findings:
            if not isinstance(finding, dict):
                continue

            flow_name = _safe_str(finding.get("flow_name"))
            if flow_name:
                flow_names.add(flow_name)

            dedup_key = _finding_dedup_key(finding)
            repeat_count = max(
                1,
                int(finding.get("repeat_count", 1) or 1),
            )

            if dedup_key not in combined_findings:
                stored_finding = dict(finding)
                stored_finding["group_key"] = dedup_key
                stored_finding["target_key"] = dedup_key
                stored_finding["repeat_count"] = repeat_count
                combined_findings[dedup_key] = stored_finding
                continue

            combined_findings[dedup_key]["repeat_count"] = (
                int(
                    combined_findings[dedup_key].get(
                        "repeat_count",
                        1,
                    )
                    or 1
                )
                + repeat_count
            )

    findings = list(combined_findings.values())

    level_1 = sum(
        1
        for finding in findings
        if int(finding.get("severity_level", 0) or 0) == 1
    )
    level_2 = sum(
        1
        for finding in findings
        if int(finding.get("severity_level", 0) or 0) == 2
    )
    level_3 = sum(
        1
        for finding in findings
        if int(finding.get("severity_level", 0) or 0) == 3
    )

    manual_review_yes = sum(
        1
        for finding in findings
        if _safe_str(
            finding.get("manual_review_required")
        ).casefold()
        == "yes"
    )
    manual_review_no = sum(
        1
        for finding in findings
        if _safe_str(
            finding.get("manual_review_required")
        ).casefold()
        == "no"
    )

    return {
        "schema_version": schema_version,
        "project_id": _safe_str(project_id),
        "review_scope": {
            "platform": platform,
            "active_rules": active_rules,
            "flows_reviewed": len(flow_names),
        },
        "summary": {
            "total_findings": len(findings),
            "level_3_findings": level_3,
            "level_2_findings": level_2,
            "level_1_findings": level_1,
            "manual_review_yes": manual_review_yes,
            "manual_review_no": manual_review_no,
        },
        "errors": combined_errors,
        "findings": findings,
        "detail_columns": EXPECTED_DETAIL_COLUMNS,
        "detail_rows": combined_detail_rows,
    }

