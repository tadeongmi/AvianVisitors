#!/usr/bin/env python3
"""Build a source-bound historical artwork release; never publish it.

The approved study remains immutable. Station derivatives use deterministic
Pillow RGBA resampling, never generative reconstruction. Every derivative links
back to the approved original and the library plate in provenance.json.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
from collections import defaultdict
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'avian/scripts'))
import bundle_manager as manager

PACK_ID = 'official-birds-of-america'
RIGHTS_ID = 'LicenseRef-NoC-US'
RIGHTS_URI = 'http://rightsstatements.org/vocab/NoC-US/1.0/'
SOURCE_URL = 'https://digital.library.pitt.edu/collection/audubons-birds-america'
# BirdNET station spelling/labels; preserve historical and modern names in the
# source metadata. These are explicit compatibility aliases, not a new taxonomy.
ALIASES = {
    'Astur cooperii': 'Accipiter cooperii',
    'Astur atricapillus': 'Accipiter gentilis',
    'Hylatomus pileatus': 'Dryocopus pileatus',
    'Larus philadelphia': 'Chroicocephalus philadelphia',
    'Leuconotopicus borealis': 'Dryobates borealis',
    'Nannopterum auritus': 'Nannopterum auritum',
    'Passerculus henslowii': 'Centronyx henslowii',
    'Passerella arborea': 'Spizelloides arborea',
    'Porphyrio martinicus': 'Porphyrio martinica',
}
# Retain these in the historical collection, without advertising them as
# North American station species. Other identified birds in a mixed plate stay.
REGIONAL_EXCLUSIONS = {'Athene noctua', 'Phoebetria fusca'}
BLUEBIRD = 'plate-36-pitt-bluebird-repaired'
BLUEBIRD_SHA = 'e58bc656fbad5758f48aa13bfc581e03c89a834340ee47f82ce22bd4b5327a62'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(manager.canonical_json(value))


def station_png(path, expected, long_side=1280):
    raw = path.read_bytes()
    if digest(raw) != expected:
        raise ValueError(f'Approved original changed: {path.name}')
    with Image.open(io.BytesIO(raw)) as opened:
        if opened.mode != 'RGBA':
            raise ValueError(f'Expected original RGBA: {path.name}')
        original = opened.copy()
    # RGBA -> RGBa explicitly premultiplies before interpolation, protecting
    # pale feathers and transparent edges against a dark resampling fringe.
    limit = long_side
    while True:
        scale = min(1, limit / max(original.size))
        size = tuple(max(1, round(value * scale)) for value in original.size)
        image = original if size == original.size else original.convert('RGBa').resize(size, Image.Resampling.LANCZOS).convert('RGBA')
        image = image.copy()
        image.paste((0, 0, 0, 0), mask=image.getchannel('A').point(lambda a: 255 if a == 0 else 0))
        output = io.BytesIO()
        image.save(output, 'PNG', compress_level=9)
        encoded = output.getvalue()
        if len(encoded) <= manager.IMAGE_MAX:
            return encoded, list(size)
        limit = int(limit * .85)
        if limit < 800:
            raise ValueError(f'Artwork cannot meet existing station bounds: {path.name}')


def release_index(study):
    rows = {row['id']: row for row in study['illustrations']}
    index = defaultdict(list)
    decisions = []
    for scientific, ids in study['variants_by_species'].items():
        if scientific in REGIONAL_EXCLUSIONS:
            decisions.append({'scientific_name': scientific, 'reason': 'Historical plate retained; not selected for the North American station index.', 'ids': ids})
            continue
        station_name = ALIASES.get(scientific, scientific)
        for artwork_id in ids:
            if artwork_id == BLUEBIRD:
                if rows[artwork_id]['sha256'] != BLUEBIRD_SHA:
                    raise ValueError('Bluebird identity evidence no longer matches approved art')
                continue
            if artwork_id not in index[station_name]:
                index[station_name].append(artwork_id)
    index['Sialia sialis'].append(BLUEBIRD)
    # The plate-wide crosswalk describes the hunted bird as the Eastern Bluebird
    # copied from plate 113. Its isolated variant must not inherit Cooper's Hawk.
    composition = 'plate-36-pitt-composition-repaired'
    if composition in rows:
        index['Sialia sialis'].append(composition)
    decisions.append({'id': BLUEBIRD, 'original_sha256': BLUEBIRD_SHA, 'scientific_name': 'Sialia sialis', 'evidence': rows[BLUEBIRD]['species_candidates'][0]['notes_de'], 'source_url': rows[BLUEBIRD]['source_url']})
    return rows, dict(sorted(index.items())), decisions


def build(source, output):
    study_path = source / 'exports/local-study-manifest.json'
    study = json.loads(study_path.read_text())
    if study['target_coverage']['uncovered_target_units'] or study['illustration_count'] != 831:
        raise ValueError('Expected the completed 831-image reviewed collection')
    rows, index, decisions = release_index(study)
    original_rights = study['source_rights']['source_collection']
    if original_rights['rights_statement_uri'] != RIGHTS_URI or original_rights['rights_statement_is_license']:
        raise ValueError('Source rights changed')
    output.mkdir(parents=True, exist_ok=True)
    objects = output / 'objects'
    objects.mkdir(exist_ok=True)
    cache_path = output / 'derivative-cache-1280.json'
    derivatives = {}
    by_original = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    for cached in by_original.values():
        cached_path = objects / (cached['sha256'] + '.png')
        cached_raw = cached_path.read_bytes()
        if len(cached_raw) != cached['bytes'] or digest(cached_raw) != cached['sha256']:
            raise ValueError('Cached derivative no longer matches its checksum')
    referenced = {item for ids in index.values() for item in ids}
    for ordinal, (artwork_id, row) in enumerate(rows.items(), 1):
        # Keep unassigned historical artwork in the provenance census, but do
        # not attach it to a guessed modern station species.
        if artwork_id not in referenced:
            continue
        if digest((source / row['file']).read_bytes()) != row['sha256']:
            raise ValueError('Source changed after approval')
        if row['source_kind'] != 'pittsburgh' or row['review_status'] != 'checked':
            raise ValueError('Unapproved or alternate-source artwork')
        if row['sha256'] not in by_original:
            encoded, size = station_png(source / row['file'], row['sha256'])
            sha = digest(encoded)
            target = objects / f'{sha}.png'
            target.write_bytes(encoded)
            by_original[row['sha256']] = {'sha256': sha, 'bytes': len(encoded), 'size': size}
        derivatives[artwork_id] = by_original[row['sha256']]
        write_json(cache_path, by_original)
        if ordinal % 50 == 0:
            print(f'Prepared {ordinal}/{len(rows)} source records', flush=True)
    species = []
    mappings = []
    for scientific, ids in index.items():
        poses = []
        seen = set()
        # Full support compositions first, then individual alternatives. The
        # choice number is an illustration number, not a claim of flight.
        ids = sorted(ids, key=lambda item: ('composition' not in item, rows[item]['plate'], item))
        common = ''
        for artwork_id in ids:
            row, derivative = rows[artwork_id], derivatives[artwork_id]
            common = common or next((s.get('current_common_name') or '' for s in row['species_candidates'] if ALIASES.get(s.get('scientific_name'), s.get('scientific_name')) == scientific), '')
            if derivative['sha256'] in seen:
                continue
            seen.add(derivative['sha256'])
            pose = f'variant-{len(poses) + 1}'
            poses.append({'id': pose, 'file': manager.pose_path(scientific, pose), 'sha256': derivative['sha256'], 'bytes': derivative['bytes']})
            mappings.append({'scientific_name': scientific, 'pose': pose, 'artwork_id': artwork_id, **derivative})
        species.append({'scientific_name': scientific, 'common_name': common, 'poses': poses})
    manifest = {
        'format': manager.FORMAT_MANIFEST, 'format_version': 2,
        'id': PACK_ID, 'version': '1.0.0', 'name': 'Birds of America',
        'description': 'John James Audubon’s birds, with original flowers, branches and landscapes preserved. Multiple source-faithful illustration choices for North America.',
        'style': {'id': 'birds-of-america', 'name': 'Birds of America'},
        'coverage': {'type': 'region', 'label': 'North America', 'region_codes': ['US', 'CA', 'MX']},
        'species': species,
        'license': {'spdx': RIGHTS_ID, 'file': 'LICENSES/NoC-US.txt'},
        'attribution': {'creator': 'John James Audubon; Robert Havell; William Home Lizars', 'source_url': 'https://avianvisitors.com/bundles/birds-of-america'},
        'provenance': {'method': 'manual', 'review': 'human-reviewed'},
    }
    validated = manager.validate_manifest(manifest)
    raw = manager.canonical_json(manifest)
    manifest_hash = digest(raw)
    (output / 'manifests').mkdir(exist_ok=True)
    (output / 'manifests' / f'{manifest_hash}.json').write_bytes(raw)
    write_json(output / 'manifest.json', manifest)
    write_json(output / 'provenance.json', {
        'source_credit': study['source_credit'], 'source_collection_url': SOURCE_URL,
        'rights': study['source_rights'], 'study_manifest_sha256': digest(study_path.read_bytes()),
        'processing': {'operation': 'Premultiplied-alpha Lanczos resize; zero invisible RGB; metadata-free PNG', 'maximum_long_side': 1280, 'generative_editing': False},
        'station_aliases': ALIASES, 'release_decisions': decisions,
        'illustration_choices': mappings,
        'source_artwork': [{key: row[key] for key in ('id', 'plate', 'sha256', 'source_url', 'source_plate_sha256')} for row in rows.values()],
        'unassigned_artwork': sorted(set(rows) - referenced),
        'review': 'Assistant compared source pixels and repaired alpha; Teddy accepted the collection and requested publication. No claim of individual human approval for every file.',
    })
    chosen = []
    for name in ['Cardinalis cardinalis', 'Ardea herodias', 'Icterus galbula', 'Cyanocitta cristata', 'Meleagris gallopavo', 'Archilochus colubris']:
        bird = next((b for b in species if b['scientific_name'] == name), None)
        if bird:
            pose = bird['poses'][0]
            chosen.append({'scientific_name': name, 'common_name': bird['common_name'], 'pose': pose['id'], 'sha256': pose['sha256'], 'bytes': pose['bytes']})
    catalog = {
        'id': PACK_ID, 'version': '1.0.0', 'name': manifest['name'], 'description': manifest['description'],
        'creator': manifest['attribution']['creator'], 'repository_url': 'https://github.com/Twarner491/AvianVisitors',
        'coverage': {'label': 'North America', 'group': 'North America', 'region_codes': ['US', 'CA', 'MX']},
        'style': {**manifest['style'], 'category': 'Traditional', 'tags': ['audubon', 'historical', 'engraving', 'flowers', 'natural-history']},
        'species_count': len(species), 'species': [b['scientific_name'] for b in species], 'review': 'official',
        'availability': 'installable', 'manifest': {'url': manager.MANIFEST_BASE_URL + manifest_hash + '.json', 'sha256': manifest_hash, 'bytes': len(raw)},
        'archive_bytes': validated['total_bytes'], 'previews': chosen, 'license': RIGHTS_ID,
    }
    catalog_document = {'format': manager.FORMAT_CATALOG, 'format_version': 1, 'updated': '2026-10-12', 'object_base_url': manager.OBJECT_BASE_URL, 'packs': [catalog]}
    checked_catalog = manager.validate_catalog(catalog_document)
    manager.verify_catalog_manifest(checked_catalog['packs'][0], validated, manifest_hash)
    write_json(output / 'catalog.json', catalog_document)
    report = {'species': len(species), 'choices': len(mappings), 'unique_objects': len(by_original), 'object_bytes': sum(d['bytes'] for d in by_original.values()), 'manifest_sha256': manifest_hash, 'unassigned_artwork': sorted(set(rows) - referenced)}
    write_json(output / 'build-report.json', report)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    options = parser.parse_args()
    build(options.source.resolve(), options.output.resolve())
