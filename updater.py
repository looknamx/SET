import os
import subprocess
import json

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
        "timeout /t 2 /nobreak > NUL\n"
        f'copy /y "{current_executable}" "{backup_executable}" > NUL\n'
        "if errorlevel 1 exit /b 1\n"
        f'del /f /q "{current_executable}"\n'
        f'move /y "{temp_executable}" "{current_executable}" > NUL\n'
        f'if not exist "{current_executable}" copy /y "{backup_executable}" "{current_executable}" > NUL\n'
        f'if exist "{current_executable}" start "" "{current_executable}"\n'
        "timeout /t 2 /nobreak > NUL\n"
        f'del /f /q "{backup_executable}"\n'
        'del /f /q "%~f0"\n'
    )
    with open(batch_path, "w", encoding="ascii", newline="\r\n") as handle:
        handle.write(batch_code)

    return batch_path, actual_sha256


def launch_updater(batch_path):
    subprocess.Popen(
        ["cmd.exe", "/c", os.path.abspath(batch_path)],
        cwd=os.path.dirname(os.path.abspath(batch_path)),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
