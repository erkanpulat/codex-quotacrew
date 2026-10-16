"""Inspect a built MSIX without installing it or opening any user credentials."""

from __future__ import annotations

import argparse
import hashlib
import json
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PurePosixPath

F = "{http://schemas.microsoft.com/appx/manifest/foundation/windows10}"
D = "{http://schemas.microsoft.com/appx/manifest/desktop/windows10}"
R = "{http://schemas.microsoft.com/appx/manifest/foundation/windows10/restrictedcapabilities}"


def verify(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        normalized = [name.replace("\\", "/") for name in names]
        if len(set(name.casefold() for name in normalized)) != len(normalized):
            raise ValueError("Package contains duplicate paths.")
        for name in normalized:
            relative = PurePosixPath(name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Package contains a path outside its root.")
            if relative.name.casefold() in {"auth.json", "config.toml", ".env"} or (
                relative.suffix.casefold()
                in {
                    ".db",
                    ".bak",
                    ".sqlite",
                    ".sqlite3",
                    ".log",
                    ".jwt",
                    ".token",
                    ".dpapi",
                    ".pfx",
                    ".p12",
                    ".key",
                }
                or relative.name.casefold().endswith((".recovery.json", ".db-wal", ".db-shm"))
            ):
                raise ValueError("Package contains runtime or credential data.")
        required = {
            "AppxManifest.xml",
            "AppxBlockMap.xml",
            "[Content_Types].xml",
            "QuotaCrew.exe",
            "cli/cx.exe",
            "LICENSE",
            "THIRD_PARTY_NOTICES.md",
            "LGPL-INSTALLATION.md",
            "Sources/qtbase-everywhere-src-6.11.2.tar.xz",
            "Sources/pyside-setup-everywhere-src-6.11.2.tar.xz",
            "PRIVACY.html",
            "store-build.json",
            "_internal/privacy/PRIVACY.html",
            "_internal/store/runtime.json",
            "_internal/licenses/LICENSES.txt",
            "cli/_internal/licenses/LICENSES.txt",
            "cli/_internal/store/runtime.json",
            "Assets/StoreLogo.png",
            "Assets/Square44x44Logo.png",
            "Assets/Square150x150Logo.png",
        }
        if missing := required - set(normalized):
            raise ValueError(f"Missing required package files: {sorted(missing)}")
        lookup = dict(zip(normalized, names, strict=True))

        def read(name):
            return archive.read(lookup[name])

        if archive.testzip() is not None:
            raise ValueError("Package has a corrupted archive member.")
        manifest = ET.fromstring(read("AppxManifest.xml"))
        identity = manifest.find(F + "Identity")
        metadata = json.loads(read("store-build.json"))
        if identity is None or any(
            identity.get(key) != metadata[value]
            for key, value in (
                ("Name", "identity_name"),
                ("Publisher", "publisher"),
                ("Version", "version"),
            )
        ):
            raise ValueError("Manifest identity does not match build metadata.")
        if identity.get("ProcessorArchitecture") != "x64":
            raise ValueError("Only the tested x64 build is supported.")
        startup = manifest.find(f".//{D}StartupTask")
        if startup is None or startup.get("Enabled") != "false":
            raise ValueError("Startup must be disabled until the user enables it.")
        for marker in ("_internal/store/runtime.json", "cli/_internal/store/runtime.json"):
            runtime = json.loads(read(marker))
            if runtime.get("channel") != "msix" or runtime.get("startup_task_id") != startup.get(
                "TaskId"
            ):
                raise ValueError("Startup runtime metadata does not match the manifest.")
        capabilities = {
            element.get("Name") for element in manifest.findall(f"{F}Capabilities/{R}Capability")
        }
        if capabilities != {"runFullTrust", "unvirtualizedResources"}:
            raise ValueError("Unexpected restricted capabilities.")
        if read("PRIVACY.html") != read("_internal/privacy/PRIVACY.html"):
            raise ValueError("In-app privacy policy differs from the packaged policy.")
        if b"destekdryapp@gmail.com" not in read("PRIVACY.html"):
            raise ValueError("The public support contact is missing.")
        for notice in ("_internal/licenses/LICENSES.txt", "cli/_internal/licenses/LICENSES.txt"):
            text = read(notice).lower()
            if b"winrt" not in text or b"qt" not in text:
                raise ValueError("Qt or WinRT license notices are missing.")
        signature_present = "AppxSignature.p7x" in normalized
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    sidecar = path.with_suffix(".msix.sha256")
    if (
        not sidecar.is_file()
        or sidecar.read_text(encoding="utf-8").strip() != f"{digest}  {path.name}"
    ):
        raise ValueError("Package SHA-256 sidecar is missing or does not match.")
    return {
        "package": str(path.resolve()),
        "version": metadata["version"],
        "channel": metadata["channel"],
        "file_count": len(names),
        "sha256": digest,
        "signature_file_present": signature_present,
        "installed_runtime_tested": False,
        "store_certified": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.package), indent=2))


if __name__ == "__main__":
    main()
