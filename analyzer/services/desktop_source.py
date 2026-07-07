from __future__ import annotations

import re
import shutil
import zipfile
from pathlib import Path
from typing import Iterable


FUNCTION_PATTERN = re.compile(
    r"^\s*FUNCTION\s+(?P<name>.+?)\s+GLOBAL\s*$",
    re.IGNORECASE | re.MULTILINE,
)

TEXT_ENCODINGS = (
    "utf-8-sig",
    "utf-8",
    "cp1252",
)

MAX_SOURCE_FILES = 500
MAX_TOTAL_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
MAX_SINGLE_TXT_BYTES = 5 * 1024 * 1024


class DesktopSourceError(ValueError):
    """Raised when a PAD Desktop source cannot be prepared safely."""


def detect_subflow_name(text: str, *, fallback: str) -> str:
    """Return the PAD FUNCTION name when available, else the file stem."""
    match = FUNCTION_PATTERN.search(text)
    if not match:
        return fallback.strip() or "UnnamedSubflow"

    detected_name = match.group("name").strip()
    return detected_name or fallback.strip() or "UnnamedSubflow"


def prepare_desktop_sources(
    uploaded_files: Iterable,
    workspace_root: str | Path,
) -> dict:
    """
    Normalize either multiple TXT files or one ZIP into PAD TXT candidates.

    The file name is not trusted as the subflow name. The visible name is
    detected from ``FUNCTION <name> GLOBAL`` whenever that header exists.
    """
    files = list(uploaded_files)
    if not files:
        raise DesktopSourceError(
            "Select one or more PAD TXT files or one ZIP project."
        )

    workspace_path = Path(workspace_root)
    source_root = workspace_path / "desktop_source"
    source_root.mkdir(parents=True, exist_ok=True)

    suffixes = [Path(uploaded_file.name).suffix.lower() for uploaded_file in files]

    if len(files) == 1 and suffixes[0] == ".zip":
        source_kind = "zip"
        source_name = Path(files[0].name).name
        source_size = int(getattr(files[0], "size", 0) or 0)

        zip_path = workspace_path / "desktop_source.zip"
        _save_uploaded_file(files[0], zip_path)
        _extract_txt_files_safely(zip_path, source_root)

    elif suffixes and all(suffix == ".txt" for suffix in suffixes):
        source_kind = "txt"
        source_size = sum(
            int(getattr(uploaded_file, "size", 0) or 0)
            for uploaded_file in files
        )

        if len(files) > MAX_SOURCE_FILES:
            raise DesktopSourceError(
                f"A maximum of {MAX_SOURCE_FILES} TXT files is allowed per upload."
            )

        if source_size > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise DesktopSourceError(
                "The selected TXT files exceed the maximum total size of 50 MB."
            )

        source_name = (
            Path(files[0].name).name
            if len(files) == 1
            else f"{len(files)} PAD TXT files selected"
        )

        upload_root = source_root / "uploaded"
        upload_root.mkdir(parents=True, exist_ok=True)

        for index, uploaded_file in enumerate(files, start=1):
            file_size = int(getattr(uploaded_file, "size", 0) or 0)
            if file_size > MAX_SINGLE_TXT_BYTES:
                raise DesktopSourceError(
                    f"{Path(uploaded_file.name).name} exceeds the 5 MB TXT limit."
                )

            safe_name = Path(uploaded_file.name).name or f"subflow_{index}.txt"
            target_path = _unique_path(upload_root / safe_name)
            _save_uploaded_file(uploaded_file, target_path)

    else:
        raise DesktopSourceError(
            "Upload either multiple .txt files or exactly one .zip file. "
            "TXT and ZIP files cannot be mixed in the same upload."
        )

    txt_files = _find_txt_files(source_root)
    if not txt_files:
        raise DesktopSourceError(
            "No .txt subflow files were found in the Desktop source."
        )

    if len(txt_files) > MAX_SOURCE_FILES:
        raise DesktopSourceError(
            f"The Desktop source contains more than {MAX_SOURCE_FILES} TXT files."
        )

    total_txt_bytes = sum(file_path.stat().st_size for file_path in txt_files)
    if total_txt_bytes > MAX_TOTAL_UNCOMPRESSED_BYTES:
        raise DesktopSourceError(
            "The Desktop source exceeds the maximum uncompressed TXT size of 50 MB."
        )

    candidates = []
    for index, file_path in enumerate(txt_files):
        file_size = file_path.stat().st_size
        if file_size > MAX_SINGLE_TXT_BYTES:
            raise DesktopSourceError(
                f"{file_path.name} exceeds the 5 MB TXT limit."
            )

        text = _read_text(file_path)
        fallback_name = file_path.stem
        display_name = detect_subflow_name(
            text,
            fallback=fallback_name,
        )

        candidates.append(
            {
                "id": str(index),
                "display_name": display_name,
                "rel_path": file_path.relative_to(source_root).as_posix(),
                "full_path": str(file_path),
                "source_file_name": file_path.name,
                "name_detected_from_function": display_name != fallback_name,
            }
        )

    return {
        "source_kind": source_kind,
        "source_name": source_name,
        "source_size": source_size,
        "source_root": str(source_root),
        "candidates": candidates,
    }


