from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
import re

from anthropic import Anthropic
from django.conf import settings
from dotenv import load_dotenv


FILES_BETA = "files-api-2025-04-14"
CODE_EXECUTION_BETA = "code-execution-2025-08-25"
SKILLS_BETA = "skills-2025-10-02"

BETAS: list[str] = [
    FILES_BETA,
    CODE_EXECUTION_BETA,
    SKILLS_BETA,
]

CODE_EXECUTION_TOOL: dict[str, str] = {
    "type": "code_execution_20250825",
    "name": "code_execution",
}

DEFAULT_MAX_TOKENS = 4096
DEFAULT_MAX_CONTINUATIONS = 1
DEFAULT_MAX_ESTIMATED_COST_USD = Decimal("1.50")
DEFAULT_TIMEOUT_SECONDS = 240.0

DEFAULT_EXECUTION_LOG_MODE = "errors"

VALID_EXECUTION_LOG_MODES = {
    "off",
    "errors",
    "all",
}



MODEL_PRICING_USD_PER_MTOK: dict[str, dict[str, Decimal]] = {
    "claude-haiku-4-5-20251001": {
        "input": Decimal("1.00"),
        "output": Decimal("5.00"),
        "cache_write_5m": Decimal("1.25"),
        "cache_read": Decimal("0.10"),
    },
    "claude-haiku-4-5": {
        "input": Decimal("1.00"),
        "output": Decimal("5.00"),
        "cache_write_5m": Decimal("1.25"),
        "cache_read": Decimal("0.10"),
    },
    "claude-sonnet-4-6": {
        "input": Decimal("3.00"),
        "output": Decimal("15.00"),
        "cache_write_5m": Decimal("3.75"),
        "cache_read": Decimal("0.30"),
    },
}


class ClaudeReviewError(RuntimeError):
    pass


