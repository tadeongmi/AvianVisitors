#!/usr/bin/env python3
"""AvianVisitors auto-illustration: give every detected species its artwork.

Run by avian-autogen.timer (every 10 min) as the web user `caddy`, the same user
the Atlas "generate" button runs as; install with avian/autogen/install-autogen.sh.
For each detected species without both poses on disk it:
  1. renders the missing pose(s) with the station's own Gemini path
     (pregen.gen_one, same style/positive/anti references as generate_one.py),
     retrying up to 3x while the bird runs off the frame;
  2. cuts it with BiRefNet trusted as-is (holes filled, largest blob). The repo's
     chroma flood and upgrade_cutouts' cream peel both strip paper-coloured
     plumage (white/cream birds lose their body), so neither is used;
  3. registers it with build_masks.py --add.

Cost brake: at most DAILY_CAP species per local day; a species that fails is
retried on later runs, at most MAX_FAILS times per day. State lives in
/var/lib/avian-autogen/state.json; the log is `journalctl -u avian-autogen`.
"""
from __future__ import annotations
import datetime, fcntl, io, json, os, sqlite3, subprocess, sys, time, traceback
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]   # <station>/avian/autogen/autogen.py
HERE = BASE / "avian/scripts"
ILL = BASE / "avian/assets/illustrations"
RAW = ILL / "raw"
REFS = BASE / "avian/assets/references"
DB = BASE / "scripts/birds.db"
CONF = BASE / "birdnet.conf"
STATE = Path(os.environ.get("STATE_DIRECTORY", "/var/lib/avian-autogen")) / "state.json"
CURATION = STATE.parent / "curation.json"   # review decisions, see avian/regional
LOCK = Path("/run/lock/avian-generation.lock")
DAILY_CAP = 10       # species per day (~2-6 Gemini calls each, ~$0.04 per call)
MAX_FAILS = 3        # attempts per species per day before giving up until tomorrow
EDGE_OK = 0.002      # fraction of frame-edge pixels that may carry ink
TRIES = 3

sys.path.insert(0, str(HERE))


def log(msg: str) -> None:
    print(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def conf_value(name: str) -> str:
    for line in CONF.read_text().splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def load_state() -> dict:
    today = datetime.date.today().isoformat()
    try:
        s = json.loads(STATE.read_text())
    except (OSError, ValueError):
        s = {}
    if s.get("day") != today:
        s = {"day": today, "made": [], "fails": {}}
    return s


def save_state(s: dict) -> None:
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=1) + "\n")
    tmp.replace(STATE)


def edge_ink(png: bytes) -> float:
    import numpy as np
    from PIL import Image
    R = np.asarray(Image.open(io.BytesIO(png)).convert("RGB")).astype(int)
    paper = np.median(np.concatenate([R[:15, :15].reshape(-1, 3), R[:15, -15:].reshape(-1, 3)]), 0)
    ink = np.sqrt(((R - paper) ** 2).sum(2)) > 40
    return float(max(ink[:3].mean(), ink[-3:].mean(), ink[:, :3].mean(), ink[:, -3:].mean()))


def plain_cut(src: Path, dst: Path, sess) -> None:
    import numpy as np
    from PIL import Image, ImageFilter
    from rembg import remove
    from scipy import ndimage
    im = Image.open(src).convert("RGB")
    arr = np.asarray(im)
    h, w, _ = arr.shape
    a = np.asarray(remove(im, session=sess))[:, :, 3]
    solid = ndimage.binary_fill_holes(a >= 128)
    L, m = ndimage.label(solid)
    if m > 1:
        solid = L == int(np.argmax(ndimage.sum(np.ones_like(L), L, range(1, m + 1)))) + 1
    if not solid.any():
        raise RuntimeError("cutout found no bird")
    inside = ndimage.binary_erosion(solid, iterations=2)
    af = a.copy(); af[inside] = 255; af[~solid] = 0
    af = np.maximum(np.asarray(Image.fromarray(af).filter(ImageFilter.GaussianBlur(0.4))),
                    (inside * 255).astype(np.uint8))
    af[~solid] = 0
    ys, xs = np.where(af > 40)
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    pad = round(0.03 * max(y1 - y0, x1 - x0))
    y0 = max(0, y0 - pad); x0 = max(0, x0 - pad); y1 = min(h, y1 + pad); x1 = min(w, x1 + pad)
    tmp = dst.with_name("." + dst.name + ".tmp.png")
    Image.fromarray(np.dstack([arr, af]).astype(np.uint8)[y0:y1, x0:x1], "RGBA").save(tmp)
    os.chmod(tmp, 0o664)
    tmp.replace(dst)


