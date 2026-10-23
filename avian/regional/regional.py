#!/usr/bin/env python3
"""Curate this station's own illustrations and export them as a catalog bundle.

The station's library holds the included woodblock set plus every species
autogen (or the Atlas "generate" button) rendered locally. This tool covers the
local ones only:

  status                 what is pending, approved, rejected or excluded
  sheet [--out FILE]     a self-contained review page with both poses per bird
  approve SLUG...        mark the current images as good to submit
  approve --pending      approve everything still pending
  reject SLUG --note T   have autogen re-render it, with T added to its prompt
  exclude SLUG...        keep it on the station but never submit it
  export [--out DIR]     approved birds -> validated, upload-ready ZIP

A decision is pinned to the sha256 of both pose files. When autogen re-renders
a rejected bird, its hash changes and it is pending again, so nothing is
submitted that was not looked at in its current form.

State lives beside autogen's in /var/lib/avian-autogen/curation.json, outside
the checkout, so the station updater never sees local changes. Run it as the
web user (caddy), like autogen; the installed avian-regional wrapper does that.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import datetime
import fcntl
import hashlib
import html
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]   # <station>/avian/regional/regional.py
HERE = BASE / "avian/scripts"
ILL = BASE / "avian/assets/illustrations"
CATALOG = BASE / "avian/bundles/catalog-v1.json"
CONFIG = Path(__file__).resolve().parent / "bundle.json"
DB = BASE / "scripts/birds.db"
STATE_DIR = Path(os.environ.get("AVIAN_AUTOGEN_STATE", "/var/lib/avian-autogen"))
CURATION = STATE_DIR / "curation.json"
LOCK = Path("/run/lock/avian-generation.lock")
# The station webroot (tailnet-only), so the sheet and the ZIP open from any
# browser at http://<station>/regional/.
WEB_DIR = BASE.parent / "BirdSongs/Extracted/regional"
DEFAULT_SHEET = WEB_DIR / "index.html"
STATUSES = ("approved", "rejected", "excluded")

sys.path.insert(0, str(HERE))


def slugify(sci: str) -> str:
    import pregen
    return pregen.slugify(sci)


def included_slugs(catalog: Path = CATALOG) -> set[str]:
    """Species the station already ships: the built-in pack in the catalog."""
    packs = json.loads(catalog.read_text())["packs"]
    included = [p for p in packs if p.get("availability") == "included"]
    if len(included) != 1:
        raise SystemExit(f"expected one included pack in {catalog}, found {len(included)}")
    return {slugify(s) for s in included[0]["species"]}


def detected_names(db: Path = DB) -> dict[str, tuple[str, str]]:
    """slug -> (scientific, common) for every species the station has heard."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        rows = con.execute("SELECT Sci_Name, Com_Name FROM detections GROUP BY Sci_Name").fetchall()
    finally:
        con.close()
    return {slugify(sci): (sci, com) for sci, com in rows}


def pose_files(slug: str, ill: Path = ILL) -> tuple[Path, Path]:
    return ill / f"{slug}.png", ill / f"{slug}-2.png"


def art_hash(slug: str, ill: Path = ILL) -> str | None:
    """sha256 over both poses; None while either is missing."""
    h = hashlib.sha256()
    for f in pose_files(slug, ill):
        if not f.is_file():
            return None
        h.update(f.name.encode() + b"\0" + f.read_bytes())
    return h.hexdigest()


def local_species(ill: Path = ILL, catalog: Path = CATALOG, db: Path = DB) -> list[dict]:
    """Every complete local (non-included) species, with its current hash."""
    shipped = included_slugs(catalog)
    names = detected_names(db) if db.exists() else {}
    slugs = sorted({p.stem.removesuffix("-2") for p in ill.glob("*.png")} - shipped)
    out = []
    for slug in slugs:
        digest = art_hash(slug, ill)
        if digest is None:
            continue
        sci, com = names.get(slug, (slug.replace("-", " ").capitalize(), ""))
        out.append({"slug": slug, "sci": sci, "com": com, "hash": digest})
    return out


def load_curation(path: Path = CURATION) -> dict:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {"schema": 1, "species": {}}
    if data.get("schema") != 1 or not isinstance(data.get("species"), dict):
        raise SystemExit(f"{path} is not a curation file this tool understands")
    return data


def save_curation(data: dict, path: Path = CURATION) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False, sort_keys=True) + "\n")
    os.chmod(tmp, 0o664)
    tmp.replace(path)


def status_of(bird: dict, curation: dict) -> str:
    """approved/rejected/excluded while the decision matches the images, else pending.

    Exclusions hold across re-renders: they are about the species, not the art."""
    entry = curation["species"].get(bird["slug"])
    if not entry:
        return "pending"
    if entry["status"] == "excluded" or entry.get("hash") == bird["hash"]:
        return entry["status"]
    return "pending"


