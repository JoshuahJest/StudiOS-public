# Rocky Horror visual asset working set

Local working assets for TouchDesigner/Resolume, sourced on 2026-10-26. This is a **working library, not a cleared-rights deliverable**.

## Contents
- `source/RHPS_Theater_Kit/RHPS Theater Kit/`: Fox/Fan Club promotional theater kit mirrored by [Rocky Talkie](https://www.rockytalkiepodcast.com/articles/design-guide/). 3 alpha logos (including 2661×1404 lips), 6 RHPS high-resolution photos (Columbia, Frank-N-Furter, group shots, alternate lips), 12 mylars, 5 giant poster replicas, plus Shock Treatment images. The mirror says the kit was intended for theater/cast promotion; that does not establish a general commercial performance or redistribution license.
- `source/tmdb_stills/`: 30 full-resolution film stills at 1920×1080 or larger, pulled from [TMDB's Rocky Horror backdrops](https://www.themoviedb.org/movie/36685-the-rocky-horror-picture-show/images/backdrops). Exact URLs, dimensions, hashes in `stills_manifest.json`. No rights grant is inferred from availability on TMDB.
- `transparent_png/`: 39 straight-alpha PNGs: 3 original theater-kit logos and 36 **automated draft** subject masks (6 kit photos + 30 stills). `cutout_manifest.json` maps each to source and dimensions. Review matting, missing limbs, extraneous subjects, and edges before performance; do not treat model-generated masks as hand-traced finals. Some film stills may contain multiple characters or scenery rather than a single useful cutout.
- Fonts: `source/DoubleFeature20.ttf` (also included in the kit, SIL OFL 1.1 per [official fan-site download](https://www.rockyhorror.com/downloads/font.php)); `source/Rocky_AOE/Truetype/ROCKYAOE.ttf` (freeware for personal/commercial design use; bundled `AOETI_README.pdf` limits some font redistribution and alphabet products). `DentonDings10.ttf` is also in kit, but license not independently checked.
- `source_stills_contact_sheet.jpg` and `transparent_cutouts_contact_sheet.jpg` allow visual selection by filename. Poster replicas and mylars remain source images, not separately cut out.

## Playback/import
Load any `transparent_png/*.png` as a Movie File In TOP in TouchDesigner or as a still image in Resolume. Alpha is preserved; use a transparent composition and check premultiplication/edge halos in the target app. Source photos/stills are preserved unmodified. The original logo files are copied and cropped only to their nonzero-alpha bounds.

## Rebuild
Run `python source_and_inventory.py`, `python fetch_stills.py` in an environment with requests/Pillow; run `.venv/bin/python make_assets.py`, `.venv/bin/python cutout_stills.py`, `.venv/bin/python contact_sheets.py` for the masks/sheets. Mask generator is rembg `u2netp` with onnxruntime in the project-local `.venv`; model downloaded to `~/.rembg/models/u2netp/`. No system-wide dependencies or credentials were required. Existing downloads/cutouts are reused where possible.

## Rights / quality gate
The film, performers' likenesses, poster imagery, and promotional stills remain protected material. "Free font" and "freely downloadable theater kit" do **not** imply the imagery is public-domain or cleared for a public VJ show, streaming, marketing, merch, or redistribution. Obtain permission or check your venue's use before deployment; for a rights-clean alternative, commission original Rocky-inspired typography/characters instead of reproducing exact film elements. Do not distribute the whole source library or bundle the fonts indiscriminately.
