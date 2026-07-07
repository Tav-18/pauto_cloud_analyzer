from __future__ import annotations

import math
import os
import re
import shutil
import tempfile
import time
import uuid
from collections import Counter
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path

from django.http import FileResponse, Http404
from django.shortcuts import redirect, render

from .forms import (
    PLATFORM_CLOUD,
    PLATFORM_DESKTOP,
    UploadSolutionZipForm,
)
from .services.claude_review import ClaudeReviewError, run_cloud_review
from .services.desktop_inventory import collect_desktop_metrics
from .services.desktop_review import run_desktop_review
from .services.desktop_source import (
    DesktopSourceError,
    prepare_desktop_sources,
)
from .services.excel_export import export_review_to_xlsx
from .services.flow_inventory import collect_flow_metrics
from .services.review_normalizer import (
    merge_normalized_reviews,
    normalize_review_payload,
)
from .services.zip_reader import extract_zip, find_json_files, save_upload




def _debug_analysis(message: str, *values) -> None:
    """
    Imprime mensajes de depuración del flujo de análisis.

    Se usa solamente para diagnosticar por qué la pantalla regresa al picker
    sin mostrar claramente el motivo del error.
    """
    print(f"[ANALYZER DEBUG] {message}", *values, flush=True)

PICKER_ROOT = Path(tempfile.gettempdir()) / "pa_flow_picker"


PLATFORM_LABELS = {
    PLATFORM_CLOUD: "Power Automate Cloud",
    PLATFORM_DESKTOP: "Power Automate Desktop",
}


def _platform_label(platform: str) -> str:
    return PLATFORM_LABELS.get(
        platform,
        "Power Automate",
    )


RULE_DISPLAY_ORDER = {
    "Hardcode": 1,
    "Parametrizable": 2,
    "Manejo de errores (RunAfter)": 3,
    "Retrasos": 4,
    "Retrasos (Delay y Wait)": 4,
    "Nomenclatura de actividades": 5,
    "Nomenclatura de variables": 6,
    "Nomenclatura de flujos": 7,
    "Prefijos variables / parámetros": 8,
    "Condición IF": 9,
    "Descripciones de acciones": 10,
    "Comentarios descriptivos": 10,
    "Scopes vacíos o con una sola acción": 11,
    "Switch caso predeterminado vacío": 12,
    "Límite de anidación en condición": 13,
    "Id de requerimiento": 14,
    "Descripciones de flujos": 15,
}

RULE_SEVERITY_ORDER = {
    "Hardcode": 3,
    "Parametrizable": 2,
    "Manejo de errores (RunAfter)": 2,
    "Retrasos": 2,
    "Retrasos (Delay y Wait)": 2,
    "Scopes vacíos o con una sola acción": 2,
    "Switch caso predeterminado vacío": 2,
    "Límite de anidación en condición": 2,
    "Id de requerimiento": 2,
    "Nomenclatura de actividades": 1,
    "Nomenclatura de variables": 1,
    "Nomenclatura de flujos": 1,
    "Prefijos variables / parámetros": 1,
    "Condición IF": 1,
    "Descripciones de acciones": 1,
    "Comentarios descriptivos": 1,
    "Descripciones de flujos": 1,
}


def _safe_pct(value) -> float:
    try:
        return max(0.0, min(float(value or 0), 100.0))
    except (TypeError, ValueError):
        return 0.0


def _rule_order(rule_name: str) -> int:
    return RULE_DISPLAY_ORDER.get(rule_name or "", 999)


def _rule_severity(rule_name: str) -> int:
    return RULE_SEVERITY_ORDER.get(rule_name or "", 0)


def _build_analysis_status(compliance_rate: float, total_findings: int) -> dict:
    if total_findings > 100:
        return {
            "label": "Rejected",
            "variant": "rejected",
            "is_rejected": True,
        }

    if compliance_rate >= 100:
        return {
            "label": "Passed",
            "variant": "passed",
            "is_rejected": False,
        }

    return {
        "label": "Not Passed",
        "variant": "not-passed",
        "is_rejected": False,
    }


