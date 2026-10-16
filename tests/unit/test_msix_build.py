import hashlib
import json
import zipfile

import pytest
from scripts import build_msix, verify_msix


def test_production_package_cannot_use_placeholder_identity_or_private_privacy_url(tmp_path):
    with pytest.raises(ValueError, match="Partner Center"):
        build_msix.load_identity(None, False)
    identity = build_msix.load_identity(None, True)
    path = tmp_path / "identity.json"
    path.write_text(json.dumps(identity))
    with pytest.raises(ValueError):
        build_msix.load_identity(path, False)
    identity.update(
        identity_name="12345.QuotaCrew",
        publisher="CN=12345678",
        privacy_policy_url="https://user:secret@example.com/privacy",
    )
    path.write_text(json.dumps(identity))
    with pytest.raises(ValueError, match="public HTTPS"):
        build_msix.load_identity(path, False)
    identity["privacy_policy_url"] = "https://example.com/privacy"
    path.write_text(json.dumps(identity))
    assert build_msix.load_identity(path, False) == identity


@pytest.mark.parametrize("filename", ["auth.json", "accounts.db", "switch.recovery.json", ".env"])
def test_packaging_refuses_user_credentials_and_runtime_database(tmp_path, filename):
    source = tmp_path / "bundle"
    source.mkdir()
    (source / filename).write_bytes(b"synthetic test data")
    destination = tmp_path / "stage"
    with pytest.raises(ValueError, match="runtime or credential"):
        build_msix.copy_bundle(source, destination)
    assert not destination.exists()


def test_manifest_declares_store_updates_user_controlled_startup_and_shared_data():
    root = build_msix.create_manifest(build_msix.load_identity(None, True), "0.2.1", True).getroot()
    namespaces = {
        "f": build_msix.FOUNDATION,
        "d": build_msix.DESKTOP,
        "r": build_msix.RESCAP,
        "v": build_msix.VIRTUALIZATION,
    }
    assert root.find("f:Identity", namespaces).get("Version") == "0.2.1.0"
    startup_task = root.find(".//d:StartupTask", namespaces)
    assert startup_task.get("Enabled") == "false"
    assert startup_task.get("TaskId") == "QuotaCrewStartup"
    assert {cap.get("Name") for cap in root.findall("f:Capabilities/r:Capability", namespaces)} == {
        "runFullTrust",
        "unvirtualizedResources",
    }
    exclusions = root.findall(".//v:ExcludedDirectory", namespaces)
    assert "$(KnownFolder:LocalAppData)\\CodexAccountManager" in {node.text for node in exclusions}


def test_archive_verifier_refuses_credentials_even_in_nested_dirs(tmp_path):
    path = tmp_path / "synthetic.msix"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("cli/_internal/auth.json", b"synthetic; never real credentials")
    with pytest.raises(ValueError, match="runtime or credential"):
        verify_msix.verify(path)


def test_archive_verifier_refuses_case_collisions(tmp_path):
    path = tmp_path / "synthetic.msix"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("QuotaCrew.exe", b"synthetic")
        archive.writestr("quotacrew.exe", b"synthetic")
    with pytest.raises(ValueError, match="duplicate"):
        verify_msix.verify(path)


def test_local_revision_updates_existing_test_package_without_changing_release_version():
    identity = build_msix.load_identity(None, True)
    root = build_msix.create_manifest(identity, "0.2.2", True, 1).getroot()
    assert root.find(f"{{{build_msix.FOUNDATION}}}Identity").get("Version") == "0.2.2.1"
    with pytest.raises(ValueError, match="local development"):
        build_msix.create_manifest(identity, "0.2.2", False, 1)


def test_store_package_version_can_advance_without_changing_application_release():
    identity = {
        "identity_name": "DRYAPPTR.QuotaCrew",
        "publisher": "CN=synthetic",
        "publisher_display_name": "Synthetic",
    }
    root = build_msix.create_manifest(identity, "0.2.5", False, package_version="0.2.6.0").getroot()
    assert root.find(f"{{{build_msix.FOUNDATION}}}Identity").get("Version") == "0.2.6.0"


@pytest.mark.parametrize("version", ["0.2.5.1", "0.2.4.0", "0.2.6", "0.2.65536.0", "invalid"])
def test_store_package_version_rejects_reserved_revision_or_regression(version):
    with pytest.raises(ValueError, match="package version"):
        build_msix.create_manifest(
            build_msix.load_identity(None, True), "0.2.5", False, package_version=version
        )


def test_source_distribution_rejects_incomplete_or_changed_archives(tmp_path, monkeypatch):
    source = tmp_path / "sources"
    source.mkdir()
    contents = {"qt.tar.xz": b"synthetic Qt source", "pyside.tar.xz": b"synthetic binding source"}
    monkeypatch.setattr(
        build_msix,
        "LGPL_SOURCES",
        {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()},
    )
    for name, data in contents.items():
        (source / name).write_bytes(data)
    destination = tmp_path / "stage"
    (source / "pyside.tar.xz").write_bytes(b"changed archive")
    with pytest.raises(ValueError, match="hash mismatch"):
        build_msix.copy_lgpl_sources(source, destination)
    assert not destination.exists()
    (source / "pyside.tar.xz").unlink()
    with pytest.raises(FileNotFoundError):
        build_msix.copy_lgpl_sources(source, destination)
    assert not destination.exists()
    (source / "pyside.tar.xz").write_bytes(contents["pyside.tar.xz"])
    build_msix.copy_lgpl_sources(source, destination)
    assert {path.name: path.read_bytes() for path in destination.iterdir()} == contents