def get_required_env(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise ClaudeReviewError(f"Falta la variable de entorno: {name}")
    return value


def get_int_env(name: str, default: int) -> int:
    raw_value = (os.getenv(name) or "").strip()

    if not raw_value:
        return default

    try:
        return int(raw_value)
    except ValueError as exc:
        raise ClaudeReviewError(
            f"La variable {name} debe ser numérica. Valor recibido: {raw_value}"
        ) from exc



def get_float_env(name: str, default: float) -> float:
    raw_value = (os.getenv(name) or "").strip()

    if not raw_value:
        return default

    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ClaudeReviewError(
            f"La variable {name} debe contener un número válido. "
            f"Valor recibido: {raw_value}"
        ) from exc

    if value <= 0:
        raise ClaudeReviewError(f"La variable {name} debe ser mayor que 0.")

    return value


def get_decimal_env(name: str, default: Decimal) -> Decimal:
    raw_value = (os.getenv(name) or "").strip()

    if not raw_value:
        return default

    try:
        value = Decimal(raw_value)
    except InvalidOperation as exc:
        raise ClaudeReviewError(
            f"La variable {name} debe contener un decimal válido. "
            f"Valor recibido: {raw_value}"
        ) from exc

    if value <= 0:
        raise ClaudeReviewError(f"La variable {name} debe ser mayor que 0.")

    return value



def get_execution_log_mode() -> str:
    mode = (
        os.getenv("ANTHROPIC_EXECUTION_LOG_MODE")
        or DEFAULT_EXECUTION_LOG_MODE
    ).strip().lower()

    if mode not in VALID_EXECUTION_LOG_MODES:
        allowed_values = ", ".join(
            sorted(VALID_EXECUTION_LOG_MODES)
        )

        raise ClaudeReviewError(
            "La variable ANTHROPIC_EXECUTION_LOG_MODE debe ser "
            f"uno de estos valores: {allowed_values}. "
            f"Valor recibido: {mode}"
        )

    return mode



def usage_to_dict(response: Any) -> dict[str, Any]:
    usage = getattr(response, "usage", None)

    if usage is None:
        return {}

    if hasattr(usage, "model_dump"):
        return usage.model_dump()

    if hasattr(usage, "dict"):
        return usage.dict()

    if isinstance(usage, dict):
        return usage

    return {}


def merge_usage(total: dict[str, Any], usage: dict[str, Any]) -> dict[str, Any]:
    numeric_fields = [
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ]

    for field in numeric_fields:
        total[field] = int(total.get(field, 0) or 0) + int(
            usage.get(field, 0) or 0
        )

    server_tool_use = usage.get("server_tool_use") or {}
    total_server_tool_use = total.setdefault("server_tool_use", {})

    if isinstance(server_tool_use, dict):
        for key, value in server_tool_use.items():
            if isinstance(value, int):
                total_server_tool_use[key] = (
                    int(total_server_tool_use.get(key, 0) or 0) + value
                )

    return total


def record_call_usage(
    *,
    total_usage: dict[str, Any],
    usage_calls: list[dict[str, Any]],
    response: Any,
) -> None:
    usage = usage_to_dict(response)
    merge_usage(total_usage, usage)

    server_tool_use = usage.get("server_tool_use") or {}
    call_index = len(usage_calls) + 1

    usage_calls.append(
        {
            "call_index": call_index,
            "call_type": "initial" if call_index == 1 else "continuation",
            "stop_reason": getattr(response, "stop_reason", None),
            "input_tokens": int(usage.get("input_tokens", 0) or 0),
            "output_tokens": int(usage.get("output_tokens", 0) or 0),
            "cache_creation_input_tokens": int(
                usage.get("cache_creation_input_tokens", 0) or 0
            ),
            "cache_read_input_tokens": int(
                usage.get("cache_read_input_tokens", 0) or 0
            ),
            "code_execution_requests": int(
                server_tool_use.get("code_execution_requests", 0) or 0
            ),
        }
    )


def estimate_token_cost_usd(
    model: str,
    usage: dict[str, Any],
) -> Decimal | None:
    pricing = MODEL_PRICING_USD_PER_MTOK.get(model)

    if pricing is None:
        return None

    input_tokens = Decimal(int(usage.get("input_tokens", 0) or 0))
    output_tokens = Decimal(int(usage.get("output_tokens", 0) or 0))
    cache_creation_tokens = Decimal(
        int(usage.get("cache_creation_input_tokens", 0) or 0)
    )
    cache_read_tokens = Decimal(
        int(usage.get("cache_read_input_tokens", 0) or 0)
    )

    one_million = Decimal("1000000")

    cost = Decimal("0")
    cost += (input_tokens / one_million) * pricing["input"]
    cost += (output_tokens / one_million) * pricing["output"]
    cost += (cache_creation_tokens / one_million) * pricing["cache_write_5m"]
    cost += (cache_read_tokens / one_million) * pricing["cache_read"]

    return cost


def total_tokens_used(usage: dict[str, Any]) -> int:
    return (
        int(usage.get("input_tokens", 0) or 0)
        + int(usage.get("output_tokens", 0) or 0)
        + int(usage.get("cache_creation_input_tokens", 0) or 0)
        + int(usage.get("cache_read_input_tokens", 0) or 0)
    )


def content_blocks_to_dicts(content: Any) -> Any:
    if isinstance(content, list):
        normalized: list[Any] = []

        for item in content:
            if hasattr(item, "model_dump"):
                normalized.append(item.model_dump())
            elif hasattr(item, "dict"):
                normalized.append(item.dict())
            else:
                normalized.append(item)

        return normalized

    return content


def get_container_id(response: Any) -> str | None:
    container = getattr(response, "container", None)

    if container is None:
        return None

    if isinstance(container, dict):
        value = container.get("id")
        return value if isinstance(value, str) else None

    value = getattr(container, "id", None)
    return value if isinstance(value, str) else None


def upload_json_file(client: Anthropic, json_path: str) -> str:
    path = Path(json_path)

    if not path.exists() or not path.is_file():
        raise ClaudeReviewError(f"No existe el JSON seleccionado: {path}")

    if path.suffix.lower() != ".json":
        raise ClaudeReviewError(f"El archivo seleccionado no es JSON: {path.name}")

    try:
        with path.open("rb") as file_obj:
            try:
                uploaded_file = client.beta.files.upload(
                    file=(path.name, file_obj, "application/json"),
                )
            except TypeError:
                file_obj.seek(0)
                uploaded_file = client.beta.files.upload(
                    file=(path.name, file_obj, "application/json"),
                    betas=[FILES_BETA],
                )
    except Exception as exc:
        raise ClaudeReviewError(
            "No se pudo subir un JSON a Claude. Valida la API key, el workspace "
            "y que el SDK anthropic esté actualizado."
        ) from exc

    return uploaded_file.id


def build_prompt(project_id: str, selected_files: list[str]) -> str:
    file_list = "\n".join(f"- {name}" for name in selected_files)

    return f"""
Use the pauto-cloud-review-json skill to review only the attached Power Automate Cloud workflow JSON files.

Project ID: {project_id or ""}

Selected files:
{file_list}

Return only valid JSON using the pauto_cloud_rows_v3 schema.
Do not generate .xlsx, .csv, Markdown, prose, or explanations outside the JSON.
Do not include raw workflow JSON or full sensitive values in the response.
""".strip()


def build_initial_messages(
    *,
    project_id: str,
    uploaded_json_files: list[dict[str, str]],
) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": build_prompt(
                project_id=project_id,
                selected_files=[
                    item["filename"] for item in uploaded_json_files
                ],
            ),
        }
    ]

    for item in uploaded_json_files:
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


