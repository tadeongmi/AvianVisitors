# Regional bundle — from autogen to a catalog submission

Autogen gives every new species its artwork. This folder turns the station's
own birds (everything that is not in the included woodblock set) into a bundle
for [the catalog](https://avianvisitors.com/bundles), with a human look at each
bird first.

```
autogen renders  ->  avian-regional sheet  ->  approve / reject / exclude  ->  avian-regional export  ->  upload
                          ^                         |
                          +---- autogen re-renders a rejected bird with your note
```

## Review

```bash
avian-regional sheet        # writes http://<station>/regional/
avian-regional status
```

The sheet shows both poses of every local species, pending ones first. Click
approve, reject (with a note on what is wrong) or exclude; the page writes the
commands to run, for example:

```bash
avian-regional approve furnarius-rufus pitangus-sulphuratus
avian-regional reject pygochelidon-cyanoleuca --note 'Underparts entirely white, no chestnut breast band.'
avian-regional exclude chloris-chloris
```

- **approve**: the images as they are now go in the bundle.
- **reject**: on its next run autogen re-renders both poses, adding the note to
  the species prompt. The bird then shows up as pending again.
- **exclude**: kept on the station, never submitted (a doubtful detection, say).

Each decision is pinned to the sha256 of both pose files, so a bird whose images
change (re-rendered, or replaced by hand) goes back to pending. Exclusions hold
across re-renders.

Decisions live in `/var/lib/avian-autogen/curation.json`, beside autogen's own
state and outside the checkout, so they survive updates and never block the
updater. `avian-regional` runs as the web user, like autogen.

## Export

```bash
avian-regional export           # approved birds only
avian-regional export --draft   # approved + pending, a preview not meant for upload
```

It copies the approved birds under the generation lock, runs the station's own
`bundle_export.py` on just those, writes the metadata from `bundle.json` into
`manifest.json`, then runs `bundle_validate.py` and `bundle_submission_check.py`
with the values the upload form will send. The ZIP lands next to the sheet, at
`http://<station>/regional/<file_name>`.

## Upload

At avianvisitors.com/bundles, sign in with GitHub and drop the ZIP. The form
fills in from the manifest; style, region and license must stay as they are
(the site rejects a mismatch). Set by hand: cover bird, region group and style
category, as `bundle.json` lists them and `export` prints.

Uploading is manual: the hosted API accepts only a signed-in browser session.

## `bundle.json`

Name, description, region label and ISO 3166-2 codes, license, creator credit,
source link, provenance (method and model), cover bird and the ZIP's file name.
The cover must be one of the exported species.
