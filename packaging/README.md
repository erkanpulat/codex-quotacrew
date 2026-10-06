# Windows packaging

Build the GUI (`QuotaCrew.exe`), CLI (`cx.exe`), installer and portable ZIP from the reviewed source. Python is bundled. Account data and Codex history live outside the installation directory and survive upgrades and removal.

## Publishing updates

1. Set the same version in `pyproject.toml`, `src/codex_account_manager/__init__.py` and `packaging/installer.iss`; update the changelog.
2. Run the [contribution checks](../CONTRIBUTING.md), review the diff and push the intended commit.
3. Run **Actions → Publish release** with that version. It tests, scans and builds the committed source, verifies installation and upgrade, then publishes the installer, portable ZIP and `SHA256SUMS.txt`.
4. Verify the completed workflow and release. Installed clients detect a higher stable version on their daily check or through **Check for updates**. Replacing files under an existing version does not trigger another update.

The updater uses `erkanpulat/codex-quotacrew` and GitHub's SHA-256 asset digest. A checksum verifies integrity; the binaries are not code-signed. Keep account data, credentials and local build output out of Git.

## Local build

Requirements: Windows, Python 3.11–3.13 and [Inno Setup 6](https://jrsoftware.org/isdl.php).

```powershell
python -m pip install -e ".[gui,dev]" pyinstaller
python -m PyInstaller --clean --noconfirm packaging/QuotaCrew.spec
python scripts/verify_windows_package.py
python scripts/verify_windows_package.py --first-run
iscc packaging/installer.iss
```

Outputs are `dist/QuotaCrew`, `dist/cx` and `installer/Output/QuotaCrew-Setup-<version>.exe`. Close programs running from those directories before rebuilding. For a custom output path, pass `--distpath` to PyInstaller and `/DBundleRoot=<absolute path>` to Inno Setup.

The package checks open the real GUI with temporary user data. `verify_windows_installer.py` additionally tests installation, upgrade, shortcuts and removal on disposable GitHub-hosted Windows runners; it refuses personal machines.

## Portable archive

```powershell
Copy-Item packaging/WINDOWS-README.md -Destination dist/QuotaCrew/README.md
Copy-Item LICENSE,THIRD_PARTY_NOTICES.md -Destination dist/QuotaCrew
Compress-Archive -Path dist/QuotaCrew/* -DestinationPath QuotaCrew-portable-<version>.zip
```

Keep the complete extracted folder together. The installer includes the CLI separately and offers optional desktop and startup shortcuts.

## Dependency notices

`scripts/bundle_licenses.py` collects notices into `_internal/licenses/LICENSES.txt`. When updating Qt, update its matching files under `packaging/licenses` and verify the packaged GUI again. Preserve license files in every distribution.