def create_message(
    *,
    client: Anthropic,
    model: str,
    skill_id: str,
    max_tokens: int,
    messages: list[dict[str, Any]],
    container_id: str | None = None,
):
    container: dict[str, Any] = {
        "skills": [
            {
                "type": "custom",
                "skill_id": skill_id,
                "version": "latest",
            },
        ]
    }

    if container_id:
        container["id"] = container_id

    return client.beta.messages.create(
        model=model,
        max_tokens=max_tokens,
        betas=BETAS,
        container=container,
        messages=messages,
        tools=[CODE_EXECUTION_TOOL],
    )


def extract_text_from_response(response: Any) -> str:
    parts: list[str] = []

    for block in getattr(response, "content", []) or []:
        if hasattr(block, "type") and getattr(block, "type") == "text":
            text = getattr(block, "text", "")
            if text:
                parts.append(text)

        elif isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            if text:
                parts.append(text)

    return "\n".join(parts).strip()


def parse_json_from_text(text: str) -> dict[str, Any]:
    """
    Extrae un objeto JSON válido de la respuesta de Claude.

    Acepta:
    - JSON puro.
    - JSON dentro de bloques ```json.
    - Texto antes o después del JSON.
    - Varios bloques de texto, siempre que alguno contenga un objeto válido.

    No intenta reparar silenciosamente un JSON verdaderamente mal formado,
    porque podría alterar los hallazgos del análisis.
    """
    cleaned = (text or "").strip()

    if not cleaned:
        raise ClaudeReviewError(
            "Claude no devolvió contenido de texto con JSON."
        )

    candidates: list[str] = []

    # Primero buscar bloques Markdown explícitos.
    fenced_blocks = re.findall(
        r"```(?:json)?\s*(.*?)```",
        cleaned,
        flags=re.IGNORECASE | re.DOTALL,
    )

    candidates.extend(
        block.strip()
        for block in fenced_blocks
        if block.strip()
    )

    # Después probar la respuesta completa.
    candidates.append(cleaned)

    decoder = json.JSONDecoder()
    last_error: json.JSONDecodeError | None = None

    for candidate in candidates:
        candidate = candidate.strip()

        if not candidate:
            continue

        # Primer intento: todo el contenido es un JSON.
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
        else:
            if isinstance(payload, dict):
                return payload

        # Segundo intento: localizar un objeto JSON dentro de texto adicional.
        for match in re.finditer(r"\{", candidate):
            start_position = match.start()
            fragment = candidate[start_position:]

            try:
                payload, _ = decoder.raw_decode(fragment)
            except json.JSONDecodeError as exc:
                last_error = exc
                continue

            if isinstance(payload, dict):
                return payload

    if last_error is not None:
        raise ClaudeReviewError(
            "Claude terminó la respuesta, pero devolvió un JSON mal formado. "
            f"Detalle: {last_error.msg}. "
            f"Línea {last_error.lineno}, columna {last_error.colno}. "
            "No se realizó otro intento para evitar un cobro adicional."
        ) from last_error

    raise ClaudeReviewError(
        "Claude terminó la respuesta, pero no se encontró ningún "
        "objeto JSON válido. No se realizó otro intento para evitar "
        "un cobro adicional."
    )

