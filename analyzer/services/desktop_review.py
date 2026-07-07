from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Any

from anthropic import Anthropic
from dotenv import load_dotenv

from .claude_review import (
    BETAS,
    CODE_EXECUTION_TOOL,
    DEFAULT_TIMEOUT_SECONDS,
    ClaudeReviewError,
    estimate_token_cost_usd,
    extract_text_from_response,
    get_decimal_env,
    get_float_env,
    get_int_env,
    get_required_env,
    parse_json_from_text,
    record_call_usage,
    total_tokens_used,
    write_execution_log,
)


DESKTOP_SCHEMA_VERSION = "pauto_desktop_rows_v1"
DEFAULT_DESKTOP_MAX_TOKENS = 2048
DEFAULT_DESKTOP_MAX_INPUT_TOKENS_PER_REQUEST = 25000


def upload_txt_file(client: Anthropic, txt_path: str) -> str:
    path = Path(txt_path)

    if not path.exists() or not path.is_file():
        raise ClaudeReviewError(
            f"No existe el TXT Desktop seleccionado: {path}"
        )

    if path.suffix.lower() != ".txt":
        raise ClaudeReviewError(
            f"El archivo Desktop seleccionado no es TXT: {path.name}"
        )

    try:
        with path.open("rb") as file_obj:
            try:
                uploaded_file = client.beta.files.upload(
                    file=(path.name, file_obj, "text/plain"),
                )
            except TypeError:
                # Compatibilidad de firma entre versiones del SDK.
                # No es un reintento de análisis ni genera una llamada al modelo.
                file_obj.seek(0)
                uploaded_file = client.beta.files.upload(
                    file=(path.name, file_obj, "text/plain"),
                    betas=["files-api-2025-04-14"],
                )
    except Exception as exc:
        raise ClaudeReviewError(
            "No se pudo subir el TXT Desktop a Claude. Valida la API key, "
            "el workspace y la versión del SDK anthropic."
        ) from exc

    return uploaded_file.id


def build_desktop_prompt(
    project_id: str,
    selected_files: list[str],
) -> str:
    file_list = "\n".join(
        f"- {name}"
        for name in selected_files
    )

    return f"""
Use the pauto-desktop-review-json skill to review only the attached Power Automate Desktop TXT subflow files.

Project ID: {project_id or ""}

Selected files:
{file_list}

This is an incremental validation run. Apply only the active rules declared by the Desktop skill.
Return only valid JSON using the {DESKTOP_SCHEMA_VERSION} schema.
Do not generate .xlsx, .csv, Markdown, prose, or explanations outside the JSON.
Do not include full sensitive values in the response.
""".strip()


def build_desktop_initial_messages(
    *,
    project_id: str,
    uploaded_txt_files: list[dict[str, str]],
) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": build_desktop_prompt(
                project_id=project_id,
                selected_files=[
                    item["filename"]
                    for item in uploaded_txt_files
                ],
            ),
        }
    ]

    for item in uploaded_txt_files:
        content.append(
            {
                "type": "container_upload",
                "file_id": item["file_id"],
            }
        )

    return [
        {
            "role": "user",
            "content": content,
        }
    ]


def _desktop_container(
    *,
    skill_id: str,
    skill_version: str,
) -> dict[str, Any]:
    return {
        "skills": [
            {
                "type": "custom",
                "skill_id": skill_id,
                "version": skill_version,
            },
        ]
    }


def count_desktop_input_tokens(
    *,
    client: Anthropic,
    model: str,
    skill_id: str,
    skill_version: str,
    messages: list[dict[str, Any]],
) -> int:
    try:
        response = client.beta.messages.count_tokens(
            model=model,
            betas=BETAS,
            container=_desktop_container(
                skill_id=skill_id,
                skill_version=skill_version,
            ),
            messages=messages,
            tools=[CODE_EXECUTION_TOOL],
        )
    except Exception as exc:
        raise ClaudeReviewError(
            "No se pudo contar los tokens de entrada Desktop antes del "
            "análisis. No se inició la revisión para evitar un consumo no "
            "estimado."
        ) from exc

    return int(getattr(response, "input_tokens", 0) or 0)


def create_desktop_message(
    *,
    client: Anthropic,
    model: str,
    skill_id: str,
    skill_version: str,
    max_tokens: int,
    messages: list[dict[str, Any]],
):
    return client.beta.messages.create(
        model=model,
        max_tokens=max_tokens,
        betas=BETAS,
        container=_desktop_container(
            skill_id=skill_id,
            skill_version=skill_version,
        ),
        messages=messages,
        tools=[CODE_EXECUTION_TOOL],
    )


