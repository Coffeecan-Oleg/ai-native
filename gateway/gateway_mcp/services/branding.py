"""Opt-in local brand assets. No network requests or access-policy decisions."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'branding_assets'
ASSETS = {'akmetron.css': 'text/css', 'logo-mark.png': 'image/png', 'puzzle-pattern.svg': 'image/svg+xml'}
for family, weights in [('inter', (400, 500, 600)), ('mont', (700, 800, 900))]:
    for weight in weights:
        for script in ('cyr', 'latin'):
            ASSETS[f'fonts/{family}-{script}-{weight}.woff2'] = 'font/woff2'


def enabled():
    return os.getenv('GATEWAY_BRAND', '') == 'akmetron'


def head():
    if not enabled():
        return ''
    return '<link rel="icon" href="/branding/logo-mark.png"><link rel="stylesheet" href="/branding/akmetron.css">'


def logo():
    return '<img class="brand-mark" src="/branding/logo-mark.png" width="44" height="44" alt=""><span><span class="brand-name">Акметрон</span><span class="brand-product">Платформа · учебная песочница</span></span>'


def header():
    return '<header class="akm-entry-brand"><a class="brand" href="/join" aria-label="Акметрон — учебная Платформа">' + logo() + '</a></header>' if enabled() else ''
