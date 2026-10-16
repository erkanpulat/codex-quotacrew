# Third-Party Notices

QuotaCrew is licensed under the MIT License. It depends on the following
open-source packages, each distributed under its own license:

| Package | Purpose | License |
| --- | --- | --- |
| pydantic | Data models | MIT |
| typer | CLI framework | MIT |
| rich | Terminal rendering | MIT |
| aiosqlite | Async SQLite | MIT |
| platformdirs | Per-OS data directories | MIT |
| psutil | Process inspection | BSD-3-Clause |
| pywin32 | Windows APIs | PSF |
| PySide6 / Shiboken / Qt (optional, `[gui]`) | Qt for Python GUI | LGPL-3.0; third-party components retain their own licenses |
| PyWinRT / winrt-runtime / Windows.ApplicationModel / Windows.Foundation (optional, `[store]`) | Packaged Windows startup task | MIT |

Refer to each project's distribution for the authoritative license text. The
PySide6 GUI dependency is optional and required for the graphical application.

## Windows binary distributions

Qt is copyright The Qt Company Ltd. and other contributors. Windows packages
include dynamically linked Qt Core, Gui, Widgets and binding-required Network libraries, the Windows
platform/style plugins and PySide6/Shiboken bindings. Qt PDF, virtual keyboard,
QML and other unused plugin bundles are not part of this distribution.

Full license texts, copyright notices and dependency versions are in
`_internal/licenses/LICENSES.txt` in each binary package. Qt 6.11.2 notices are
also available in [the source tree](packaging/licenses/Qt-6.11.2.txt).
Unmodified corresponding sources are available from
[Qt Base 6.11.2](https://github.com/qt/qtbase/tree/v6.11.2) and
[PySide/Shiboken 6.11.2](https://github.com/pyside/pyside-setup/tree/v6.11.2).

You may replace the shared libraries with compatible modified versions and
reverse-engineer the application to debug those modifications. The application
source and build instructions are provided in this repository; rebuild it with
your modified dependency if required. Direct-download EXE distributions do not
lock replacement of these libraries through package signing.
MSIX distributions include complete Qt/PySide/Shiboken source archives in
`Sources` and replacement instructions in `LGPL-INSTALLATION.md`. Copy the
installed payload to a writable directory and run it with compatible modified
libraries, without a publisher signing key. The original Store installation
remains protected by Windows package integrity. The separate executable copy
uses standalone startup and update behavior; use a separate Windows test user
to keep your production profiles isolated.

Python and the PyInstaller bootloader retain their own licenses, included in
the same notice file. The bootloader's exception permits distributing this app
under its MIT license.

## Trademarks

"OpenAI" and "Codex" are trademarks of OpenAI. QuotaCrew is an
independent, unofficial tool and is not affiliated with, sponsored by, or
endorsed by OpenAI.