def missing_species() -> list[tuple[str, str, list[int]]]:
    import pregen
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
    rows = db.execute("SELECT Sci_Name, Com_Name, COUNT(*) n FROM detections "
                      "GROUP BY Sci_Name ORDER BY n DESC").fetchall()
    db.close()
    out = []
    for sci, com, _n in rows:
        slug = pregen.slugify(sci)
        poses = [p for p, f in ((1, f"{slug}.png"), (2, f"{slug}-2.png")) if not (ILL / f).exists()]
        if poses:
            out.append((sci, com, poses))
    return out


def rejected_species() -> tuple[list[tuple[str, str, list[int]]], dict[str, str]]:
    """Birds rejected in review (avian/regional) whose rejected images are still
    on disk: re-render both poses, with the reviewer's note added to the prompt."""
    sys.path.insert(0, str(BASE / "avian/regional"))
    import regional
    try:
        curation = regional.load_curation(CURATION)
    except SystemExit as exc:
        log(f"ignoring review state: {exc}")
        return [], {}
    queue = regional.rerender_queue(curation, ILL)
    return [(r["sci"], r["com"], [1, 2]) for r in queue], {r["sci"]: r["note"] for r in queue}


def main() -> int:
    rejected, review_notes = rejected_species()
    todo = missing_species() + rejected
    if not todo:
        return 0
    state = load_state()
    key = conf_value("GEMINI_API_KEY")
    if not key:
        log("no GEMINI_API_KEY in birdnet.conf; skipping")
        return 0

    lock = LOCK.open("r+")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("generation busy (Atlas button or updater); next run")
        return 0

    import pregen
    from rembg import new_session
    prompt = pregen.load_prompt(HERE / "prompt.template.md")
    notes = pregen.load_species_notes(HERE / "species-notes.json")
    sess = None
    RAW.mkdir(parents=True, exist_ok=True)

    for sci, com, poses in todo:
        if len(state["made"]) >= DAILY_CAP:
            log(f"daily cap {DAILY_CAP} reached; {sci} waits for tomorrow")
            break
        if state["fails"].get(sci, 0) >= MAX_FAILS:
            continue
        slug = pregen.slugify(sci)
        log(f"generating {sci} ({com}) poses {poses}")
        try:
            pos_ref = pregen.ensure_reference(REFS, slug, sci, com)
            anti_key = pregen.select_anti_ref_key(sci)
            anti = pregen.load_anti_ref(REFS, anti_key) if anti_key else None
            if sess is None:
                sess = new_session("birefnet-general")
            made = []
            for pose in poses:
                fname = f"{slug}.png" if pose == 1 else f"{slug}-{pose}.png"
                style = REFS / "styles" / pregen.select_style_ref(sci, pose)
                if not style.exists():
                    style = ILL / ("turdus-migratorius.png" if pose == 1 else "turdus-migratorius-2.png")
                best = None
                for attempt in range(1, TRIES + 1):
                    note = " ".join(n for n in (notes.get(sci), review_notes.get(sci)) if n) or None
                    png = pregen.gen_one(key, prompt, sci, com, pose, positive_ref=pos_ref,
                                         anti_ref=anti, anti_ref_key=anti_key if anti else None,
                                         species_note=note, style_ref=style)
                    e = edge_ink(png)
                    if best is None or e < best[0]:
                        best = (e, png)
                    if e < EDGE_OK:
                        break
                    log(f"  {fname} attempt {attempt}: bird runs off the frame ({e:.4f}), retrying")
                    time.sleep(6)
                (RAW / fname).write_bytes(best[1])
                plain_cut(RAW / fname, ILL / fname, sess)
                made.append(fname[:-4])
                log(f"  [ok] {fname} (edge {best[0]:.4f})")
                time.sleep(6)
            subprocess.run([sys.executable, str(HERE / "build_masks.py"), "--add", *made], check=True)
            state["made"].append(sci)
        except Exception as exc:  # noqa: BLE001 - keep going with the next species
            state["fails"][sci] = state["fails"].get(sci, 0) + 1
            log(f"  [fail] {sci}: {exc}")
            traceback.print_exc()
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
