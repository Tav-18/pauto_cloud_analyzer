from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

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
DEFAULT_MAX_CONTINUATIONS = 3

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
        total[field] = int(total.get(field, 0) or 0) + int(usage.get(field, 0) or 0)

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

    usage_calls.append(
        {
            "call_index": len(usage_calls) + 1,
            "stop_reason": getattr(response, "stop_reason", None),
            "input_tokens": int(usage.get("input_tokens", 0) or 0),
            "output_tokens": int(usage.get("output_tokens", 0) or 0),
            "cache_creation_input_tokens": int(
                usage.get("cache_creation_input_tokens", 0) or 0
            ),
            "cache_read_input_tokens": int(usage.get("cache_read_input_tokens", 0) or 0),
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
    cache_read_tokens = Decimal(int(usage.get("cache_read_input_tokens", 0) or 0))

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

Return only valid JSON using the pauto_cloud_rows_v2 schema.
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
                selected_files=[item["filename"] for item in uploaded_json_files],
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
    cleaned = (text or "").strip()

    if not cleaned:
        raise ClaudeReviewError("Claude no devolvió texto con JSON.")

    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")

    if start == -1 or end == -1 or end <= start:
        raise ClaudeReviewError("No se pudo localizar un objeto JSON en la respuesta de Claude.")

    try:
        return json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ClaudeReviewError(
            "La respuesta de Claude no es JSON válido. Puede estar truncada por max_tokens."
        ) from exc


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
    outputs_dir = Path(getattr(settings, "OUTPUTS_DIR", Path("outputs")))
    outputs_dir.mkdir(parents=True, exist_ok=True)

    log_path = outputs_dir / "execution_log.jsonl"
    server_tool_use = usage.get("server_tool_use") or {}

    log_record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "model": model,
        "project_id": project_id,
        "selected_files": selected_files,
        "input_tokens": int(usage.get("input_tokens", 0) or 0),
        "output_tokens": int(usage.get("output_tokens", 0) or 0),
        "cache_creation_input_tokens": int(
            usage.get("cache_creation_input_tokens", 0) or 0
        ),
        "cache_read_input_tokens": int(usage.get("cache_read_input_tokens", 0) or 0),
        "total_tokens": total_tokens_used(usage),
        "code_execution_requests": int(
            server_tool_use.get("code_execution_requests", 0) or 0
        ),
        "estimated_token_cost_usd": (
            float(estimated_token_cost_usd)
            if estimated_token_cost_usd is not None
            else None
        ),
        "calls": usage_calls,
        "error": error,
        "note": (
            "Costo estimado solo por tokens. Code execution puede tener costo aparte "
            "según tiempo de ejecución, free tier y configuración de la cuenta."
        ),
    }

    with log_path.open("a", encoding="utf-8") as file_obj:
        file_obj.write(json.dumps(log_record, ensure_ascii=False) + "\n")


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

    max_tokens = get_int_env("ANTHROPIC_MAX_TOKENS", DEFAULT_MAX_TOKENS)
    max_continuations = get_int_env(
        "ANTHROPIC_MAX_CONTINUATIONS",
        DEFAULT_MAX_CONTINUATIONS,
    )

    client = Anthropic(api_key=api_key)

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

    while True:
        stop_reason = getattr(response, "stop_reason", None)

        if stop_reason != "pause_turn":
            break

        if continuations >= max_continuations:
            raise ClaudeReviewError(
                "La ejecución quedó en pause_turn demasiadas veces. "
                "Aumenta ANTHROPIC_MAX_CONTINUATIONS solo si es necesario."
            )

        continuations += 1

        messages.append(
            {
                "role": "assistant",
                "content": content_blocks_to_dicts(response.content),
            }
        )

        container_id = get_container_id(response)

        time.sleep(2)

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

    print("DEBUG stop_reason:", getattr(response, "stop_reason", None))
    print("DEBUG raw_text length:", len(raw_text or ""))

    if getattr(response, "stop_reason", None) == "max_tokens":
        print("DEBUG raw_text preview:", repr(raw_text[:1000]))
        raise ClaudeReviewError(
            "Claude alcanzó max_tokens antes de devolver el JSON final. "
            "La ejecución consumió el presupuesto de salida durante el análisis."
        )

    try:
        review_json = parse_json_from_text(raw_text)
    except Exception:
        print("DEBUG full raw_text:", repr(raw_text))
        print("DEBUG response content:", content_blocks_to_dicts(response.content))
        raise

    print("DEBUG summary:", review_json.get("summary"))
    print("DEBUG detail_rows count:", len(review_json.get("detail_rows", [])))
    print("DEBUG first 2 rows:", review_json.get("detail_rows", [])[:2])
    print("DEBUG errors:", review_json.get("errors", []))
    print("DEBUG review_json type:", type(review_json))
    print("DEBUG review_json keys:", list(review_json.keys()) if isinstance(review_json, dict) else "NOT_DICT")
    print("DEBUG raw_text preview:", raw_text[:2500])
    print("DEBUG review_json full:", review_json)
    estimated_token_cost_usd = estimate_token_cost_usd(model, total_usage)

    write_execution_log(
        model=model,
        project_id=project_id,
        selected_files=[item["filename"] for item in uploaded_json_files],
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