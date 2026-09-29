# Autogen — artwork for every new species, unattended

The Atlas "generate" button makes one species at a time, on request. Autogen
does the same job on a timer: every 10 minutes it looks for detected species
with no illustration and renders them.

For each missing pose it:

1. renders with Gemini through `pregen.gen_one`, with the same style, positive
   and anti references `generate_one.py` uses. If the bird runs off the frame
   (ink on the image border), it retries, up to three times, keeping the
   cleanest render;
2. cuts the bird out with BiRefNet, trusting the matte as-is (holes filled,
   largest blob kept);
3. registers it with `build_masks.py --add`, so it appears on the next reload.

## Why not the existing cutters

Both repo cutters decide what is background by colour. The on-station chroma
flood (`generate_one.py`) leaks through gaps in the ink outline, and the
"exterior-cream peel" in `upgrade_cutouts.py` strips paper-coloured pixels it
can reach from outside. On a white or cream bird (White Woodpecker, Campo
Flicker) both remove the body and keep only the dark parts. BiRefNet's own
matte gets these birds right, so autogen uses it directly.

## Install

On the station, after the main install:

```bash
bash ~/BirdNET-Pi/avian/autogen/install-autogen.sh
```

It needs `GEMINI_API_KEY` in `birdnet.conf` (settings → Gemini key). The first
run downloads the BiRefNet model (~1 GB) to `/var/lib/avian-autogen/u2net`.
The cutout peaks around 14 GB of RAM, so it suits a workstation-class station
better than a Pi.

## Limits

- At most 10 species per day, and 3 failed attempts per species per day
  (`DAILY_CAP`, `MAX_FAILS` in `autogen.py`). A species costs roughly 2 to 6
  Gemini calls.
- It checks framing, not accuracy. If a render gets the plumage wrong, add a
  line for that species to `avian/scripts/species-notes.json`, delete the PNG,
  and the next run re-renders it.

State is in `/var/lib/avian-autogen/state.json`; the log is
`journalctl -u avian-autogen`.