def _save_uploaded_file(uploaded_file, target_path: Path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)

    with target_path.open("wb") as file_obj:
        for chunk in uploaded_file.chunks():
            file_obj.write(chunk)


def _extract_txt_files_safely(zip_path: Path, extract_root: Path) -> None:
    extract_root = extract_root.resolve()
    extract_root.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(zip_path, "r") as zip_file:
            txt_members = [
                member
                for member in zip_file.infolist()
                if not member.is_dir()
                and Path(member.filename).suffix.lower() == ".txt"
            ]

            if not txt_members:
                raise DesktopSourceError(
                    "The ZIP does not contain any .txt subflow files."
                )

            if len(txt_members) > MAX_SOURCE_FILES:
                raise DesktopSourceError(
                    f"The ZIP contains more than {MAX_SOURCE_FILES} TXT files."
                )

            total_size = sum(member.file_size for member in txt_members)
            if total_size > MAX_TOTAL_UNCOMPRESSED_BYTES:
                raise DesktopSourceError(
                    "The ZIP exceeds the maximum uncompressed TXT size of 50 MB."
                )

            extracted_targets: set[Path] = set()

            for member in txt_members:
                if member.file_size > MAX_SINGLE_TXT_BYTES:
                    raise DesktopSourceError(
                        f"{Path(member.filename).name} exceeds the 5 MB TXT limit."
                    )

                target_path = (extract_root / member.filename).resolve()

                try:
                    target_path.relative_to(extract_root)
                except ValueError as exc:
                    raise DesktopSourceError(
                        f"Unsafe ZIP path detected: {member.filename}"
                    ) from exc

                if target_path in extracted_targets:
                    raise DesktopSourceError(
                        f"The ZIP contains a duplicated TXT path: {member.filename}"
                    )

                extracted_targets.add(target_path)
                target_path.parent.mkdir(parents=True, exist_ok=True)

                try:
                    with zip_file.open(member, "r") as source_obj:
                        with target_path.open("wb") as target_obj:
                            shutil.copyfileobj(source_obj, target_obj)
                except RuntimeError as exc:
                    raise DesktopSourceError(
                        "The ZIP could not be read. Password-protected ZIP files are not supported."
                    ) from exc

    except zipfile.BadZipFile as exc:
        raise DesktopSourceError("The uploaded ZIP file is not valid.") from exc


def _find_txt_files(source_root: Path) -> list[Path]:
    return sorted(
        (
            file_path
            for file_path in source_root.rglob("*.txt")
            if file_path.is_file()
            and "__MACOSX" not in file_path.parts
        ),
        key=lambda file_path: file_path.as_posix().lower(),
    )


def _read_text(file_path: Path) -> str:
    raw_content = file_path.read_bytes()

    for encoding in TEXT_ENCODINGS:
        try:
            return raw_content.decode(encoding)
        except UnicodeDecodeError:
            continue

    raise DesktopSourceError(
        f"{file_path.name} could not be decoded as UTF-8 or Windows-1252 text."
    )


def _unique_path(target_path: Path) -> Path:
    if not target_path.exists():
        return target_path

    stem = target_path.stem
    suffix = target_path.suffix
    parent = target_path.parent
    counter = 2

    while True:
        candidate = parent / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1