def rerender_queue(curation: dict, ill: Path = ILL) -> list[dict]:
    """Rejected birds whose images are still the rejected ones (for autogen)."""
    out = []
    for slug, entry in curation["species"].items():
        if entry.get("status") == "rejected" and entry.get("hash") == art_hash(slug, ill):
            out.append({"slug": slug, "sci": entry["sci"], "com": entry.get("com", ""),
                        "note": entry.get("note", "")})
    return out


def decide(slugs: list[str], status: str, note: str = "", *, birds: list[dict], curation: dict) -> list[str]:
    by_slug = {b["slug"]: b for b in birds}
    unknown = [s for s in slugs if s not in by_slug]
    if unknown:
        raise SystemExit("not a local species with both poses: " + ", ".join(unknown))
    now = datetime.datetime.now().isoformat(timespec="seconds")
    for slug in slugs:
        b = by_slug[slug]
        entry = {"status": status, "hash": b["hash"], "sci": b["sci"], "com": b["com"], "at": now}
        if note:
            entry["note"] = note
        curation["species"][slug] = entry
    return slugs


# ---------------------------------------------------------------- review sheet

def thumb_uri(path: Path, size: int = 300) -> str:
    from PIL import Image
    im = Image.open(path).convert("RGBA")
    im.thumbnail((size, size))
    paper = Image.new("RGB", im.size, (250, 248, 243))
    paper.paste(im, mask=im)
    buf = io.BytesIO()
    paper.save(buf, "JPEG", quality=82)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


SHEET_CSS = """
:root{--paper:#faf8f3;--ink:#2b2622;--muted:#7a716a;--line:#e3ddd2;--ok:#2f7d4f;--bad:#b0412e;--ex:#6b6b6b}
@media (prefers-color-scheme:dark){:root{--paper:#1d1b19;--ink:#ece6dc;--muted:#a39a90;--line:#38332e}}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.4 system-ui,sans-serif}
header{padding:20px 16px 8px}h1{font-size:22px;margin:0 0 4px}header p{margin:0;color:var(--muted)}
h2{font-size:16px;margin:24px 16px 8px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px;padding:0 16px}
.bird{border:1px solid var(--line);border-radius:10px;padding:10px;background:rgba(255,255,255,.35)}
.poses{display:flex;gap:6px}.poses img{width:50%;border-radius:6px;background:#faf8f3}
.name{margin:8px 0 2px;font-weight:600}.sci{color:var(--muted);font-style:italic;font-size:13px}
.note{font-size:13px;color:var(--bad);margin-top:4px}
.acts{display:flex;gap:6px;margin-top:8px;flex-wrap:wrap}
.acts button{flex:1;padding:6px;border:1px solid var(--line);border-radius:6px;background:transparent;color:var(--ink);cursor:pointer}
.acts input{flex-basis:100%;padding:6px;border:1px solid var(--line);border-radius:6px;background:transparent;color:var(--ink)}
.bird[data-pick=approve]{outline:2px solid var(--ok)}.bird[data-pick=reject]{outline:2px solid var(--bad)}
.bird[data-pick=exclude]{outline:2px solid var(--ex);opacity:.6}
#out{position:sticky;bottom:0;background:var(--paper);border-top:1px solid var(--line);padding:10px 16px}
#out textarea{width:100%;height:84px;font:12px ui-monospace,monospace;background:transparent;color:var(--ink);
  border:1px solid var(--line);border-radius:6px;padding:6px}
"""

SHEET_JS = """
const out=document.querySelector('#cmds');
function sh(s){return "'"+s.replace(/'/g,"'\\\\''")+"'"}
function render(){
  const pick={approve:[],exclude:[]},rej=[];
  document.querySelectorAll('.bird[data-pick]').forEach(b=>{
    const p=b.dataset.pick,s=b.dataset.slug;
    if(p==='reject'){const n=b.querySelector('input').value.trim();rej.push('avian-regional reject '+s+' --note '+sh(n||'wrong plumage'))}
    else if(pick[p])pick[p].push(s)});
  const lines=[];
  if(pick.approve.length)lines.push('avian-regional approve '+pick.approve.join(' '));
  if(pick.exclude.length)lines.push('avian-regional exclude '+pick.exclude.join(' '));
  out.value=lines.concat(rej).join('\\n');
}
document.querySelectorAll('.bird').forEach(b=>{
  b.querySelectorAll('button').forEach(btn=>btn.addEventListener('click',()=>{
    b.dataset.pick=b.dataset.pick===btn.value?'':btn.value;
    if(!b.dataset.pick)delete b.dataset.pick;render()}));
  b.querySelector('input').addEventListener('input',render);
});
"""


