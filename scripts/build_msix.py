"""Build an unsigned MSIX; production identities must come from Partner Center."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
FOUNDATION = "http://schemas.microsoft.com/appx/manifest/foundation/windows10"
UAP = "http://schemas.microsoft.com/appx/manifest/uap/windows10"
UAP10 = "http://schemas.microsoft.com/appx/manifest/uap/windows10/10"
DESKTOP = "http://schemas.microsoft.com/appx/manifest/desktop/windows10"
DESKTOP6 = "http://schemas.microsoft.com/appx/manifest/desktop/windows10/6"
VIRTUALIZATION = "http://schemas.microsoft.com/appx/manifest/virtualization/windows10"
RESCAP = "http://schemas.microsoft.com/appx/manifest/foundation/windows10/restrictedcapabilities"
for prefix, uri in (
    ("", FOUNDATION),
    ("uap", UAP),
    ("uap10", UAP10),
    ("desktop", DESKTOP),
    ("desktop6", DESKTOP6),
    ("virtualization", VIRTUALIZATION),
    ("rescap", RESCAP),
):
    ET.register_namespace(prefix, uri)


def node(parent, name, text=None, *, namespace=FOUNDATION, **attributes):
    element = ET.SubElement(parent, f"{{{namespace}}}{name}", attributes)
    element.text = text
    return element


def load_identity(path: Path | None, development: bool) -> dict[str, str]:
    if development:
        if path:
            raise ValueError("Use --development without a production identity file.")
        return {
            "identity_name": "DRYAPPTR.QuotaCrew.Development",
            "publisher": "CN=QuotaCrew Development",
            "publisher_display_name": "DRY APP TR",
            "privacy_policy_url": "",
        }
    if path is None:
        raise ValueError("Provide --identity from Partner Center, or explicitly use --development.")
    identity = json.loads(path.read_text(encoding="utf-8-sig"))
    required = ("identity_name", "publisher", "publisher_display_name", "privacy_policy_url")
    if not isinstance(identity, dict) or any(
        not isinstance(identity.get(key), str) or not identity[key].strip() for key in required
    ):
        raise ValueError("Fill every public identity field with the exact Partner Center values.")
    if not re.fullmatch(r"[A-Za-z0-9.-]{3,50}", identity["identity_name"]):
        raise ValueError("Invalid package identity name.")
    if not identity["publisher"].startswith("CN="):
        raise ValueError(
            "Publisher must be the exact certificate subject provided by Partner Center."
        )
    if "development" in identity["identity_name"].lower():
        raise ValueError("A development identity cannot be used for a Store package.")
    url = urlsplit(identity["privacy_policy_url"])
    if url.scheme != "https" or not url.hostname or url.username or url.password:
        raise ValueError("Provide a public HTTPS privacy policy URL before a Store build.")
    return {key: identity[key] for key in required}


def create_manifest(
    identity: dict[str, str],
    version: str,
    development: bool,
    revision: int = 0,
    *,
    package_version: str | None = None,
) -> ET.ElementTree:
    if not 0 <= revision <= 65535 or (revision and not development):
        raise ValueError("Package revisions are supported only for local development builds.")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version) or any(
        int(n) > 65535 for n in version.split(".")
    ):
        raise ValueError("Use a three-part numeric source version with components up to 65535.")
    if package_version is not None and (
        development
        or not re.fullmatch(r"\d+\.\d+\.\d+\.0", package_version)
        or any(int(n) > 65535 for n in package_version.split("."))
        or tuple(map(int, package_version.split("."))) < (*map(int, version.split(".")), 0)
    ):
        raise ValueError(
            "Store package version must end in .0 and cannot precede the application version."
        )
    package = ET.Element(
        f"{{{FOUNDATION}}}Package",
        {
            "IgnorableNamespaces": "uap uap10 desktop desktop6 virtualization rescap",
        },
    )
    node(
        package,
        "Identity",
        Name=identity["identity_name"],
        Publisher=identity["publisher"],
        Version=package_version or f"{version}.{revision}",
        ProcessorArchitecture="x64",
    )
    properties = node(package, "Properties")
    display_name = "QuotaCrew (Development)" if development else "QuotaCrew"
    node(properties, "DisplayName", display_name)
    node(properties, "PublisherDisplayName", identity["publisher_display_name"])
    node(properties, "Description", "Independent Codex account and quota utility.")
    node(properties, "Logo", "Assets\\StoreLogo.png")
    # CLI processes outside our package must see the same profiles, and user data
    # must survive uninstall. Windows 11 narrows this to the folders listed below.
    node(properties, "FileSystemWriteVirtualization", "disabled", namespace=DESKTOP6)
    node(properties, "RegistryWriteVirtualization", "disabled", namespace=DESKTOP6)
    fs = node(properties, "FileSystemWriteVirtualization", namespace=VIRTUALIZATION)
    excluded = node(fs, "ExcludedDirectories", namespace=VIRTUALIZATION)
    for folder in ("CodexAccountManager", "Programs\\OpenAI\\Codex"):
        node(
            excluded,
            "ExcludedDirectory",
            "$(KnownFolder:LocalAppData)\\" + folder,
            namespace=VIRTUALIZATION,
        )
    registry = node(properties, "RegistryWriteVirtualization", namespace=VIRTUALIZATION)
    keys = node(registry, "ExcludedKeys", namespace=VIRTUALIZATION)
    node(keys, "ExcludedKey", "HKEY_CURRENT_USER\\Environment", namespace=VIRTUALIZATION)
    resources = node(package, "Resources")
    for language in ("en-US", "tr-TR"):
        node(resources, "Resource", Language=language)
    dependencies = node(package, "Dependencies")
    node(
        dependencies,
        "TargetDeviceFamily",
        Name="Windows.Desktop",
        MinVersion="10.0.19041.0",
        MaxVersionTested="10.0.26100.0",
    )
    capabilities = node(package, "Capabilities")
    for name in ("runFullTrust", "unvirtualizedResources"):
        node(capabilities, "Capability", namespace=RESCAP, Name=name)
    applications = node(package, "Applications")
    application = node(
        applications,
        "Application",
        Id="App",
        Executable="QuotaCrew.exe",
        **{
            f"{{{UAP10}}}RuntimeBehavior": "packagedClassicApp",
            f"{{{UAP10}}}TrustLevel": "mediumIL",
        },
    )
    node(
        application,
        "VisualElements",
        namespace=UAP,
        DisplayName=display_name,
        Description="Independent Codex account and quota utility.",
        BackgroundColor="#0B1020",
        Square150x150Logo="Assets\\Square150x150Logo.png",
        Square44x44Logo="Assets\\Square44x44Logo.png",
    )
    extensions = node(application, "Extensions")
    extension = node(
        extensions,
        "Extension",
        namespace=DESKTOP,
        Category="windows.startupTask",
        Executable="QuotaCrew.exe",
        EntryPoint="Windows.FullTrustApplication",
    )
    node(
        extension,
        "StartupTask",
        namespace=DESKTOP,
        TaskId="QuotaCrewStartup",
        Enabled="false",
        DisplayName=display_name,
    )
    ET.indent(package)
    return ET.ElementTree(package)


def copy_bundle(source: Path, destination: Path) -> None:
    if not source.is_dir() or source.is_symlink():
        raise ValueError("Missing or redirected PyInstaller bundle directory.")
    forbidden_names = {"auth.json", "config.toml", ".env", "switch.recovery.json"}
    for path in source.rglob("*"):
        if path.is_symlink() or not path.resolve().is_relative_to(source.resolve()):
            raise ValueError("Bundle contains a redirected path.")
        if path.is_file() and (
            path.name.lower() in forbidden_names
            or path.suffix.lower()
            in {
                ".db",
                ".bak",
                ".sqlite",
                ".sqlite3",
                ".log",
                ".token",
                ".jwt",
                ".dpapi",
                ".pfx",
                ".p12",
                ".key",
            }
            or path.name.lower().endswith((".recovery.json", ".db-wal", ".db-shm"))
        ):
            raise ValueError("Bundle contains runtime or credential files; refusing to package.")
    shutil.copytree(source, destination, dirs_exist_ok=True)


def make_assets(destination: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from codex_account_manager.gui.design import app_icon

    app = QApplication.instance() or QApplication([])
    destination.mkdir()
    for filename, size in (
        ("StoreLogo.png", 50),
        ("Square44x44Logo.png", 44),
        ("Square150x150Logo.png", 150),
    ):
        if not app_icon(size).pixmap(size, size).save(str(destination / filename), "PNG"):
            raise RuntimeError("Could not generate Store artwork.")
    assert app is not None


LGPL_SOURCES = {
    "qtbase-everywhere-src-6.11.2.tar.xz": "5b2e00eccaf5a4d8c14134ffa0ea8dfd0a35ae1ffc7f8d87fa4305a1ed23cf22",
    "pyside-setup-everywhere-src-6.11.2.tar.xz": "cba47efbaad1bedd529725cbc14e21f156c7a19366f07b3edfbb076ffd7afdf8",
}


def copy_lgpl_sources(source: Path, destination: Path) -> None:
    for filename, expected in LGPL_SOURCES.items():
        archive = source / filename
        if archive.is_symlink() or not archive.resolve().is_relative_to(source.resolve()):
            raise ValueError("Redirected LGPL source archive.")
        with archive.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise ValueError(f"LGPL source archive hash mismatch: {filename}")
    destination.mkdir()
    for filename in LGPL_SOURCES:
        shutil.copy2(source / filename, destination / filename)


def find_makeappx() -> Path:
    root = (
        Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Windows Kits/10/bin"
    )
    candidates = list(root.glob("10.*/x64/makeappx.exe"))
    if not candidates:
        raise FileNotFoundError("Install Windows SDK with MakeAppx to build an MSIX.")
    return max(candidates, key=lambda p: tuple(int(n) for n in p.parent.parent.name.split(".")))


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("MSIX generation requires Windows and Windows SDK.")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identity", type=Path)
    parser.add_argument("--development", action="store_true")
    parser.add_argument("--package-version", help="Store identity version; must end in .0.")
    parser.add_argument(
        "--revision",
        type=int,
        default=0,
        help="Local development package revision; app release version stays unchanged.",
    )
    parser.add_argument("--bundle-dir", type=Path, default=ROOT / "build/store-candidate")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "build/store-package")
    args = parser.parse_args()
    if not 0 <= args.revision <= 65535 or (args.revision and not args.development):
        parser.error("--revision must be 0–65535 and nonzero revisions require --development.")
    identity = load_identity(args.identity, args.development)
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    manifest = create_manifest(
        identity, version, args.development, args.revision, package_version=args.package_version
    )
    package_version = manifest.getroot().find(f"{{{FOUNDATION}}}Identity").get("Version")
    bundles = args.bundle_dir.resolve()
    for bundle in ("QuotaCrew", "cx"):
        if not (bundles / bundle / "_internal/store/runtime.json").is_file():
            raise ValueError(
                "Build the bundle with QUOTACREW_STORE_BUILD=1 and the store extra first."
            )
    from win32api import GetFileVersionInfo

    for executable in (bundles / "QuotaCrew/QuotaCrew.exe", bundles / "cx/cx.exe"):
        if (
            GetFileVersionInfo(str(executable), r"\StringFileInfo\040904B0\ProductVersion")
            != version
        ):
            raise ValueError("The bundle version does not match the current source. Rebuild first.")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    filename = f"QuotaCrew-{package_version}-x64{'-development' if args.development else ''}.msix"
    artifact = output / filename
    if artifact.exists():
        raise FileExistsError(
            "Do not overwrite an existing package; choose a new output directory."
        )
    with tempfile.TemporaryDirectory(prefix="msix-stage-", dir=output) as temporary:
        stage = Path(temporary)
        copy_bundle(bundles / "QuotaCrew", stage)
        copy_bundle(bundles / "cx", stage / "cli")
        for filename in ("LICENSE", "THIRD_PARTY_NOTICES.md", "PRIVACY.html"):
            shutil.copy2(ROOT / filename, stage / filename)
        copy_lgpl_sources(ROOT / "build/lgpl-sources", stage / "Sources")
        shutil.copy2(ROOT / "packaging/store/lgpl-installation.md", stage / "LGPL-INSTALLATION.md")
        shutil.copy2(ROOT / "packaging/WINDOWS-README.md", stage / "README.md")
        make_assets(stage / "Assets")
        manifest.write(
            stage / "AppxManifest.xml",
            encoding="utf-8",
            xml_declaration=True,
        )
        metadata = {
            **identity,
            "version": package_version,
            "application_version": version,
            "channel": "development" if args.development else "store",
        }
        (stage / "store-build.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        subprocess.run(
            [str(find_makeappx()), "pack", "/d", str(stage), "/p", str(artifact), "/o"], check=True
        )
    with artifact.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    artifact.with_suffix(".msix.sha256").write_text(
        f"{digest}  {artifact.name}\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "package": str(artifact),
                "sha256": digest,
                "channel": metadata["channel"],
                "signed": False,
                "store_identity_supplied": not args.development,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
