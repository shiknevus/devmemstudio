# Third-party software

Devmem Studio uses the following open-source components. Their original copyright notices and licenses apply.

- Python 3.12.2 — Python Software Foundation License: https://docs.python.org/3/license.html
- Qt / PySide6 6.11.2 and Shiboken6 — LGPLv3 / GPLv3 or commercial licenses: https://doc.qt.io/qtforpython-6/licenses.html
- Paramiko 4.0.0 — LGPL-2.1: https://github.com/paramiko/paramiko/blob/4.0.0/LICENSE
- cryptography — Apache-2.0 / BSD-3-Clause: https://github.com/pyca/cryptography
- bcrypt — Apache-2.0: https://github.com/pyca/bcrypt
- PyNaCl — Apache-2.0: https://github.com/pyca/pynacl
- cffi — MIT: https://cffi.readthedocs.io/
- PyInstaller bootloader — GPL with distribution exception: https://pyinstaller.org/en/stable/license.html

Qt libraries remain separate shared libraries inside the extracted runtime of the single-file package. The build source and spec are supplied with this project. The icon and interface graphics are project-native SVG / Qt drawings. Windows system fonts are used at runtime and are not bundled.
