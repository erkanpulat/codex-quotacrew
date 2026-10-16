import os
import sys
import runpy
import tomllib
from pathlib import Path
from xml.dom import minidom
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.win32.winmanifest import create_application_manifest
from PyInstaller.utils.win32.versioninfo import FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo

# Keep DLL discovery independent of unrelated tools installed on PATH.
# Qt uses Windows ICU; a same-named third-party ICU can have incompatible exports.
if sys.platform == "win32":
    windows = os.environ.get("SystemRoot", "C:/Windows")
    os.environ["PATH"] = os.pathsep.join([
        os.path.dirname(sys.executable), sys.base_prefix,
        os.path.join(windows, "System32"), windows,
    ])

_root = Path(SPECPATH).parent
_version = tomllib.loads((_root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
_store_build = os.environ.get("QUOTACREW_STORE_BUILD") == "1"
_notices = runpy.run_path(str(_root / "scripts/bundle_licenses.py"))["collect_notices"](
    _root, extras=("gui", "store") if _store_build else ("gui",),
)
_hidden = collect_submodules("codex_account_manager")
if _store_build:
    from winrt.windows.applicationmodel import StartupTask
    _hidden += collect_submodules("winrt")
_datas = [(str(_notices), "licenses"), (str(_root / "THIRD_PARTY_NOTICES.md"), "licenses"), (str(_root / "LICENSE"), "licenses"), (str(_root / "PRIVACY.html"), "privacy")]
if _store_build:
    _datas.append((str(_root / "packaging/store/runtime.json"), "store"))

def executable_version(name):
    numbers = tuple(int(part) for part in _version.split(".")) + (0,)
    fields = {
        "CompanyName": "QuotaCrew contributors",
        "ProductName": "QuotaCrew for Codex",
        "FileDescription": "QuotaCrew" if name == "QuotaCrew" else "QuotaCrew CLI",
        "FileVersion": _version,
        "ProductVersion": _version,
        "OriginalFilename": name + ".exe",
        "InternalName": name,
        "LegalCopyright": "Copyright (c) 2026 QuotaCrew contributors",
    }
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=numbers, prodvers=numbers),
        kids=[StringFileInfo([StringTable("040904B0", [StringStruct(key, value) for key, value in fields.items()])]), VarFileInfo([VarStruct("Translation", [1033, 1200])])],
    )

_common = dict(
    pathex=["src"],
    hiddenimports=_hidden,
    hookspath=[str(_root / "packaging/hooks")],
    datas=_datas,
    runtime_hooks=[],
    excludes=["tkinter", "mypy", "pydantic.mypy", "pytest", "ruff"] + ([] if _store_build else ["winrt"]),
    noarchive=False,
)

gui_a = Analysis(["../src/codex_account_manager/gui/tray_main.py"], **_common)
gui_a.binaries = [item for item in gui_a.binaries if Path(item[0]).name != "opengl32sw.dll"]
gui_pyz = PYZ(gui_a.pure)
gui_manifest = minidom.parseString(create_application_manifest())
windows_settings = gui_manifest.getElementsByTagName("windowsSettings")[0]
for name, year, value in (("dpiAware", 2005, "true/pm"), ("dpiAwareness", 2016, "PerMonitorV2")):
    setting = gui_manifest.createElement(name)
    setting.setAttribute("xmlns", f"http://schemas.microsoft.com/SMI/{year}/WindowsSettings")
    setting.appendChild(gui_manifest.createTextNode(value))
    windows_settings.appendChild(setting)
gui_exe = EXE(
    gui_pyz, gui_a.scripts, [], exclude_binaries=True,
    name="QuotaCrew", console=False, icon=os.path.join(SPECPATH, "assets", "app.ico"),
    version=executable_version("QuotaCrew"),
    manifest=gui_manifest.toxml(),
)
gui_coll = COLLECT(
    gui_exe, gui_a.binaries, gui_a.datas, name="QuotaCrew",
)

cli_a = Analysis(["../src/codex_account_manager/cli/main.py"], **_common)
cli_a.binaries = [item for item in cli_a.binaries if Path(item[0]).name != "opengl32sw.dll"]
cli_pyz = PYZ(cli_a.pure)
cli_exe = EXE(
    cli_pyz, cli_a.scripts, [], exclude_binaries=True,
    name="cx", console=True, icon=os.path.join(SPECPATH, "assets", "app.ico"),
    version=executable_version("cx"),
)
cli_coll = COLLECT(
    cli_exe, cli_a.binaries, cli_a.datas, name="cx",
)