def sheet_html(birds: list[dict], curation: dict, ill: Path = ILL, generated: str = "") -> str:
    groups = {s: [] for s in ("pending", "rejected", "approved", "excluded")}
    for b in birds:
        groups[status_of(b, curation)].append(b)
    titles = {"pending": "Pending review", "rejected": "Rejected, waiting for autogen to re-render",
              "approved": "Approved", "excluded": "Excluded from the bundle"}
    parts = [f"<!doctype html><html lang=en><head><meta charset=utf-8>"
             f"<meta name=viewport content='width=device-width,initial-scale=1'>"
             f"<title>Regional bundle review</title><style>{SHEET_CSS}</style></head><body>"
             f"<header><h1>Regional bundle review</h1><p>{len(birds)} local species · "
             + " · ".join(f"{len(groups[s])} {s}" for s in groups)
             + f" · {html.escape(generated)}</p></header>"]
    for status, items in groups.items():
        if not items:
            continue
        parts.append(f"<h2>{titles[status]} ({len(items)})</h2><div class=grid>")
        for b in items:
            entry = curation["species"].get(b["slug"], {})
            perched, flight = pose_files(b["slug"], ill)
            note = (f"<div class=note>{html.escape(entry['note'])}</div>"
                    if status == "rejected" and entry.get("note") else "")
            parts.append(
                f"<div class=bird data-slug='{html.escape(b['slug'])}'>"
                f"<div class=poses><img alt='' src='{thumb_uri(perched)}'><img alt='' src='{thumb_uri(flight)}'></div>"
                f"<div class=name>{html.escape(b['com'] or b['sci'])}</div>"
                f"<div class=sci>{html.escape(b['sci'])} · {html.escape(b['slug'])}</div>{note}"
                f"<div class=acts><button value=approve>approve</button><button value=reject>reject</button>"
                f"<button value=exclude>exclude</button><input placeholder='what is wrong (for a reject)'></div></div>")
        parts.append("</div>")
    parts.append("<div id=out><textarea id=cmds readonly placeholder='Pick approve / reject / exclude; "
                 "the commands to run on the station appear here.'></textarea></div>"
                 f"<script>{SHEET_JS}</script></body></html>")
    return "".join(parts)


# ---------------------------------------------------------------------- export

@contextlib.contextmanager
def library_lock(path: Path = LOCK):
    """Hold the generation lock while copying, so autogen cannot swap a pose mid-copy."""
    with path.open("r+") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("generation is running (autogen or the Atlas button); try again shortly")
        yield