def _build_compliance_core(compliance_rate: float, total_findings: int) -> dict:
    pct = _safe_pct(compliance_rate)
    is_rejected = total_findings > 100

    if is_rejected:
        display_pct = 0
        theme = "rejected"
        tier = "Rejected"
        helper = """
        <ul class="status-helper-list">
          <li>Due to the high number of incidents, the manual review is rejected.</li>
          <li>A new date for the review is pending.</li>
          <li>The leaders must be notified of the risk, and an incident with the highest impact must be raised.</li>
        </ul>
        """
        filled_segments = 0
    else:
        display_pct = pct

        if pct >= 90:
            theme = "green"
            tier = "Optimal"
            helper = "Healthy overall adherence to best practices."
        elif pct >= 80:
            theme = "yellow"
            tier = "Stable"
            helper = "Minor drift detected. Review is recommended."
        elif pct >= 60:
            theme = "orange"
            tier = "Warning"
            helper = "Several improvements are needed before approval."
        else:
            theme = "red"
            tier = "Critical"
            helper = "Low health score. Immediate remediation is recommended."

        filled_segments = max(0, min(20, math.floor(display_pct / 5)))

    segments = [{"filled": i < filled_segments} for i in range(20)]

    return {
        "theme": theme,
        "tier": tier,
        "helper": helper,
        "display_pct": int(display_pct) if float(display_pct).is_integer() else round(display_pct, 1),
        "filled_segments": filled_segments,
        "segments": segments,
    }


def _ensure_picker_root() -> None:
    PICKER_ROOT.mkdir(parents=True, exist_ok=True)


def _cleanup_old_picker_dirs(max_age_hours: int = 24) -> None:
    if not PICKER_ROOT.exists():
        return

    cutoff = time.time() - (max_age_hours * 3600)

    for child in PICKER_ROOT.iterdir():
        try:
            if child.is_dir() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
        except Exception:
            continue


def _display_flow_name_from_file(file_path: str) -> str:
    stem = Path(file_path).stem
    return stem.split("-", 1)[0].strip() if "-" in stem else stem.strip()


def _build_json_candidates(json_files: list[str], extracted_root: str) -> list[dict]:
    candidates = []

    for idx, json_file in enumerate(sorted(json_files)):
        rel_path = os.path.relpath(json_file, extracted_root).replace("\\", "/")

        candidates.append(
            {
                "id": str(idx),
                "display_name": _display_flow_name_from_file(json_file),
                "rel_path": rel_path,
                "full_path": json_file,
            }
        )

    return candidates


def _render_upload_with_picker(
    request,
    *,
    form,
    pick_id: str,
    project_id: str,
    platform: str,
    candidates: list[dict],
    selected_ids: list[str] | None = None,
    picker_error: str | None = None,
    uploaded_file_name: str = "",
    uploaded_file_size: int = 0,
):
    if selected_ids is None:
        selected_ids = [item["id"] for item in candidates]

    return render(
        request,
        "analyzer/upload.html",
        {
            "form": form,
            "platform": platform,
            "platform_label": _platform_label(platform),
            "show_json_picker": True,
            "pick_id": pick_id,
            "project_id": project_id,
            "json_candidates": candidates,
            "json_count": len(candidates),
            "selected_json_ids": selected_ids,
            "selected_json_count": len(selected_ids),
            "picker_error": picker_error,
            "uploaded_file_name": uploaded_file_name,
            "uploaded_file_size": uploaded_file_size,
        },
    )


def _render_upload_with_desktop_picker(
    request,
    *,
    form,
    pick_id: str,
    project_id: str,
    candidates: list[dict],
    selected_ids: list[str] | None = None,
    picker_error: str | None = None,
    desktop_ready_message: str | None = None,
    uploaded_file_name: str = "",
    uploaded_file_size: int = 0,
):
    if selected_ids is None:
        selected_ids = [item["id"] for item in candidates]

    return render(
        request,
        "analyzer/upload.html",
        {
            "form": form,
            "platform": PLATFORM_DESKTOP,
            "platform_label": _platform_label(PLATFORM_DESKTOP),
            "show_desktop_picker": True,
            "pick_id": pick_id,
            "project_id": project_id,
            "desktop_candidates": candidates,
            "desktop_count": len(candidates),
            "selected_desktop_ids": selected_ids,
            "selected_desktop_count": len(selected_ids),
            "desktop_picker_error": picker_error,
            "desktop_ready_message": desktop_ready_message,
            "desktop_uploaded_file_name": uploaded_file_name,
            "desktop_uploaded_file_size": uploaded_file_size,
        },
    )


def _calculate_action_health(
    *,
    total_actions: int,
    findings: list[dict],
) -> dict:
    flagged_targets = {
        finding.get("target_key") or finding.get("target_pretty") or str(index)
        for index, finding in enumerate(findings)
    }

    flagged_actions_count = min(total_actions, len(flagged_targets)) if total_actions else 0
    passed_actions_count = max(0, total_actions - flagged_actions_count)

    if total_actions > 0:
        passed_actions_pct = round((passed_actions_count / total_actions) * 100, 1)
    else:
        passed_actions_pct = 100 if not findings else 0

    return {
        "flagged_actions_count": flagged_actions_count,
        "passed_actions_count": passed_actions_count,
        "passed_actions_pct": passed_actions_pct,
    }



