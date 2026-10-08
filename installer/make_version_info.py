# Copyright (c) 2026 AIXENI (aixeni.xyz). All rights reserved. Proprietary, see LICENSE. Copying, modifying or redistributing any part of this file without written permission is prohibited.
# AN-AIXENI-7f3c9e21
"""Writes the Windows file details (Properties > Details) for AstraNova's programs: publisher AIXENI, copyright,
product name and version. Usage: python installer/make_version_info.py build/version_info.txt "AstraNova Setup"
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from astra import __version__  # noqa: E402

out, desc = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "AstraNova")
nums = [int(x) for x in __version__.split(".")[:3]] + [0]
while len(nums) < 4:
    nums.insert(-1, 0)
t = nums[:4]
Path(out).parent.mkdir(parents=True, exist_ok=True)
Path(out).write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={tuple(t)}, prodvers={tuple(t)}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1,
                    subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'AIXENI'),
      StringStruct('FileDescription', '{desc}'),
      StringStruct('FileVersion', '{__version__}'),
      StringStruct('InternalName', 'AstraNova'),
      StringStruct('LegalCopyright', 'Copyright (c) 2026 AIXENI. All rights reserved.'),
      StringStruct('OriginalFilename', 'AstraNova.exe'),
      StringStruct('ProductName', 'AstraNova'),
      StringStruct('ProductVersion', '{__version__}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""", encoding="utf-8")
print("version info:", out)