def extract_complete_review_json(response: Any) -> dict[str, Any] | None:
    raw_text = extract_text_from_response(response)

    if not raw_text:
        return None

    try:
        payload = parse_json_from_text(raw_text)
    except ClaudeReviewError:
        return None

    if payload.get("schema_version") != "pauto_cloud_rows_v3":
        return None

    if not isinstance(payload.get("detail_rows"), list):
        return None

    errors = payload.get("errors")
    if errors is not None and not isinstance(errors, list):
        return None

    return payload


def write_execution_log(
    *,
    model: str,
    project_id: str,
    selected_files: list[str],
    usage: dict[str, Any],
    usage_calls: list[dict[str, Any]],
    estimated_token_cost_usd: Decimal | None,
    status: str,
    error: str | None = None,
) -> None:
    """
    Guarda información de las ejecuciones de Claude según el modo configurado.

    Modos disponibles mediante ANTHROPIC_EXECUTION_LOG_MODE:

    - all: guarda ejecuciones exitosas y fallidas.
    - errors: guarda únicamente ejecuciones fallidas.
    - off: no guarda ningún registro.
    """
    log_mode = (
        os.getenv("ANTHROPIC_EXECUTION_LOG_MODE")
        or "errors"
    ).strip().lower()

    valid_modes = {
        "all",
        "errors",
        "off",
    }

    if log_mode not in valid_modes:
        allowed_values = ", ".join(sorted(valid_modes))

        raise ClaudeReviewError(
            "La variable ANTHROPIC_EXECUTION_LOG_MODE debe contener "
            f"uno de estos valores: {allowed_values}. "
            f"Valor recibido: {log_mode}"
        )

    # No guardar ningún registro.
    if log_mode == "off":
        return

    # Guardar únicamente errores.
    if log_mode == "errors" and status == "success":
        return

    outputs_dir = Path(
        getattr(
            settings,
            "OUTPUTS_DIR",
            Path("outputs"),
        )
    )
    outputs_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    log_path = outputs_dir / "execution_log.jsonl"

    server_tool_use = usage.get("server_tool_use") or {}

    log_record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "model": model,
        "project_id": project_id,
        "selected_files": selected_files,
        "input_tokens": int(
            usage.get("input_tokens", 0) or 0
        ),
        "output_tokens": int(
            usage.get("output_tokens", 0) or 0
        ),
        "cache_creation_input_tokens": int(
            usage.get(
                "cache_creation_input_tokens",
                0,
            )
            or 0
        ),
        "cache_read_input_tokens": int(
            usage.get(
                "cache_read_input_tokens",
                0,
            )
            or 0
        ),
        "total_tokens": total_tokens_used(usage),
        "code_execution_requests": int(
            server_tool_use.get(
                "code_execution_requests",
                0,
            )
            or 0
        ),
        "estimated_token_cost_usd": (
            float(estimated_token_cost_usd)
            if estimated_token_cost_usd is not None
            else None
        ),
        "calls": usage_calls,
        "error": error,
        "note": (
            "Costo estimado solamente por tokens. Code Execution puede "
            "tener un costo adicional según el tiempo de ejecución, "
            "el free tier y la configuración de la cuenta."
        ),
    }

    try:
        with log_path.open(
            "a",
            encoding="utf-8",
        ) as file_obj:
            file_obj.write(
                json.dumps(
                    log_record,
                    ensure_ascii=False,
                )
                + "\n"
            )
    except OSError:
        # Un error al escribir el log no debe provocar que falle
        # todo el análisis de Claude.
        return

