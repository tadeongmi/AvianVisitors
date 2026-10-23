#!/usr/bin/env python3
"""Compare original and optimized cutouts at station/frame display sizes."""
import argparse
import hashlib
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw


def resize(image, limit):
    scale = min(1, limit / max(image.size))
    size = tuple(max(1, round(value * scale)) for value in image.size)
    return image.convert('RGBa').resize(size, Image.Resampling.LANCZOS).convert('RGBA')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--release', type=Path, required=True)
    args = parser.parse_args()
    study = json.loads((args.source / 'exports/local-study-manifest.json').read_text())
    provenance = json.loads((args.release / 'provenance.json').read_text())
    originals = {row['id']: row for row in study['illustrations']}
    results, seen = [], set()
    for item in provenance['illustration_choices']:
        if item['sha256'] in seen:
            continue
        seen.add(item['sha256'])
        row = originals[item['artwork_id']]
        raw = (args.release / 'objects' / (item['sha256'] + '.png')).read_bytes()
        if hashlib.sha256(raw).hexdigest() != item['sha256']:
            raise ValueError('Derivative changed after packaging')
        with Image.open(args.source / row['file']) as source:
            original = source.convert('RGBA')
        with Image.open(args.release / 'objects' / (item['sha256'] + '.png')) as source:
            optimized = source.convert('RGBA')
        metrics = []
        for side in (560, 1024):
            expected = resize(original, side)
            actual = optimized.convert('RGBa').resize(expected.size, Image.Resampling.LANCZOS).convert('RGBA')
            for ground in ((245, 243, 235, 255), (32, 35, 30, 255)):
                a = np.asarray(Image.alpha_composite(Image.new('RGBA', expected.size, ground), expected), dtype=np.float32)[:, :, :3]
                b = np.asarray(Image.alpha_composite(Image.new('RGBA', expected.size, ground), actual), dtype=np.float32)[:, :, :3]
                delta = np.abs(a - b)
                mse = float(np.mean(delta ** 2))
                metrics.append({'side': side, 'ground': 'light' if ground[0] > 100 else 'dark', 'mean_channel_difference': float(delta.mean()), 'p99_channel_difference': float(np.percentile(delta, 99)), 'psnr_db': 99 if not mse else 10 * math.log10(255 ** 2 / mse)})
        results.append({'artwork_id': item['artwork_id'], 'original_sha256': row['sha256'], 'sha256': item['sha256'], 'metrics': metrics})
        if len(results) % 100 == 0:
            print(f'Compared {len(results)} originals and derivatives', flush=True)
    worst = sorted(results, key=lambda row: min(m['psnr_db'] for m in row['metrics']))[:8]
    report = {'unique_images_compared': len(results), 'display_sides': [560, 1024], 'backgrounds': ['light', 'dark'], 'minimum_psnr_db': min(m['psnr_db'] for row in results for m in row['metrics']), 'maximum_mean_channel_difference': max(m['mean_channel_difference'] for row in results for m in row['metrics']), 'worst_examples': [r['artwork_id'] for r in worst], 'images': results}
    (args.release / 'quality-report.json').write_text(json.dumps(report, indent=2) + '\n')
    sheet = Image.new('RGB', (1120, len(worst) * 300), '#f5f3eb')
    draw = ImageDraw.Draw(sheet)
    for number, item in enumerate(worst):
        row = originals[item['artwork_id']]
        for column, path in enumerate((args.source / row['file'], args.release / 'objects' / (item['sha256'] + '.png'))):
            with Image.open(path) as opened:
                image = resize(opened.convert('RGBA'), 260)
            left, top = column * 560 + (560 - image.width) // 2, number * 300 + 30
            sheet.paste(image, (left, top), image)
            draw.text((column * 560 + 12, number * 300 + 10), ('Original / ' if column == 0 else 'Optimized / ') + item['artwork_id'], fill='#222222')
    sheet.save(args.release / 'quality-comparison.jpg', quality=95)
    print(json.dumps({k: v for k, v in report.items() if k != 'images'}, indent=2))


if __name__ == '__main__':
    main()
