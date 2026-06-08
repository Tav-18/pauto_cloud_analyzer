from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _guess_flow_name(raw: dict[str, Any], fallback: str) -> str:
    return (
        raw.get("properties", {}).get("displayName")
        or raw.get("name")
        or raw.get("properties", {}).get("friendlyName")
        or fallback
    )


def _get_workflow_raw(raw: dict[str, Any]) -> dict[str, Any]:
    definition = (
        raw.get("properties", {}).get("definition")
        or raw.get("definition")
        or {}
    )

    actions = definition.get("actions") or {}

    if isinstance(actions, dict) and actions:
        return raw

    resources = raw.get("resources")
    if isinstance(resources, list):
        for resource in resources:
            if not isinstance(resource, dict):
                continue

            resource_type = str(resource.get("type", "") or "").lower()
            if resource_type == "microsoft.logic/workflows":
                return resource

    return raw


def _get_definition(workflow_raw: dict[str, Any]) -> dict[str, Any]:
    return (
        workflow_raw.get("properties", {}).get("definition")
        or workflow_raw.get("definition")
        or {}
    )


def _count_actions_recursive(actions_dict: Any) -> int:
    if not isinstance(actions_dict, dict):
        return 0

    count = 0

    for _, action_body in actions_dict.items():
        if not isinstance(action_body, dict):
            continue

        count += 1

        nested_actions = action_body.get("actions")
        count += _count_actions_recursive(nested_actions)

        else_block = action_body.get("else")
        if isinstance(else_block, dict):
            count += _count_actions_recursive(else_block.get("actions"))

        branches = action_body.get("branches")
        if isinstance(branches, list):
            for branch in branches:
                if isinstance(branch, dict):
                    count += _count_actions_recursive(branch.get("actions"))

        cases = action_body.get("cases")
        if isinstance(cases, dict):
            for _, case_body in cases.items():
                if isinstance(case_body, dict):
                    count += _count_actions_recursive(case_body.get("actions"))

        default_case = action_body.get("defaultCase")
        if isinstance(default_case, dict):
            count += _count_actions_recursive(default_case.get("actions"))

    return count


def collect_flow_metrics(json_files: list[str]) -> dict[str, Any]:
    total_flows = 0
    total_actions = 0
    flow_names: list[str] = []
    invalid_files: list[str] = []

    for json_file in json_files:
        path = Path(json_file)

        try:
            with path.open("r", encoding="utf-8") as file_obj:
                raw = json.load(file_obj)

            if not isinstance(raw, dict):
                invalid_files.append(path.name)
                continue

            workflow_raw = _get_workflow_raw(raw)
            definition = _get_definition(workflow_raw)
            actions = definition.get("actions") or {}

            action_count = _count_actions_recursive(actions)

            flow_name = _guess_flow_name(workflow_raw, fallback=path.stem)

            total_flows += 1
            total_actions += action_count
            flow_names.append(str(flow_name))

        except Exception:
            invalid_files.append(path.name)

    return {
        "total_flows": total_flows,
        "total_actions": total_actions,
        "flow_names": flow_names,
        "invalid_files": invalid_files,
    }