def extract_complete_desktop_review_json(
    response: Any,
) -> dict[str, Any] | None:
    raw_text = extract_text_from_response(response)

    if not raw_text:
        return None

    try:
        payload = parse_json_from_text(raw_text)
    except ClaudeReviewError:
        return None

    if payload.get("schema_version") != DESKTOP_SCHEMA_VERSION:
        return None

    if not isinstance(payload.get("detail_rows"), list):
        return None

    errors = payload.get("errors")
    if errors is not None and not isinstance(errors, list):
        return None

    return payload


def _delete_uploaded_files(
    client: Anthropic,
    file_ids: list[str],
) -> None:
    for file_id in file_ids:
        try:
            client.beta.files.delete(file_id=file_id)
        except Exception:
            # La limpieza remota no debe ocultar el resultado de una revisión
            # ya completada ni sustituir el error original de la ejecución.
            continue


def run_desktop_review(
    *,
    txt_files: list[str],
    project_id: str,
) -> dict[str, Any]:
    load_dotenv()

    if not txt_files:
        raise ClaudeReviewError(
            "No hay TXT Desktop seleccionados para revisar."
        )

    api_key = get_required_env("ANTHROPIC_API_KEY")
    model = get_required_env("ANTHROPIC_MODEL")
    skill_id = get_required_env("ANTHROPIC_DESKTOP_SKILL_ID")
    skill_version = get_required_env(
        "ANTHROPIC_DESKTOP_SKILL_VERSION"
    )

    max_tokens = get_int_env(
        "ANTHROPIC_DESKTOP_MAX_TOKENS",
        DEFAULT_DESKTOP_MAX_TOKENS,
    )
    max_input_tokens_per_request = get_int_env(
        "ANTHROPIC_DESKTOP_MAX_INPUT_TOKENS_PER_REQUEST",
        DEFAULT_DESKTOP_MAX_INPUT_TOKENS_PER_REQUEST,
    )
    timeout_seconds = get_float_env(
        "ANTHROPIC_TIMEOUT_SECONDS",
        DEFAULT_TIMEOUT_SECONDS,
    )

    if max_tokens <= 0:
        raise ClaudeReviewError(
            "ANTHROPIC_DESKTOP_MAX_TOKENS debe ser mayor que 0."
        )

    if max_input_tokens_per_request <= 0:
        raise ClaudeReviewError(
            "ANTHROPIC_DESKTOP_MAX_INPUT_TOKENS_PER_REQUEST debe ser "
            "mayor que 0."
        )

    # Sin reintentos automáticos del SDK.
    client = Anthropic(
        api_key=api_key,
        timeout=timeout_seconds,
        max_retries=0,
    )

    uploaded_txt_files: list[dict[str, str]] = []
    uploaded_file_ids: list[str] = []
    selected_files: list[str] = []
    total_usage: dict[str, Any] = {}
    usage_calls: list[dict[str, Any]] = []
    log_written = False

    try:
        for txt_file in txt_files:
            path = Path(txt_file)
            file_id = upload_txt_file(client, txt_file)
            uploaded_file_ids.append(file_id)

            uploaded_txt_files.append(
                {
                    "file_id": file_id,
                    "filename": path.name,
                }
            )

        selected_files = [
            item["filename"]
            for item in uploaded_txt_files
        ]

        messages = build_desktop_initial_messages(
            project_id=project_id,
            uploaded_txt_files=uploaded_txt_files,
        )

        estimated_input_tokens = count_desktop_input_tokens(
            client=client,
            model=model,
            skill_id=skill_id,
            skill_version=skill_version,
            messages=messages,
        )

        if estimated_input_tokens > max_input_tokens_per_request:
            error_message = (
                "La petición Desktop requiere aproximadamente "
                f"{estimated_input_tokens:,} tokens de entrada y supera el "
                "límite preventivo de "
                f"{max_input_tokens_per_request:,}. No se inició el análisis."
            )

            write_execution_log(
                model=model,
                project_id=project_id,
                selected_files=selected_files,
                usage=total_usage,
                usage_calls=usage_calls,
                estimated_token_cost_usd=None,
                status="desktop_input_token_limit_reached",
                error=error_message,
            )
            log_written = True

            raise ClaudeReviewError(error_message)

        response = create_desktop_message(
            client=client,
            model=model,
            skill_id=skill_id,
            skill_version=skill_version,
            max_tokens=max_tokens,
            messages=messages,
        )

        record_call_usage(
            total_usage=total_usage,
            usage_calls=usage_calls,
            response=response,
        )

        # Política Desktop: cero continuaciones.
        # pause_turn puede indicar que el trabajo no terminó. No se hace una
        # segunda llamada pagada; se detiene y se registra el motivo.
        completed_review_json: dict[str, Any] | None = None

        if getattr(response, "stop_reason", None) == "pause_turn":
            completed_review_json = extract_complete_desktop_review_json(
                response
            )

            if completed_review_json is None:
                current_cost = estimate_token_cost_usd(
                    model,
                    total_usage,
                )
                error_message = (
                    "Claude devolvió pause_turn. La política Desktop bloquea "
                    "continuaciones para evitar una segunda llamada pagada."
                )

                write_execution_log(
                    model=model,
                    project_id=project_id,
                    selected_files=selected_files,
                    usage=total_usage,
                    usage_calls=usage_calls,
                    estimated_token_cost_usd=current_cost,
                    status="continuation_limit_reached",
                    error=error_message,
                )
                log_written = True

                raise ClaudeReviewError(error_message)

        raw_text = extract_text_from_response(response)

        if completed_review_json is not None:
            review_json = completed_review_json
        else:
            if getattr(response, "stop_reason", None) == "max_tokens":
                current_cost = estimate_token_cost_usd(
                    model,
                    total_usage,
                )
                error_message = (
                    "Claude alcanzó max_tokens antes de devolver el JSON "
                    "Desktop final. No se realizó otro intento."
                )

                write_execution_log(
                    model=model,
                    project_id=project_id,
                    selected_files=selected_files,
                    usage=total_usage,
                    usage_calls=usage_calls,
                    estimated_token_cost_usd=current_cost,
                    status="max_tokens_reached",
                    error=error_message,
                )
                log_written = True

                raise ClaudeReviewError(error_message)

            try:
                review_json = parse_json_from_text(raw_text)
            except ClaudeReviewError as exc:
                current_cost = estimate_token_cost_usd(
                    model,
                    total_usage,
                )

                write_execution_log(
                    model=model,
                    project_id=project_id,
                    selected_files=selected_files,
                    usage=total_usage,
                    usage_calls=usage_calls,
                    estimated_token_cost_usd=current_cost,
                    status="invalid_json",
                    error=str(exc),
                )
                log_written = True
                raise

        if review_json.get("schema_version") != DESKTOP_SCHEMA_VERSION:
            current_cost = estimate_token_cost_usd(
                model,
                total_usage,
            )
            error_message = (
                "Claude devolvió un schema Desktop inesperado. Esperado: "
                f"{DESKTOP_SCHEMA_VERSION}."
            )

            write_execution_log(
                model=model,
                project_id=project_id,
                selected_files=selected_files,
                usage=total_usage,
                usage_calls=usage_calls,
                estimated_token_cost_usd=current_cost,
                status="invalid_schema",
                error=error_message,
            )
            log_written = True

            raise ClaudeReviewError(error_message)

        if not isinstance(review_json.get("detail_rows"), list):
            current_cost = estimate_token_cost_usd(
                model,
                total_usage,
            )
            error_message = (
                "El JSON Desktop no contiene detail_rows como lista."
            )

            write_execution_log(
                model=model,
                project_id=project_id,
                selected_files=selected_files,
                usage=total_usage,
                usage_calls=usage_calls,
                estimated_token_cost_usd=current_cost,
                status="invalid_detail_rows",
                error=error_message,
            )
            log_written = True

            raise ClaudeReviewError(error_message)

        estimated_token_cost_usd = estimate_token_cost_usd(
            model,
            total_usage,
        )

        write_execution_log(
            model=model,
            project_id=project_id,
            selected_files=selected_files,
            usage=total_usage,
            usage_calls=usage_calls,
            estimated_token_cost_usd=estimated_token_cost_usd,
            status="success",
            error=None,
        )
        log_written = True

        return {
            "review_json": review_json,
            "raw_text": raw_text,
            "model": model,
            "skill_id": skill_id,
            "skill_version": skill_version,
            "usage": total_usage,
            "usage_calls": usage_calls,
            "tokens_used": total_tokens_used(total_usage),
            "estimated_input_tokens": estimated_input_tokens,
            "estimated_token_cost_usd": (
                float(estimated_token_cost_usd)
                if estimated_token_cost_usd is not None
                else None
            ),
            "stop_reason": getattr(response, "stop_reason", None),
            "continuations": 0,
        }

    except ClaudeReviewError as exc:
        if not log_written:
            current_cost = estimate_token_cost_usd(
                model,
                total_usage,
            )

            write_execution_log(
                model=model,
                project_id=project_id,
                selected_files=selected_files,
                usage=total_usage,
                usage_calls=usage_calls,
                estimated_token_cost_usd=current_cost,
                status="desktop_review_error",
                error=str(exc),
            )

        raise

    except Exception as exc:
        if not log_written:
            current_cost = estimate_token_cost_usd(
                model,
                total_usage,
            )

            write_execution_log(
                model=model,
                project_id=project_id,
                selected_files=selected_files,
                usage=total_usage,
                usage_calls=usage_calls,
                estimated_token_cost_usd=current_cost,
                status="desktop_unexpected_error",
                error=str(exc),
            )

        raise ClaudeReviewError(
            f"Error inesperado durante la revisión Desktop: {exc}"
        ) from exc

    finally:
        _delete_uploaded_files(
            client,
            uploaded_file_ids,
        )
