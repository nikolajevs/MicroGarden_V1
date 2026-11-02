### KiCad Libraries License

The KiCad libraries are licensed under the [Creative Commons CC-BY-SA 4.0 License](https://creativecommons.org/licenses/by-sa/4.0/legalcode), with the following exception:

---------

_To the extent that the creation of electronic designs that use 'Licensed Material' can be considered to be 'Adapted Material', then the copyright holder waives article 3 of the license with respect to these designs and any generated files which use data provided as part of the 'Licensed Material'._

---------

You can read more about KiCad library licensing [here](http://kicad-pcb.org/libraries/license).

**What does this mean?**

KiCad libraries are licensed in such a way to ensure free use of library data for commercial, closed, and non-commercial projects without restriction. KiCad does not wish to exert any control over designs produced using the library data, or force users to reveal proprietary information contained in their designs. Neither do we wish to force users to attribute the KiCad libraries _within their design_.

Use of the library data in a project does not (by itself) require that the design or any files generated from the design are licensed under the CC-BY-SA 4.0 License. You are free to use the library data in your own projects without the obligation to share your project files under this or any other license agreement.

However, if you wish to redistribute the KiCad libraries, or parts thereof (including in modified form) as a collection then the exception above does not apply. Redistributed library collections must be shared under the same license agreement. Under these circumstances, the libraries must also retain attribution information, including the license documents which are distributed with the library files.

----------------------

**Warranty**

The KiCad libraries are provided in the hope that they will be useful, but are provided without warranty of any kind, express or implied.

The KiCad libraries are compiled by the KiCad community - if you find an error in the library data, please help the community and contribute a fix!


### Reused XH connector adaptation

`JST_XH_1x04_P2.50mm_Horizontal_NoSupports.step` is copied unchanged from
`hardware/cheap-version/libraries/GrowBox.3dshapes/`. Its base model is
`JST_XH_S4B-XH-A_1x04_P2.50mm_Horizontal.step`, Copyright (C) 2019 Rene Poeschl.
The derivative retains CC-BY-SA 4.0 with the KiCad exception above. The original
attribution and a modification notice are embedded in the STEP.

The GrowBox adaptation removes only two rear support wings for the stock
XH2.54-4P-90 connector shown by the user; the lead positions, principal body
and height are retained. It was produced by
`hardware/cheap-version/tools/make_xh_no_supports.py`; its provenance and
limitations are recorded in that project's `SOURCES_mechanical.md` and in
PCB_EU `models3d.json`. The pinned upstream URL and SHA-256 are retained in `models3d.json`
for reproduction. The visual adaptation does not qualify the exact
stock part or its dimensions.
