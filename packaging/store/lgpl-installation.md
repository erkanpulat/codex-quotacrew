# Qt / PySide6 source, replacement and installation

QuotaCrew uses dynamically linked Qt Core, Gui, Widgets and Network, the Windows
platform/style plugins, PySide6 and Shiboken, version 6.11.2. You may modify those
libraries and reverse-engineer QuotaCrew to debug your modifications. QuotaCrew's
MIT license does not restrict these rights. Third-party components keep their
own license terms; their notices are in `_internal/licenses/LICENSES.txt`.

## Corresponding library source

The complete unmodified upstream source archives, including build files and
bundled third-party sources, are included in this package's `Sources` directory.
The same archives are also available from Qt:

- [Qt Base 6.11.2 source](https://download.qt.io/archive/qt/6.11/6.11.2/submodules/qtbase-everywhere-src-6.11.2.tar.xz)
  — SHA-256 `5b2e00eccaf5a4d8c14134ffa0ea8dfd0a35ae1ffc7f8d87fa4305a1ed23cf22`.
- [PySide6 / Shiboken 6.11.2 source](https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.11.2-src/pyside-setup-everywhere-src-6.11.2.tar.xz)
  — SHA-256 `cba47efbaad1bedd529725cbc14e21f156c7a19366f07b3edfbb076ffd7afdf8`.

The packaged libraries come from the official PySide6 6.11.2 Windows x64 wheels;
QuotaCrew makes no changes to the library sources. Extract the source archives
with an XZ-capable archive tool and use their build instructions. License and
copyright notices remain in the archives and `_internal/licenses/LICENSES.txt`.
For source-access problems, contact `destekdryapp@gmail.com`.

## Run with modified shared libraries

The Store installation directory is protected by Windows package integrity.
Make a separate, writable copy of the entire installed application payload before
replacing a library. Do not edit the original Store installation or its signature.

1. Locate the current-user package using `Get-AppxPackage DRYAPPTR.QuotaCrew`
   (`DRYAPPTR.QuotaCrew.Development` for the separate test build). Copy its
   `InstallLocation` contents to a new directory you own, such as
   `Documents\QuotaCrew-modified`. Preserve all relative paths and both the GUI
   and `cli` payloads. Administrative ownership changes are not needed or part
   of this procedure. Include the `Sources` directory and this document in the copy.
2. In the copy, replace compatible Qt DLLs in `_internal\PySide6` and
   `cli\_internal\PySide6`. Replace associated platform/style plugins under
   `plugins` as required. A change to PySide6 or Shiboken also requires replacing
   the corresponding `.pyd` and DLL files in their `_internal` subdirectories.
3. Use Windows x64 release builds with compatible Python ABI and MSVC runtime.
   Keep the library filenames and exported interfaces expected by the bindings.
   Use the included upstream build files and
   [Qt for Python's Windows build guide](https://doc.qt.io/qtforpython-6/building_from_source/windows.html)
   when building modified bindings. Changes that break the ABI require rebuilding
   the bindings and potentially the application rather than only swapping DLLs.
4. Launch `QuotaCrew.exe` from the writable copy. This copy runs outside the Store
   package identity; Windows StartupTask and Store installation/update behavior
   are specific to the installed package. No publisher private signing key is
   required to run the separate executable copy.
5. Use a separate Windows test user for this validation because QuotaCrew uses
   shared per-user Codex/profile directories. Do not use production credentials
   or compare library behavior by switching real accounts.

References: [Qt LGPL obligations](https://www.qt.io/development/open-source-lgpl-obligations),
[LGPL v3, section 4](https://www.gnu.org/licenses/lgpl-3.0.html).
