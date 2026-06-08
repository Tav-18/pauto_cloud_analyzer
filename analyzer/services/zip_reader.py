import os
import zipfile
from pathlib import Path
from typing import List


def save_upload(uploaded_file, dst_path: str) -> None:
    os.makedirs(os.path.dirname(dst_path), exist_ok=True)

    with open(dst_path, "wb") as file_obj:
        for chunk in uploaded_file.chunks():
            file_obj.write(chunk)


def extract_zip(zip_path: str, extract_to: str) -> str:
    os.makedirs(extract_to, exist_ok=True)

    extract_root = Path(extract_to).resolve()

    with zipfile.ZipFile(zip_path, "r") as zip_file:
        for member in zip_file.infolist():
            member_path = extract_root / member.filename
            resolved_member_path = member_path.resolve()

            if not str(resolved_member_path).startswith(str(extract_root)):
                raise ValueError(f"Unsafe ZIP path detected: {member.filename}")

        zip_file.extractall(extract_root)

    return extract_to


def find_json_files(root_dir: str) -> List[str]:
    """
    Devuelve solo los JSON que estén dentro de una carpeta llamada Workflows.
    No distingue mayúsculas/minúsculas.
    """
    matches: List[str] = []

    for root, _, files in os.walk(root_dir):
        rel_root = os.path.relpath(root, root_dir)
        rel_parts = [
            part.lower()
            for part in rel_root.replace("\\", "/").split("/")
            if part and part != "."
        ]

        if "workflows" not in rel_parts:
            continue

        for name in files:
            if name.lower().endswith(".json"):
                matches.append(os.path.join(root, name))

    return matches