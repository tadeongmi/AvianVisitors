# Birds of America preservation

The station manifest is SHA-256
`f367d5fa8c80aa2296dcf3750313d9b65b409a59135d203ac2ba6bd08a220847`
(175,796 bytes). It identifies 439 species, 883 species/illustration references,
and 819 unique optimized PNG objects totaling 752,759,267 bytes.

The full-resolution preservation manifest is SHA-256
`c8d48a3f7319475a10d6e3db33eda3ed3a7af226ea96ac72e4d4f97eb0dfdc21`
(2,122,332 bytes). It records all 831 approved cutout records as 829 unique
original PNGs and preserves the exact text and hashes of all 435 source IIIF
metadata records. This is a preservation record, not an installable bundle.

Both manifests use the public URL pattern
`https://avianvisitors.com/api/bundles/manifests/<sha256>.json`.
PNG objects use `https://avianvisitors.com/api/bundles/objects/<sha256>.png`.
Download the preservation manifest, verify its byte length and SHA-256, and
follow its individual object URLs to recover the originals. Verify each object's
recorded length and checksum before using it. Original library scan JPEGs can
be fetched again from the retained source URLs; they are not included in the
cutout archive.

The hosted credits page retains the complete source index, rights evidence,
per-illustration provenance, station naming aliases, exclusions and separate
BiRefNet MIT notice. It links the preservation manifest directly.

The deterministic build uses Pillow with premultiplied-alpha Lanczos resizing
at a maximum long side of 1,280 pixels. `build_birds_of_america.py` verifies the
approved original before deriving a station copy. `check_birds_of_america_quality.py`
compares all unique station copies against their originals. The release check
found a minimum PSNR of 40.97 dB and a maximum mean channel difference of 0.992
on a 0–255 scale across 560/1,024-pixel light/dark comparisons.

`upload_birds_of_america.py` freezes input hashes, reuses only exact matching
remote objects, publishes the manifest after all its images, and independently
downloads every upload for verification. Local originals and working caches may
be removed only after both complete upload receipts and public manifests have
been checked. Keep the small local study metadata, review decisions, repair
recipes, quality report and publication receipts for reproducibility.
