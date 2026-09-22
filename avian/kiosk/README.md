# Kiosk — the collage on a browser that cannot run it

The collage stylesheets lean on `inset:`, `clamp()`, `dvh`, `aspect-ratio` and
flex `gap`. Safari discards declarations it cannot parse, so on a pre-2020
browser the page loads and then lays itself out wrong. An iPad 4 tops out at
iOS 10.3 / Safari 10, well under every one of those.

The JavaScript is not the problem — `apt.js` and `stamps.js` are ES5 and guard
every modern API (`if (window.ResizeObserver)`), and all 902 illustrations are
PNG rather than WebP, which Safari could not read until iOS 14. It is purely CSS.

So the old browser never sees the CSS. The Pi renders the real site with headless
Chromium — the same `frame/shoot.py` the e-ink frame uses — and writes a PNG into
the webroot. The panel displays a picture, which iOS 10 does perfectly.

```
  Pi 5: BirdNET-Pi → collage → shoot.py (headless Chromium) → frame.png
                                                                 ↓
  iPad 4 (Safari 10):  /kiosk/  →  <div style="background-image: frame.png">
```

## Install

On the station, after the main install:

```bash
bash ~/BirdNET-Pi/avian/kiosk/install-kiosk.sh
```

Then open `http://<station>/kiosk/` on the panel. On iOS, *Share → Add to Home
Screen* gives it a real fullscreen window with no Safari chrome, and
*Settings → Display & Brightness → Auto-Lock → Never* keeps it lit.

## Sizing

Defaults render 768×1024 at `--dsf 2` — a 1536×2048 PNG, pixel-exact for an
iPad 4 in portrait. Override before installing:

```bash
KIOSK_WIDTH=1024 KIOSK_HEIGHT=768 bash ~/BirdNET-Pi/avian/kiosk/install-kiosk.sh
```

Settings live in `/etc/avian-kiosk.conf`; edit and
`sudo systemctl restart avian-kiosk.timer`.

## Refresh

A systemd timer re-renders every 10 minutes by default (`KIOSK_INTERVAL`). The
render writes to a temp file and `mv`s it into place, so the panel never fetches
a half-written PNG. The page itself polls every 2 minutes (`?period=` in
seconds), swaps the image only when the bytes change, backs off to 5 minutes
after repeated failures, and shows a small red dot once a shot is properly
stale. Tap anywhere to force a refresh.

## Chromium on ARM

Playwright does not ship a Chromium build for every ARM64 distribution, and
`playwright install-deps` does not know every Debian release. `install-kiosk.sh`
tries the bundled browser, verifies it actually launches, and otherwise installs
the distro's `chromium` and sets `AVIAN_CHROMIUM`, which `shoot.py` passes to
Playwright as `executable_path`.

## Species names

`shoot.py` can label each bird with its common name (`--bird-names`, which asks
the page for `labels=1`). The e-ink frame exposes this as a toggle rather than a
flag you have to remember, and the kiosk does the same:

```bash
KIOSK_BIRD_NAMES=1 bash ~/BirdNET-Pi/avian/kiosk/install-kiosk.sh
```

On an existing install, edit `KIOSK_BIRD_NAMES` in `/etc/avian-kiosk.conf` and
re-render:

```bash
sudo systemctl start avian-kiosk.service
```

Accepts `1`, `true`, `yes` or `on`; anything else leaves the names off. Worth
turning on for a wall frame people walk past and ask about, and off if you want
the collage to read as a picture rather than a chart.

## Flaky renders

With `KIOSK_BIRD_NAMES=1`, `shoot.py` occasionally aborts with:

```
shoot failed: frame labels missing for: <species>
```

It checks that every collage tile carries a rendered label, and sometimes
one is absent. An identical re-run succeeds, and the species involved has
complete `dims.json` / `masks.json` entries and both illustration poses, so
it is not missing data. I could not characterise it further: the same page
loaded outside `shoot.py` always has every label, but `shoot.py` injects CSS
and rewrites the layout tunables, so that is not a like-for-like comparison.

Since the render is timer-driven, one flaky attempt would leave a stale PNG
on the wall for the whole interval. `avian-kiosk-shot` therefore retries up
to three times, five seconds apart, before giving up. Retries are logged:

```bash
journalctl -u avian-kiosk.service | grep retrying
```
