import os
import subprocess
import json
import tempfile

import requests

from bot_utils import download_file


VERSION_TIMEOUT = 10


def _validate_sha256(value, field_name):
    value = str(value).strip().lower()
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"Invalid SHA256 in manifest field: {field_name}")
    return value


def fetch_release_manifest(manifest_url):
    response = requests.get(manifest_url, timeout=VERSION_TIMEOUT)
    response.raise_for_status()
    manifest = response.json()
    version = str(manifest.get("version", "")).strip()
    if not version:
        raise ValueError("Release manifest does not contain a version")
    return {
        "version": version,
        "exe_sha256": _validate_sha256(manifest.get("exe_sha256", ""), "exe_sha256"),
        "model_sha256": _validate_sha256(manifest.get("model_sha256", ""), "model_sha256"),
    }


def fetch_model_manifest(manifest_url):
    response = requests.get(manifest_url, timeout=VERSION_TIMEOUT)
    response.raise_for_status()
    manifest = response.json()
    model_version = str(manifest.get("model_version", "")).strip()
    download_url = str(manifest.get("download_url", "")).strip()
    min_app_version = str(manifest.get("min_app_version", "")).strip()
    if not model_version:
        raise ValueError("Model manifest does not contain model_version")
    if not download_url.startswith("https://"):
        raise ValueError("Model manifest download_url must use HTTPS")
    if not min_app_version:
        raise ValueError("Model manifest does not contain min_app_version")
    return {
        "model_version": model_version,
        "download_url": download_url,
        "sha256": _validate_sha256(manifest.get("sha256", ""), "sha256"),
        "min_app_version": min_app_version,
    }


def parse_version(version):
    clean = "".join(char for char in str(version) if char.isdigit() or char == ".")
    return tuple(int(part) for part in clean.split(".") if part.isdigit())


def is_newer_version(latest, current):
    latest_parts = parse_version(latest)
    current_parts = parse_version(current)
    if latest_parts and current_parts:
        length = max(len(latest_parts), len(current_parts))
        latest_parts += (0,) * (length - len(latest_parts))
        current_parts += (0,) * (length - len(current_parts))
        return latest_parts > current_parts
    return str(latest).strip() != str(current).strip()


def fetch_latest_version(version_url):
    response = requests.get(version_url, timeout=VERSION_TIMEOUT)
    response.raise_for_status()
    version = response.text.strip()
    if not version:
        raise ValueError("The version endpoint returned an empty response")
    return version


def download_validated_model(download_url, destination, expected_sha256, validator):
    destination = os.path.abspath(destination)
    destination_dir = os.path.dirname(destination)
    os.makedirs(destination_dir, exist_ok=True)
    descriptor, candidate = tempfile.mkstemp(
        prefix=".model-candidate-", suffix=".pt", dir=destination_dir
    )
    os.close(descriptor)
    os.remove(candidate)
    try:
        actual_sha256 = download_file(download_url, candidate, expected_sha256)
        validator(candidate)
        os.replace(candidate, destination)
        return actual_sha256
    finally:
        if os.path.exists(candidate):
            os.remove(candidate)


def download_app_update(download_url, current_executable, expected_sha256=None):
    current_executable = os.path.abspath(current_executable)
    app_dir = os.path.dirname(current_executable)
    temp_executable = os.path.join(app_dir, "update_temp.exe")
    batch_path = os.path.join(app_dir, "updater.bat")
    backup_executable = os.path.join(app_dir, "update_backup.exe")

    actual_sha256 = download_file(
        download_url,
        temp_executable,
        expected_sha256=expected_sha256,
    )

    batch_code = (
        "@echo off\n"
        "setlocal\n"
        f'copy /y "{current_executable}" "{backup_executable}" > NUL\n'
        "if errorlevel 1 exit /b 1\n"
        "set /a update_wait_count=0\n"
        ":wait_for_app_exit\n"
        f'del /f /q "{current_executable}" > NUL 2>&1\n'
        f'if not exist "{current_executable}" goto install_update\n'
        "set /a update_wait_count+=1\n"
        "if %update_wait_count% GEQ 60 goto update_failed\n"
        "timeout /t 1 /nobreak > NUL\n"
        "goto wait_for_app_exit\n"
        ":install_update\n"
        f'move /y "{temp_executable}" "{current_executable}" > NUL\n'
        f'if not exist "{current_executable}" goto update_failed\n'
        "timeout /t 5 /nobreak > NUL\n"
        'set "PYINSTALLER_RESET_ENVIRONMENT=1"\n'
        f'start "" "{current_executable}"\n'
        "timeout /t 3 /nobreak > NUL\n"
        f'del /f /q "{backup_executable}"\n'
        'del /f /q "%~f0"\n'
        "exit /b 0\n"
        ":update_failed\n"
        f'if not exist "{current_executable}" copy /y "{backup_executable}" "{current_executable}" > NUL\n'
        "exit /b 1\n"
    )
    with open(batch_path, "w", encoding="ascii", newline="\r\n") as handle:
        handle.write(batch_code)

    return batch_path, actual_sha256


def launch_updater(batch_path):
    environment = os.environ.copy()
    for name in tuple(environment):
        if name.startswith("_PYI_"):
            environment.pop(name, None)
    environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    subprocess.Popen(
        ["cmd.exe", "/c", os.path.abspath(batch_path)],
        cwd=os.path.dirname(os.path.abspath(batch_path)),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        env=environment,
    )