def _read_analysis_budget_usd() -> Decimal | None:
    """
    Presupuesto acumulado para todos los JSON seleccionados.

    El límite se comprueba entre archivos. No puede detener una llamada de
    Claude que ya comenzó, por lo que el último archivo procesado podría hacer
    que el total rebase ligeramente el presupuesto.
    """
    raw_value = (
        os.getenv("ANTHROPIC_MAX_ESTIMATED_COST_USD", "1.50") or ""
    ).strip()

    if not raw_value:
        return None

    try:
        value = Decimal(raw_value)
    except InvalidOperation:
        return Decimal("1.50")

    return value if value > 0 else None


def _merge_usage_totals(
    total_usage: dict,
    current_usage: dict,
) -> None:
    token_fields = (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    )

    for field in token_fields:
        total_usage[field] = int(total_usage.get(field, 0) or 0) + int(
            current_usage.get(field, 0) or 0
        )

    current_tools = current_usage.get("server_tool_use") or {}
    total_tools = total_usage.setdefault("server_tool_use", {})

    if isinstance(current_tools, dict):
        for key, value in current_tools.items():
            if isinstance(value, int):
                total_tools[key] = int(total_tools.get(key, 0) or 0) + value


def _total_tokens_from_usage(usage: dict) -> int:
    return sum(
        int(usage.get(field, 0) or 0)
        for field in (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
    )


def _append_usage_calls(
    aggregate_calls: list[dict],
    current_calls: list[dict],
    *,
    source_file: str,
) -> None:
    for current_call in current_calls:
        normalized_call = dict(current_call)
        normalized_call["call_index"] = len(aggregate_calls) + 1
        normalized_call["source_file"] = source_file
        aggregate_calls.append(normalized_call)


def upload_view(request):
    platform = (
        request.POST.get("platform")
        or PLATFORM_CLOUD
    ).strip().lower()

    if request.method == "POST":
        form = UploadSolutionZipForm(
            request.POST,
            request.FILES,
        )

        if form.is_valid():
            platform = form.cleaned_data["platform"]

            project_id = (
                form.cleaned_data.get("project_id")
                or ""
            ).strip()

            if platform == PLATFORM_DESKTOP:
                _ensure_picker_root()
                _cleanup_old_picker_dirs()

                pick_id = str(uuid.uuid4())
                pick_dir = PICKER_ROOT / pick_id
                pick_dir.mkdir(
                    parents=True,
                    exist_ok=True,
                )

                try:
                    source_info = prepare_desktop_sources(
                        form.cleaned_data.get("desktop_files") or [],
                        pick_dir,
                    )
                except DesktopSourceError as exc:
                    shutil.rmtree(
                        pick_dir,
                        ignore_errors=True,
                    )

                    return render(
                        request,
                        "analyzer/upload.html",
                        {
                            "form": form,
                            "platform": platform,
                            "platform_label": _platform_label(platform),
                            "project_id": project_id,
                            "error": str(exc),
                        },
                    )
                except Exception as exc:
                    shutil.rmtree(
                        pick_dir,
                        ignore_errors=True,
                    )

                    return render(
                        request,
                        "analyzer/upload.html",
                        {
                            "form": form,
                            "platform": platform,
                            "platform_label": _platform_label(platform),
                            "project_id": project_id,
                            "error": (
                                "Could not process Desktop source: "
                                f"{exc}"
                            ),
                        },
                    )

                candidates = source_info["candidates"]
                uploaded_file_name = source_info["source_name"]
                uploaded_file_size = source_info["source_size"]

                request.session[f"pick:{pick_id}"] = {
                    "platform": platform,
                    "project_id": project_id,
                    "pick_dir": str(pick_dir),
                    "source_root": source_info["source_root"],
                    "source_kind": source_info["source_kind"],
                    "candidates": candidates,
                    "uploaded_file_name": uploaded_file_name,
                    "uploaded_file_size": uploaded_file_size,
                }

                return _render_upload_with_desktop_picker(
                    request,
                    form=form,
                    pick_id=pick_id,
                    project_id=project_id,
                    candidates=candidates,
                    selected_ids=[
                        item["id"]
                        for item in candidates
                    ],
                    uploaded_file_name=uploaded_file_name,
                    uploaded_file_size=uploaded_file_size,
                )

            _ensure_picker_root()
            _cleanup_old_picker_dirs()

            pick_id = str(uuid.uuid4())

            pick_dir = PICKER_ROOT / pick_id
            extracted_root = pick_dir / "extracted"
            zip_path = pick_dir / "solution.zip"

            pick_dir.mkdir(
                parents=True,
                exist_ok=True,
            )

            extracted_root.mkdir(
                parents=True,
                exist_ok=True,
            )

            uploaded_file = request.FILES["solution_zip"]

            uploaded_file_name = uploaded_file.name
            uploaded_file_size = uploaded_file.size

            try:
                save_upload(
                    uploaded_file,
                    str(zip_path),
                )

                extract_zip(
                    str(zip_path),
                    str(extracted_root),
                )

                json_files = find_json_files(
                    str(extracted_root),
                )

            except Exception as exc:
                shutil.rmtree(
                    pick_dir,
                    ignore_errors=True,
                )

                return render(
                    request,
                    "analyzer/upload.html",
                    {
                        "form": form,
                        "platform": platform,
                        "platform_label": _platform_label(platform),
                        "error": (
                            "Could not process ZIP file: "
                            f"{exc}"
                        ),
                    },
                )

            if not json_files:
                shutil.rmtree(
                    pick_dir,
                    ignore_errors=True,
                )

                return render(
                    request,
                    "analyzer/upload.html",
                    {
                        "form": form,
                        "platform": platform,
                        "platform_label": _platform_label(platform),
                        "error": (
                            "No JSON files were found "
                            "inside a Workflows folder."
                        ),
                    },
                )

            candidates = _build_json_candidates(
                json_files,
                str(extracted_root),
            )

            request.session[f"pick:{pick_id}"] = {
                "platform": platform,
                "project_id": project_id,
                "pick_dir": str(pick_dir),
                "extracted_root": str(extracted_root),
                "candidates": candidates,
                "uploaded_file_name": uploaded_file_name,
                "uploaded_file_size": uploaded_file_size,
            }

            return _render_upload_with_picker(
                request,
                form=form,
                pick_id=pick_id,
                project_id=project_id,
                platform=platform,
                candidates=candidates,
                selected_ids=[
                    item["id"]
                    for item in candidates
                ],
                uploaded_file_name=uploaded_file_name,
                uploaded_file_size=uploaded_file_size,
            )

    else:
        platform = PLATFORM_CLOUD

        form = UploadSolutionZipForm(
            initial={
                "platform": platform,
            }
        )

    return render(
        request,
        "analyzer/upload.html",
        {
            "form": form,
            "platform": platform,
            "platform_label": _platform_label(platform),
        },
    )



def select_jsons_view(request, pick_id: str):
    data = request.session.get(f"pick:{pick_id}")

    if not data:
        return redirect("upload")

    platform = data.get(
        "platform",
        PLATFORM_CLOUD,
    )

    if platform != PLATFORM_CLOUD:
        return redirect("upload")

    candidates = data.get("candidates", [])
    project_id = (
        request.POST.get("project_id")
        or data.get("project_id", "")
    ).strip()

    data["project_id"] = project_id
    request.session[f"pick:{pick_id}"] = data

    uploaded_file_name = data.get("uploaded_file_name", "")
    uploaded_file_size = data.get("uploaded_file_size", 0)

    if request.method != "POST":
        return _render_upload_with_picker(
            request,
            form=UploadSolutionZipForm(
                initial={
                    "platform": platform,
                    "project_id": project_id,
                }
            ),
            pick_id=pick_id,
            project_id=project_id,
            platform=platform,
            candidates=candidates,
            selected_ids=[item["id"] for item in candidates],
            uploaded_file_name=uploaded_file_name,
            uploaded_file_size=uploaded_file_size,
        )

    selected_ids = request.POST.getlist("selected_jsons")

    _debug_analysis("selected_ids:", selected_ids)
    _debug_analysis(
        "candidate_ids:",
        [item.get("id") for item in candidates],
    )

    if not selected_ids:
        return _render_upload_with_picker(
            request,
            form=UploadSolutionZipForm(
                initial={
                    "platform": platform,
                    "project_id": project_id,
                }
            ),
            pick_id=pick_id,
            project_id=project_id,
            platform=platform,
            candidates=candidates,
            selected_ids=[],
            picker_error="Select at least one flow to continue.",
            uploaded_file_name=uploaded_file_name,
            uploaded_file_size=uploaded_file_size,
        )

    candidate_map = {item["id"]: item for item in candidates}
    selected_items = [
        candidate_map[item_id]
        for item_id in selected_ids
        if item_id in candidate_map
    ]

    _debug_analysis(
        "selected_items:",
        [
            {
                "id": item.get("id"),
                "display_name": item.get("display_name"),
                "full_path": item.get("full_path"),
            }
            for item in selected_items
        ],
    )

    if not selected_items:
        return _render_upload_with_picker(
            request,
            form=UploadSolutionZipForm(
                initial={
                    "platform": platform,
                    "project_id": project_id,
                }
            ),
            pick_id=pick_id,
            project_id=project_id,
            platform=platform,
            candidates=candidates,
            selected_ids=[],
            picker_error=(
                "The selected flows are no longer valid. "
                "Please upload the ZIP again."
            ),
            uploaded_file_name=uploaded_file_name,
            uploaded_file_size=uploaded_file_size,
        )

    # Cada JSON seleccionado se analiza en una petición independiente.
    normalized_reviews: list[dict] = []
    processed_json_paths: list[str] = []
    processed_files: list[str] = []
    analysis_errors: list[str] = []

    aggregate_usage: dict = {}
    aggregate_usage_calls: list[dict] = []
    aggregate_cost = Decimal("0")
    aggregate_continuations = 0

    model_used = ""
    final_stop_reason = ""
    analysis_budget = _read_analysis_budget_usd()

    _debug_analysis("analysis_budget:", analysis_budget)
    _debug_analysis("selected_count:", len(selected_items))

    for item_index, item in enumerate(selected_items, start=1):
        json_path = item["full_path"]
        json_name = Path(json_path).name
        json_file_path = Path(json_path)

        _debug_analysis(
            f"starting item {item_index}/{len(selected_items)}:",
            {
                "json_name": json_name,
                "exists": json_file_path.exists(),
                "size_bytes": (
                    json_file_path.stat().st_size
                    if json_file_path.exists()
                    else None
                ),
                "path": str(json_file_path),
            },
        )

        # El presupuesto puede bloquear archivos futuros, pero no una llamada
        # que ya comenzó.
        if (
            analysis_budget is not None
            and aggregate_cost >= analysis_budget
        ):
            analysis_errors.append(
                "Análisis parcial: se alcanzó el presupuesto estimado "
                f"de ${analysis_budget:.2f} USD antes de procesar "
                f"{json_name}."
            )
            break

        try:
            claude_result = run_cloud_review(
                json_files=[json_path],
                project_id=project_id,
            )
            normalized_review = normalize_review_payload(
                claude_result["review_json"]
            )

            _debug_analysis(
                f"finished {json_name}:",
                {
                    "cost": claude_result.get("estimated_token_cost_usd"),
                    "tokens": claude_result.get("tokens_used"),
                    "stop_reason": claude_result.get("stop_reason"),
                    "continuations": claude_result.get("continuations"),
                    "findings": len(normalized_review.get("findings", [])),
                },
            )
        except ClaudeReviewError as exc:
            _debug_analysis(
                f"ClaudeReviewError while processing {json_name}:",
                repr(exc),
            )
            analysis_errors.append(
                f"{json_name}: Claude review failed: {exc}"
            )
            break
        except Exception as exc:
            import traceback

            _debug_analysis(
                f"Unexpected error while processing {json_name}:",
                repr(exc),
            )
            traceback.print_exc()
            analysis_errors.append(
                f"{json_name}: unexpected analysis error: {exc}"
            )
            break

        normalized_reviews.append(normalized_review)
        processed_json_paths.append(json_path)
        processed_files.append(json_name)

        current_usage = claude_result.get("usage") or {}
        _merge_usage_totals(aggregate_usage, current_usage)

        _append_usage_calls(
            aggregate_usage_calls,
            claude_result.get("usage_calls") or [],
            source_file=json_name,
        )

        current_cost = claude_result.get("estimated_token_cost_usd")
        if current_cost is not None:
            aggregate_cost += Decimal(str(current_cost))

        aggregate_continuations += int(
            claude_result.get("continuations", 0) or 0
        )

        model_used = claude_result.get("model") or model_used
        final_stop_reason = (
            claude_result.get("stop_reason") or final_stop_reason
        )

        if (
            analysis_budget is not None
            and aggregate_cost >= analysis_budget
            and item_index < len(selected_items)
        ):
            analysis_errors.append(
                "Análisis parcial: después de procesar "
                f"{json_name}, el costo estimado acumulado llegó a "
                f"${aggregate_cost:.6f} USD. No se iniciaron los "
                "JSON restantes."
            )
            break

    if not normalized_reviews:
        error_message = (
            analysis_errors[0]
            if analysis_errors
            else "No selected JSON could be analyzed."
        )

        _debug_analysis("analysis stopped before any result:", error_message)

        return _render_upload_with_picker(
            request,
            form=UploadSolutionZipForm(
                initial={
                    "platform": platform,
                    "project_id": project_id,
                }
            ),
            pick_id=pick_id,
            project_id=project_id,
            platform=platform,
            candidates=candidates,
            selected_ids=selected_ids,
            picker_error=error_message,
            uploaded_file_name=uploaded_file_name,
            uploaded_file_size=uploaded_file_size,
        )

    normalized = merge_normalized_reviews(
        normalized_reviews,
        project_id=project_id,
    )
    normalized["errors"].extend(analysis_errors)

    findings = normalized["findings"]
    findings_sorted = sorted(
        findings,
        key=lambda finding: (
            -int(finding.get("severity_level", 0) or 0),
            _rule_order(finding.get("rule_name", "")),
            str(finding.get("flow_name", "")).lower(),
            str(finding.get("target_pretty", "")).lower(),
        ),
    )

    # Las métricas se calculan solo para los JSON que sí terminaron.
    flow_metrics = collect_flow_metrics(processed_json_paths)
    total_actions = int(flow_metrics.get("total_actions", 0) or 0)

    health = _calculate_action_health(
        total_actions=total_actions,
        findings=findings_sorted,
    )

    run_id = str(uuid.uuid4())
    analysis_complete = len(processed_files) == len(selected_items)

    request.session[f"run:{run_id}"] = {
        "platform": platform,
        "project_id": project_id,
        "findings": findings_sorted[:1000],
        "summary": normalized.get("summary", {}),
        "review_scope": normalized.get("review_scope", {}),
        "errors": normalized.get("errors", []),
        "schema_version": normalized.get("schema_version", ""),

        "total_json": len(processed_files),
        "selected_json_count": len(selected_items),
        "processed_json_count": len(processed_files),
        "processed_files": processed_files,
        "analysis_complete": analysis_complete,

        "total_flows": int(flow_metrics.get("total_flows", 0) or 0),
        "total_actions": total_actions,
        "flagged_actions_count": health["flagged_actions_count"],
        "passed_actions_count": health["passed_actions_count"],
        "passed_actions_pct": health["passed_actions_pct"],

        "model_used": model_used,
        "usage": aggregate_usage,
        "usage_calls": aggregate_usage_calls,
        "tokens_used": _total_tokens_from_usage(aggregate_usage),
        "estimated_token_cost_usd": float(aggregate_cost),
        "stop_reason": final_stop_reason,
        "continuations": aggregate_continuations,
    }

    pick_dir = data.get("pick_dir")
    if pick_dir:
        shutil.rmtree(pick_dir, ignore_errors=True)

    request.session.pop(f"pick:{pick_id}", None)

    _debug_analysis(
        "analysis completed:",
        {
            "run_id": run_id,
            "processed_files": processed_files,
            "analysis_complete": analysis_complete,
            "aggregate_cost": float(aggregate_cost),
            "errors": analysis_errors,
        },
    )

    return redirect("result", run_id=run_id)


def select_desktop_files_view(request, pick_id: str):
    data = request.session.get(f"pick:{pick_id}")

    if not data:
        return redirect("upload")

    platform = data.get(
        "platform",
        PLATFORM_CLOUD,
    )

    if platform != PLATFORM_DESKTOP:
        return redirect("upload")

    candidates = data.get("candidates", [])
    project_id = (
        request.POST.get("project_id")
        or data.get("project_id", "")
    ).strip()

    data["project_id"] = project_id
    request.session[f"pick:{pick_id}"] = data

    uploaded_file_name = data.get("uploaded_file_name", "")
    uploaded_file_size = data.get("uploaded_file_size", 0)

    if request.method != "POST":
        return _render_upload_with_desktop_picker(
            request,
            form=UploadSolutionZipForm(
                initial={
                    "platform": platform,
                    "project_id": project_id,
                }
            ),
            pick_id=pick_id,
            project_id=project_id,
            candidates=candidates,
            selected_ids=[item["id"] for item in candidates],
            uploaded_file_name=uploaded_file_name,
            uploaded_file_size=uploaded_file_size,
        )

    selected_ids = request.POST.getlist("selected_desktop_files")

    if not selected_ids:
        return _render_upload_with_desktop_picker(
            request,
            form=UploadSolutionZipForm(
                initial={
                    "platform": platform,
                    "project_id": project_id,
                }
            ),
            pick_id=pick_id,
            project_id=project_id,
            candidates=candidates,
            selected_ids=[],
            picker_error="Select at least one subflow to continue.",
            uploaded_file_name=uploaded_file_name,
            uploaded_file_size=uploaded_file_size,
        )

    candidate_map = {item["id"]: item for item in candidates}
    selected_items = [
        candidate_map[item_id]
        for item_id in selected_ids
        if item_id in candidate_map
    ]

    if not selected_items:
        return _render_upload_with_desktop_picker(
            request,
            form=UploadSolutionZipForm(
                initial={
                    "platform": platform,
                    "project_id": project_id,
                }
            ),
            pick_id=pick_id,
            project_id=project_id,
            candidates=candidates,
            selected_ids=[],
            picker_error=(
                "The selected subflows are no longer valid. "
                "Please upload the Desktop source again."
            ),
            uploaded_file_name=uploaded_file_name,
            uploaded_file_size=uploaded_file_size,
        )

    normalized_reviews: list[dict] = []
    processed_txt_paths: list[str] = []
    processed_files: list[str] = []
    analysis_errors: list[str] = []

    aggregate_usage: dict = {}
    aggregate_usage_calls: list[dict] = []
    aggregate_cost = Decimal("0")
    aggregate_continuations = 0
    estimated_input_tokens_total = 0

    model_used = ""
    final_stop_reason = ""

    raw_budget = (
        os.getenv("ANTHROPIC_DESKTOP_MAX_RUN_COST_USD", "1.00")
        or ""
    ).strip()

    try:
        analysis_budget = (
            Decimal(raw_budget)
            if raw_budget
            else None
        )
    except InvalidOperation:
        analysis_budget = Decimal("1.00")

    if analysis_budget is not None and analysis_budget <= 0:
        analysis_budget = None

    for item_index, item in enumerate(selected_items, start=1):
        txt_path = item["full_path"]
        txt_name = Path(txt_path).name

        if (
            analysis_budget is not None
            and aggregate_cost >= analysis_budget
        ):
            analysis_errors.append(
                "Análisis Desktop parcial: se alcanzó el presupuesto "
                f"estimado de ${analysis_budget:.2f} USD antes de procesar "
                f"{txt_name}."
            )
            break

        try:
            claude_result = run_desktop_review(
                txt_files=[txt_path],
                project_id=project_id,
            )
            normalized_review = normalize_review_payload(
                claude_result["review_json"]
            )
        except ClaudeReviewError as exc:
            analysis_errors.append(
                f"{txt_name}: Desktop AI review failed: {exc}"
            )
            break
        except Exception as exc:
            analysis_errors.append(
                f"{txt_name}: unexpected Desktop analysis error: {exc}"
            )
            break

        normalized_reviews.append(normalized_review)
        processed_txt_paths.append(txt_path)
        processed_files.append(txt_name)

        current_usage = claude_result.get("usage") or {}
        _merge_usage_totals(
            aggregate_usage,
            current_usage,
        )

        _append_usage_calls(
            aggregate_usage_calls,
            claude_result.get("usage_calls") or [],
            source_file=txt_name,
        )

        estimated_input_tokens_total += int(
            claude_result.get("estimated_input_tokens", 0)
            or 0
        )

        current_cost = claude_result.get(
            "estimated_token_cost_usd"
        )
        if current_cost is not None:
            aggregate_cost += Decimal(str(current_cost))

        aggregate_continuations += int(
            claude_result.get("continuations", 0)
            or 0
        )

        model_used = claude_result.get("model") or model_used
        final_stop_reason = (
            claude_result.get("stop_reason")
            or final_stop_reason
        )

        if (
            analysis_budget is not None
            and aggregate_cost >= analysis_budget
            and item_index < len(selected_items)
        ):
            analysis_errors.append(
                "Análisis Desktop parcial: después de procesar "
                f"{txt_name}, el costo estimado acumulado llegó a "
                f"${aggregate_cost:.6f} USD. No se iniciaron los TXT "
                "restantes."
            )
            break

    if not normalized_reviews:
        error_message = (
            analysis_errors[0]
            if analysis_errors
            else "No selected Desktop TXT could be analyzed."
        )

        return _render_upload_with_desktop_picker(
            request,
            form=UploadSolutionZipForm(
                initial={
                    "platform": platform,
                    "project_id": project_id,
                }
            ),
            pick_id=pick_id,
            project_id=project_id,
            candidates=candidates,
            selected_ids=selected_ids,
            picker_error=error_message,
            uploaded_file_name=uploaded_file_name,
            uploaded_file_size=uploaded_file_size,
        )

    normalized = merge_normalized_reviews(
        normalized_reviews,
        project_id=project_id,
    )
    normalized["errors"].extend(analysis_errors)

    findings = normalized["findings"]
    findings_sorted = sorted(
        findings,
        key=lambda finding: (
            -int(finding.get("severity_level", 0) or 0),
            _rule_order(finding.get("rule_name", "")),
            str(finding.get("flow_name", "")).lower(),
            str(finding.get("target_pretty", "")).lower(),
        ),
    )

    desktop_metrics = collect_desktop_metrics(
        processed_txt_paths
    )
    total_actions = int(
        desktop_metrics.get("total_actions", 0)
        or 0
    )

    health = _calculate_action_health(
        total_actions=total_actions,
        findings=findings_sorted,
    )

    run_id = str(uuid.uuid4())
    analysis_complete = len(processed_files) == len(selected_items)

    request.session[f"run:{run_id}"] = {
        "platform": platform,
        "project_id": project_id,
        "findings": findings_sorted[:1000],
        "summary": normalized.get("summary", {}),
        "review_scope": normalized.get("review_scope", {}),
        "errors": normalized.get("errors", []),
        "schema_version": normalized.get("schema_version", ""),

        "total_json": len(processed_files),
        "selected_json_count": len(selected_items),
        "processed_json_count": len(processed_files),
        "processed_files": processed_files,
        "analysis_complete": analysis_complete,

        "total_flows": int(
            desktop_metrics.get("total_flows", 0)
            or 0
        ),
        "total_actions": total_actions,
        "flagged_actions_count": health["flagged_actions_count"],
        "passed_actions_count": health["passed_actions_count"],
        "passed_actions_pct": health["passed_actions_pct"],

        "model_used": model_used,
        "usage": aggregate_usage,
        "usage_calls": aggregate_usage_calls,
        "tokens_used": _total_tokens_from_usage(aggregate_usage),
        "estimated_input_tokens": estimated_input_tokens_total,
        "estimated_token_cost_usd": float(aggregate_cost),
        "stop_reason": final_stop_reason,
        "continuations": aggregate_continuations,
    }

    pick_dir = data.get("pick_dir")
    if pick_dir:
        shutil.rmtree(
            pick_dir,
            ignore_errors=True,
        )

    request.session.pop(
        f"pick:{pick_id}",
        None,
    )

    return redirect(
        "result",
        run_id=run_id,
    )


def result_view(request, run_id: str):
    data = request.session.get(f"run:{run_id}")

    if not data:
        return render(
            request,
            "analyzer/result.html",
            {
                "run_id": run_id,
                "error": "No results for this run_id.",
            },
        )

    findings = data.get("findings", [])
    counts = Counter([(finding.get("rule_name") or "Unknown") for finding in findings])

    rule_rows = sorted(
        counts.items(),
        key=lambda item: (
            -_rule_severity(item[0]),
            -item[1],
            _rule_order(item[0]),
            item[0].lower(),
        ),
    )

    passed_actions_pct = _safe_pct(data.get("passed_actions_pct", 0))
    total_findings = len(findings)

    status = _build_analysis_status(passed_actions_pct, total_findings)
    compliance_core = _build_compliance_core(passed_actions_pct, total_findings)

    estimated_cost = data.get("estimated_token_cost_usd")
    if estimated_cost is None:
        estimated_cost_display = "N/A"
    else:
        estimated_cost_display = f"${float(estimated_cost):.6f}"

    return render(
        request,
        "analyzer/result.html",
        {
            "run_id": run_id,
            "project_id": data.get("project_id", ""),
            "findings": findings,
            "rule_rows": rule_rows,
            "analysis_errors": data.get("errors", []),

            "total_json": data.get("total_json", 0),
            "total_flows": data.get("total_flows", 0),
            "total_actions": data.get("total_actions", 0),
            "flagged_actions_count": data.get("flagged_actions_count", 0),
            "passed_actions_count": data.get("passed_actions_count", 0),
            "passed_actions_pct": passed_actions_pct,
            "total_findings": total_findings,

            "model_used": data.get("model_used", ""),
            "tokens_used": data.get("tokens_used", 0),
            "estimated_token_cost_usd": data.get("estimated_token_cost_usd"),
            "estimated_token_cost_display": estimated_cost_display,

            "status_label": status["label"],
            "status_variant": status["variant"],
            "is_rejected": status["is_rejected"],

            "compliance_theme": compliance_core["theme"],
            "compliance_tier": compliance_core["tier"],
            "compliance_helper": compliance_core["helper"],
            "compliance_display_pct": compliance_core["display_pct"],
            "compliance_segments": compliance_core["segments"],
            "compliance_segments_filled": compliance_core["filled_segments"],
            "platform": data.get(
    "platform",
    PLATFORM_CLOUD,
),
"platform_label": _platform_label(
    data.get(
        "platform",
        PLATFORM_CLOUD,
    )
),
            
        },
    )


def download_excel(request, run_id: str):
    data = request.session.get(f"run:{run_id}")

    if not data:
        raise Http404("No results for this run_id.")

    buffer = BytesIO()

    export_review_to_xlsx(
        buffer,
        findings=data.get("findings", []),
        project_id=data.get("project_id", ""),
        usage_data={
            "model": data.get("model_used", ""),
            "usage": data.get("usage", {}),
            "tokens_used": data.get("tokens_used", 0),
            "estimated_token_cost_usd": data.get("estimated_token_cost_usd"),
            "stop_reason": data.get("stop_reason", ""),
            "continuations": data.get("continuations", 0),
        },
    )

    buffer.seek(0)

    project_id = (data.get("project_id") or "SIN_ID").strip()
    project_id = project_id.replace(" ", "_").replace("/", "-")
    safe_project_id = re.sub(r"[^A-Za-z0-9_.-]", "", project_id)

    return FileResponse(
        buffer,
        as_attachment=True,
        filename=f"reporte_{safe_project_id}.xlsx",
    )