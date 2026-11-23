# REDCap metadata

`data_dictionary.csv` is the project's data dictionary (PID 47765) in REDCap's own CSV layout, metadata only (no
PHI). It was rebuilt on 16.11.26 from the printed codebook (`docs/NMOSD database _ REDCap_Codebook.pdf`, all 303
fields in 12 instruments) with

    python scripts/codebook_to_dictionary.py "docs/NMOSD database _ REDCap_Codebook.pdf" --out config/redcap/data_dictionary.csv

A field's number in the review app is its row in this file, which is the codebook's numbering (the script stops if
the codebook's numbers are not 1..N in order). REDCap's own download (Project Setup → Data Dictionary → "Download
the current Data Dictionary") can replace it; keep the same row order. `tests/fixtures/redcap/data_dictionary_pilot.csv`
is a copy for the tests.

`structured_maps.yaml` maps Demographics values to REDCap codes for the structured fields.
