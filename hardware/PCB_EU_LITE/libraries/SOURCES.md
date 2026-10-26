# Происхождение библиотек

Импорт из [PCB_EU, 7bdf281](https://github.com/nikolajevs/PCB_EU/tree/7bdf28109dd0b46dcb12c7556a6aa5835d1a3d35/hardware/PCB_EU/libraries/SOURCES.md), 26.10.2026.
Собственные библиотеки Lite перенесены полностью; из общих библиотек включены
только используемые посадки EU_Mechanical/EU_Parts и локальные модели.
Изменены только относительные пути к моделям. Геометрия посадок сохранена.

- [Лицензия посадок KiCad](LICENSE-footprints.md).
- [Лицензия графики символов](LICENSE-symbol-graphics.md).
- [Лицензия моделей KiCad](EU.3dshapes/LICENSE-KiCad.md).
- [Лицензия собственных моделей](EU.3dshapes/LICENSE-own-models.txt).
- [Исходные атрибуции и чертежи](https://github.com/nikolajevs/PCB_EU/tree/7bdf28109dd0b46dcb12c7556a6aa5835d1a3d35/hardware/PCB_EU/libraries/SOURCES.md).
- [Реестр моделей, источников и контрольных сумм](https://github.com/nikolajevs/PCB_EU/tree/7bdf28109dd0b46dcb12c7556a6aa5835d1a3d35/hardware/PCB_EU/models3d.json).
- [Исходные назначения посадок](https://github.com/nikolajevs/PCB_EU/tree/7bdf28109dd0b46dcb12c7556a6aa5835d1a3d35/hardware/PCB_EU/footprint_assignments.json).

Стандартные модели `${KICAD9_3DMODEL_DIR}`/`${KICAD10_3DMODEL_DIR}` используют
установленные библиотеки KiCad. Наличие 3D-модели не подтверждает физическую
совместимость детали: ограничения U101/U201 и примерки описаны в README.

Заимствованные посадки:

- `libraries/EU_Mechanical.pretty/Crystal_Abracon_ABS07.kicad_mod`
- `libraries/EU_Mechanical.pretty/Fuse_Schurter_UMT250.kicad_mod`
- `libraries/EU_Mechanical.pretty/Fuse_Schurter_UST_USI1206_Reflow.kicad_mod`
- `libraries/EU_Mechanical.pretty/L_Jantek_JNRH1207.kicad_mod`
- `libraries/EU_Mechanical.pretty/TI_PWP0014A_LM3429.kicad_mod`
- `libraries/EU_Mechanical.pretty/microSD_GCT_MEM2061-01-188-00-A.kicad_mod`
- `libraries/EU_Parts.pretty/ESP32-WROOM-32E_NoVias.kicad_mod`

Локальные модели:

- `libraries/EU.3dshapes/Battery_COMF_BC-2001_with_CR2032.step`
- `libraries/EU.3dshapes/Crystal_Abracon_ABS07.step`
- `libraries/EU.3dshapes/ESP32-WROOM-32E.step`
- `libraries/EU.3dshapes/Fuse_Schurter_UMT250.step`
- `libraries/EU.3dshapes/Fuse_Schurter_UST_USI1206.step`
- `libraries/EU.3dshapes/L_Jantek_JNRH1207.step`
- `libraries/EU.3dshapes/TI_PWP0014A_LM3429.step`
- `libraries/EU.3dshapes/microSD_GCT_MEM2061-01-188-00-A.step`
- `libraries/LITE.3dshapes/Diodes_PowerDI5.step`
- `libraries/LITE.3dshapes/KF2510_1x04_P2.54mm_RightAngle.wrl`
- `libraries/LITE.3dshapes/LED_OSTAPA57E2A_Formed_P2.54mm.wrl`
- `libraries/LITE.3dshapes/L_Feryster_DTMSS-27_V.step`
- `libraries/LITE.3dshapes/SOIC-8_3.9x4.9mm_P1.27mm.step`
- `libraries/LITE.3dshapes/VSSOP-10_3x3mm_P0.5mm.step`
