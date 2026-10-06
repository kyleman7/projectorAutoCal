# Colorimeter corrections

`spotread -X <file>` corrects a filter colorimeter (the i1 Display Pro) for the
spectrum of the light it is measuring. Without one, readings of a UHE-lamp 3LCD
projector can be several ΔE off, mostly at white.

| File | Source | Notes |
|------|--------|-------|
| `epson_tw9400_5050ub_i1pro.ccss` | [DisplayCAL colorimeter corrections database](https://colorimetercorrections.displaycal.net/) — "EPSON TW9400 PJ (i1 Pro)", Argyll ccxxmake, 2019-01-18, 4 samples, 3.3 nm | TW9400 = Home Cinema 5050UB, the 5040UB's successor (same lamp 3LCD design). The picture mode it was measured in is not recorded; Epson's Cinema modes add a filter that changes the spectrum. |

Selected by `colorimeter.ccss_path` in `configs/default_config.json` (or the Setup
tab). A CCSS made from your own projector with a spectrophotometer
(`ccxxmake`) would be more accurate.
