# Machine data

`bed_holes.json`: hole positions (mm from the bed's front-left corner) of the Makera Z1 MDF bed and the Carvera Air MDF bed,
used by `machine_models.py` to drill the real hole grid into the spoilboard of the machine model.

- `makera_z1_mdf` — from Makera's official `Z1-MDF-v2.1` bed file (36 counterbored M5 holes, 206×206×6) and the
  cinetronix *makera-z1-bed-cad-files* repository (CC BY 4.0, https://github.com/cinetronix/makera-z1-bed-cad-files).
- `carvera_air_mdf` — from the Carvera Community bed model (66 Ø6 holes, 306×222×15).

Only the extracted hole coordinates live here; the CAD files themselves are not redistributed. The Carvera Community
simplified machine models (github.com/Carvera-Community/Carvera_Community_Profiles) were used as a dimensional reference
for the spindle nose, head, enclosure and 4th-axis parts; every number carries its source and accuracy tier in
`MachineModel.sources` (shown in Settings ▸ Machines ▸ Model).