def run_cloud_review(
    *,
    json_files: list[str],
    project_id: str,
) -> dict[str, Any]:
    load_dotenv()

    if not json_files:
        raise ClaudeReviewError("No hay JSON seleccionados para revisar.")

    api_key = get_required_env("ANTHROPIC_API_KEY")
    model = get_required_env("ANTHROPIC_MODEL")
    skill_id = get_required_env("ANTHROPIC_SKILL_ID")

    max_tokens = get_int_env(
        "ANTHROPIC_MAX_TOKENS",
        DEFAULT_MAX_TOKENS,
    )
    max_continuations = get_int_env(
        "ANTHROPIC_MAX_CONTINUATIONS",
        DEFAULT_MAX_CONTINUATIONS,
    )
    max_estimated_cost_usd = get_decimal_env(
        "ANTHROPIC_MAX_ESTIMATED_COST_USD",
        DEFAULT_MAX_ESTIMATED_COST_USD,
    )

    timeout_seconds = get_float_env(
        "ANTHROPIC_TIMEOUT_SECONDS",
        DEFAULT_TIMEOUT_SECONDS,
    )

    if max_tokens <= 0:
        raise ClaudeReviewError(
            "ANTHROPIC_MAX_TOKENS debe ser mayor que 0."
        )

    if max_continuations < 0:
        raise ClaudeReviewError(
            "ANTHROPIC_MAX_CONTINUATIONS no puede ser menor que 0."
        )

    if max_continuations > 1:
        raise ClaudeReviewError(
            "Por control de costos, ANTHROPIC_MAX_CONTINUATIONS no puede "
            "ser mayor que 1."
        )

    client = Anthropic(
        api_key=api_key,
        timeout=timeout_seconds,
        max_retries=0,
    )

    uploaded_json_files: list[dict[str, str]] = []

    for json_file in json_files:
        path = Path(json_file)
        file_id = upload_json_file(client, json_file)

        uploaded_json_files.append(
            {
                "file_id": file_id,
                "filename": path.name,
            }
        )

    selected_files = [
        item["filename"] for item in uploaded_json_files
    ]
    total_usage: dict[str, Any] = {}
    usage_calls: list[dict[str, Any]] = []

    messages = build_initial_messages(
        project_id=project_id,
        uploaded_json_files=uploaded_json_files,
    )

    response = create_message(
        client=client,
        model=model,
        skill_id=skill_id,
        max_tokens=max_tokens,
        messages=messages,
    )

    record_call_usage(
        total_usage=total_usage,
        usage_calls=usage_calls,
        response=response,
    )

    continuations = 0
    completed_review_json: dict[str, Any] | None = None

    while getattr(response, "stop_reason", None) == "pause_turn":
        completed_review_json = extract_complete_review_json(response)

        if completed_review_json is not None:
            break

        if continuations >= max_continuations:
            current_cost = estimate_token_cost_usd(model, total_usage)

            write_execution_log(
                model=model,
                project_id=project_id,
                selected_files=selected_files,
                usage=total_usage,
                usage_calls=usage_calls,
                estimated_token_cost_usd=current_cost,
                status="continuation_limit_reached",
                error=(
                    "Claude solicitó otra continuación después de alcanzar "
                    f"el límite de {max_continuations}."
                ),
            )

            raise ClaudeReviewError(
                "Claude no terminó dentro del límite permitido de "
                f"{max_continuations} continuación(es). Se detuvo la "
                "ejecución para evitar llamadas adicionales."
            )

        current_cost = estimate_token_cost_usd(model, total_usage)

        if (
            current_cost is not None
            and current_cost >= max_estimated_cost_usd
        ):
            write_execution_log(
                model=model,
                project_id=project_id,
                selected_files=selected_files,
                usage=total_usage,
                usage_calls=usage_calls,
                estimated_token_cost_usd=current_cost,
                status="cost_limit_reached",
                error=(
                    "Se evitó una continuación porque el costo estimado "
                    f"alcanzó ${current_cost:.6f} USD."
                ),
            )

            raise ClaudeReviewError(
                "La ejecución solicitó una continuación, pero ya alcanzó "
                f"el límite estimado de ${max_estimated_cost_usd} USD. "
                "La llamada adicional fue bloqueada."
            )

        messages.append(
            {
                "role": "assistant",
                "content": content_blocks_to_dicts(response.content),
            }
        )

        container_id = get_container_id(response)

        if not container_id:
            raise ClaudeReviewError(
                "Claude solicitó una continuación, pero no devolvió un "
                "container_id válido."
            )

        continuations += 1

        response = create_message(
            client=client,
            model=model,
            skill_id=skill_id,
            max_tokens=max_tokens,
            messages=messages,
            container_id=container_id,
        )

        record_call_usage(
            total_usage=total_usage,
            usage_calls=usage_calls,
            response=response,
        )

    raw_text = extract_text_from_response(response)

    if completed_review_json is not None:
        review_json = completed_review_json
    else:
        if getattr(response, "stop_reason", None) == "max_tokens":
            estimated_cost = estimate_token_cost_usd(model, total_usage)

            write_execution_log(
                model=model,
                project_id=project_id,
                selected_files=selected_files,
                usage=total_usage,
                usage_calls=usage_calls,
                estimated_token_cost_usd=estimated_cost,
                status="max_tokens_reached",
                error=(
                    "Claude alcanzó max_tokens antes de devolver el JSON final."
                ),
            )

            raise ClaudeReviewError(
                "Claude alcanzó max_tokens antes de devolver el JSON final. "
                "La ejecución consumió el presupuesto de salida durante el "
                "análisis."
            )

        try:
            review_json = parse_json_from_text(raw_text)
        except ClaudeReviewError as exc:
            estimated_cost = estimate_token_cost_usd(model, total_usage)

            write_execution_log(
                model=model,
                project_id=project_id,
                selected_files=selected_files,
                usage=total_usage,
                usage_calls=usage_calls,
                estimated_token_cost_usd=estimated_cost,
                status="invalid_json",
                error=str(exc),
            )

            raise

    estimated_token_cost_usd = estimate_token_cost_usd(model, total_usage)

    write_execution_log(
        model=model,
        project_id=project_id,
        selected_files=selected_files,
        usage=total_usage,
        usage_calls=usage_calls,
        estimated_token_cost_usd=estimated_token_cost_usd,
        status="success",
    )

    return {
        "review_json": review_json,
        "raw_text": raw_text,
        "model": model,
        "skill_id": skill_id,
        "usage": total_usage,
        "usage_calls": usage_calls,
        "tokens_used": total_tokens_used(total_usage),
        "estimated_token_cost_usd": (
            float(estimated_token_cost_usd)
            if estimated_token_cost_usd is not None
            else None
        ),
        "stop_reason": getattr(response, "stop_reason", None),
        "continuations": continuations,
    }
