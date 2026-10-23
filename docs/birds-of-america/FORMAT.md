# Historical illustration bundles

Manifest version 2 provides up to 32 numbered illustration choices per species.
The IDs are `variant-1` through `variant-32`, with canonical filenames
`illustrations/<species>.png`, `<species>-2.png`, through `<species>-32.png`.
These numbers describe alternatives, not perched/flight classifications. A
species cannot mix numbered variants and the legacy named poses. Version 1
keeps its existing two-pose contract unchanged.

The station and Frame preserve every selected species' variants when installing,
build separate silhouette geometry for each, verify all bytes and SHA-256 values,
and reuse shared artwork objects. The postcard displays numbered choices and
remembers the selected illustration. The existing PNG, decoded-pixel, download,
total storage, executable-content, and rollback protections remain in force.

`LicenseRef-NoC-US` identifies the accompanying `LICENSES/NoC-US.txt` rights
notice. It is a local SPDX-style reference to a rights statement, **not** a
Creative Commons license or a worldwide public-domain assertion. Display it as
“Rights: No Copyright — United States,” with the exact source statement at
<http://rightsstatements.org/vocab/NoC-US/1.0/>. The source provider has not
determined copyright status elsewhere. Keep the library attribution and source
links with any distribution. The segmentation software's MIT license is separate
from the artwork's status.

Birds of America uses the University of Pittsburgh Library System's Darlington
Collection scans. Original full-resolution reviewed cutouts remain unchanged.
Station copies are resized to at most 1280 pixels on the long side with
premultiplied-alpha Lanczos interpolation. This is a deterministic resize, not
generative editing. Provenance binds each derivative to an approved original,
its source plate checksum, and the library item URL.

Historical unidentified birds remain in the source collection without a guessed
modern station mapping. Mixed compositions are indexed under each identified
bird they contain. Explicit aliases retain compatibility with station BirdNET
labels; they do not claim to replace a current ornithological taxonomy.

This official bundle is distributed through its own checked-in station catalog
entry and immutable artwork objects. Community submission, moderation and v1
feed contracts remain unchanged. Existing station versions need the reader and
postcard update before this bundle can be installed. Do not insert the new entry
into the shared v1 community feed, which older clients validate in full.
