from __future__ import annotations

from pathlib import Path


TEXT_ENCODINGS = (
    "utf-8-sig",
    "utf-8",
    "cp1252",
)


def collect_desktop_metrics(txt_paths: list[str]) -> dict[str, int]:
    total_actions = 0
    processed_files = 0

    for txt_path in txt_paths:
        path = Path(txt_path)
        if not path.exists() or not path.is_file():
            continue

        text = _read_text(path)
        total_actions += _count_action_lines(text)
        processed_files += 1

    return {
        "total_flows": processed_files,
        "total_actions": total_actions,
    }


def _read_text(path: Path) -> str:
    raw_content = path.read_bytes()

    for encoding in TEXT_ENCODINGS:
        try:
            return raw_content.decode(encoding)
        except UnicodeDecodeError:
            continue

    return raw_content.decode("utf-8", errors="replace")


def _count_action_lines(text: str) -> int:
    """
    Cuenta líneas estructurales que representan acciones PAD visibles.

    Esta función no evalúa buenas prácticas ni genera incidencias. Solo aporta
    una métrica para el dashboard existente.
    """
    total = 0
    inside_block_comment = False

    for raw_line in text.splitlines():
        line = raw_line.strip()

        if not line:
            continue

        if inside_block_comment:
            if "#/" in line:
                inside_block_comment = False
            continue

        if line.startswith("/#"):
            if "#/" not in line[2:]:
                inside_block_comment = True
            continue

        if line.startswith("#"):
            continue

        upper_line = line.upper()

        if upper_line.startswith("FUNCTION "):
            continue

        if upper_line == "END FUNCTION":
            continue

        if upper_line.startswith("**REGION"):
            continue

        if upper_line.startswith("**ENDREGION"):
            continue

        if upper_line.startswith("ON ERROR"):
            continue

        if upper_line in {
            "END",
            "ELSE",
        }:
            continue

        total += 1

    return total