def patch_manifest(src: Path, dst: Path, cfg: dict) -> dict:
    """Rewrite manifest.json with the bundle.json metadata, keeping the exporter's
    member order, stored compression and fixed timestamps."""
    with zipfile.ZipFile(src) as z:
        infos = z.infolist()
        m = json.loads(z.read("manifest.json"))
        m["name"] = cfg["name"]
        m["description"] = cfg["description"]
        m["coverage"] = {"label": cfg["region_label"], "region_codes": cfg["region_codes"], "type": "selection"}
        m["license"]["spdx"] = cfg["license"]
        m["attribution"] = {"creator": cfg["creator"]}
        if cfg.get("source_url"):
            m["attribution"]["source_url"] = cfg["source_url"]
        m["provenance"] = {"method": cfg["method"], "model": cfg.get("model"), "review": "contributor-reviewed"}
        body = json.dumps(m, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        with zipfile.ZipFile(dst, "x") as out:
            for i in infos:
                n = zipfile.ZipInfo(i.filename, date_time=i.date_time)
                n.compress_type, n.external_attr, n.create_system = i.compress_type, i.external_attr, i.create_system
                out.writestr(n, body if i.filename == "manifest.json" else z.read(i.filename))
    return m


def claims(cfg: dict, species_count: int) -> dict:
    """What the hosted form submits, in bundle_submission_check's shape."""
    return {
        "name": cfg["name"], "species_count": species_count, "style_name": cfg["style_name"],
        "license_spdx": cfg["license"], "region_label": cfg["region_label"],
        "region_codes": cfg["region_codes"],
        "provenance": {"method": cfg["method"], "model": cfg.get("model"), "review": "contributor-reviewed"},
        "attribution": {"creator": cfg["creator"], "source_url": cfg.get("source_url")},
    }


def export(out_dir: Path, *, draft: bool, cfg: dict, birds: list[dict], curation: dict) -> dict:
    wanted = {"approved", "pending"} if draft else {"approved"}
    chosen = [b for b in birds if status_of(b, curation) in wanted]
    if not chosen:
        raise SystemExit("nothing approved yet: review the sheet, or use --draft for a preview ZIP")
    cover_slug = slugify(cfg["cover"])
    if cover_slug not in {b["slug"] for b in chosen}:
        raise SystemExit(f"cover bird {cfg['cover']} is not among the exported species")
    out_dir.mkdir(parents=True, exist_ok=True)
    name = cfg["file_name"].removesuffix(".zip") + ("-draft" if draft else "") + ".zip"
    final = out_dir / name
    with tempfile.TemporaryDirectory(prefix="avian-regional-") as tmp:
        tmp = Path(tmp)
        stage = tmp / "illustrations"
        stage.mkdir()
        with library_lock():
            for b in birds:
                if b in chosen:
                    if art_hash(b["slug"]) != b["hash"]:
                        raise SystemExit(f"{b['slug']} changed while exporting; run export again")
                    for f in pose_files(b["slug"]):
                        shutil.copy2(f, stage / f.name)
        private_lock = tmp / "lock"
        private_lock.touch()
        raw = tmp / "raw.zip"
        # The same trusted taxonomy export.php hands the exporter.
        catalogs = [c for c in (CATALOG, HERE / "bundle-taxonomy-v1.json") if c.is_file()]
        run = subprocess.run([sys.executable, "-I", str(HERE / "bundle_export.py"), "--illustrations", str(stage),
                              "--generation-lock", str(private_lock), "--output", str(raw),
                              *[arg for c in catalogs for arg in ("--catalog", str(c))]],
                             capture_output=True, text=True)
        if run.returncode:
            raise SystemExit("bundle_export.py failed: " + (run.stdout + run.stderr).strip()[-600:])
        patched = tmp / name
        manifest = patch_manifest(raw, patched, cfg)
        check = subprocess.run([sys.executable, "-I", str(HERE / "bundle_validate.py"), str(patched)],
                               capture_output=True, text=True)
        verdict = json.loads(check.stdout or "{}")
        if check.returncode or verdict.get("ok") is not True:
            raise SystemExit("bundle_validate.py rejected the ZIP: " + (check.stdout + check.stderr).strip()[-600:])
        import bundle_submission_check
        bundle_submission_check.check_claims(patched, {"metadata": claims(cfg, len(manifest["species"]))})
        shutil.move(patched, final)
    os.chmod(final, 0o644)
    return {"zip": str(final), "sha256": verdict["archive_sha256"], "species": verdict["species"],
            "objects": verdict["objects"], "draft": draft,
            "form": {"region_group": cfg["region_group"], "style_category": cfg["style_category"],
                     "cover": cfg["cover"]}}


# ------------------------------------------------------------------------- cli

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    p = sub.add_parser("sheet")
    p.add_argument("--out", type=Path, default=DEFAULT_SHEET)
    p = sub.add_parser("approve")
    p.add_argument("slugs", nargs="*")
    p.add_argument("--pending", action="store_true")
    p = sub.add_parser("reject")
    p.add_argument("slug")
    p.add_argument("--note", required=True)
    p = sub.add_parser("exclude")
    p.add_argument("slugs", nargs="+")
    p = sub.add_parser("export")
    p.add_argument("--out", type=Path, default=WEB_DIR)
    p.add_argument("--draft", action="store_true", help="include pending birds too (a preview, not for upload)")
    a = ap.parse_args(argv)

    birds = local_species()
    curation = load_curation()
    if a.cmd == "status":
        counts: dict[str, list[str]] = {}
        for b in birds:
            counts.setdefault(status_of(b, curation), []).append(b["slug"])
        print(f"{len(birds)} local species")
        for s in ("pending", "approved", "rejected", "excluded"):
            print(f"  {s:9} {len(counts.get(s, []))}")
        if counts.get("pending"):
            print("pending: " + " ".join(counts["pending"]))
        return 0
    if a.cmd == "sheet":
        a.out.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("built %Y-%m-%d %H:%M")
        tmp = a.out.with_suffix(".tmp")
        tmp.write_text(sheet_html(birds, curation, generated=stamp))
        os.chmod(tmp, 0o644)
        tmp.replace(a.out)
        print(f"wrote {a.out}")
        return 0
    if a.cmd == "export":
        print(json.dumps(export(a.out, draft=a.draft, cfg=json.loads(CONFIG.read_text()),
                                birds=birds, curation=curation), indent=1, ensure_ascii=False))
        return 0
    if a.cmd == "approve":
        slugs = a.slugs + ([b["slug"] for b in birds if status_of(b, curation) == "pending"] if a.pending else [])
        if not slugs:
            raise SystemExit("name species to approve, or pass --pending")
        done = decide(slugs, "approved", birds=birds, curation=curation)
    elif a.cmd == "reject":
        done = decide([a.slug], "rejected", a.note, birds=birds, curation=curation)
    else:
        done = decide(a.slugs, "excluded", birds=birds, curation=curation)
    save_curation(curation)
    print({"approve": "approved", "reject": "rejected", "exclude": "excluded"}[a.cmd] + ": " + " ".join(done))
    return 0


if __name__ == "__main__":
    sys.exit(main())
