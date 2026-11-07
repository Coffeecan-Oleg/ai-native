#!/usr/bin/env python3
"""
Акметрон — Даташит: рендер HTML → PDF (A4) через Playwright Chromium.

Usage:
    python render_pdf.py <content.js> <output.pdf> <device_photo>

ОБЯЗАТЕЛЬНО: <device_photo> — путь к реальной фотографии прибора (png/jpg/webp).
Даташит НЕ формируется без фотографии прибора. Логотип (assets/logo-mark.png)
и фото встраиваются в HTML как data-URI, поэтому не «теряются» при печати.
Всё остальное (вёрстка, шрифты, рендерер) фиксировано и лежит рядом в assets/.
"""
import sys, os, pathlib, tempfile, base64, mimetypes
from playwright.sync_api import sync_playwright

ASSETS = pathlib.Path(__file__).resolve().parent
CHROMIUM = os.environ.get("AKMETRON_CHROMIUM", "")


def data_uri(path: pathlib.Path) -> str:
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def build_html(content_js_path: str, photo_path: str) -> str:
    css = (ASSETS / "datasheet.css").read_text(encoding="utf-8")
    renderer = (ASSETS / "renderer.js").read_text(encoding="utf-8")
    photoslot = (ASSETS / "photoslot.js").read_text(encoding="utf-8")
    content = pathlib.Path(content_js_path).read_text(encoding="utf-8")
    base = ASSETS.as_uri() + "/"

    inject = ""
    logo = ASSETS / "logo-mark.png"
    if logo.exists():
        inject += f'window.__AKM_LOGO__ = {data_uri(logo)!r};\n'
    inject += f'window.__AKM_PHOTO__ = {data_uri(pathlib.Path(photo_path))!r};\n'

    return f"""<!DOCTYPE html>
<html lang="ru"><head>
<meta charset="utf-8">
<base href="{base}">
<style>{css}</style>
<script>{photoslot}</script>
<script>{inject}</script>
</head>
<body class="ds-doc">
<div id="ds-stack" class="ds-stack"></div>
<script>{content}</script>
<script>{renderer}</script>
</body></html>"""


def main():
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)
    content_js, out_pdf, photo = sys.argv[1], sys.argv[2], sys.argv[3]
    if not pathlib.Path(photo).exists():
        print(f"ОШИБКА: фотография прибора не найдена: {photo}\n"
              f"Даташит не формируется без реального фото прибора.")
        sys.exit(2)
    html = build_html(content_js, photo)

    with tempfile.NamedTemporaryFile("w", suffix=".html", dir=ASSETS,
                                     delete=False, encoding="utf-8") as f:
        tmp = f.name
        f.write(html)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(**({"executable_path": CHROMIUM} if CHROMIUM else {}))
            page = browser.new_page()
            page.route("**/*", lambda route: route.continue_() if route.request.url.startswith("file:") else route.abort())
            page.goto(pathlib.Path(tmp).as_uri(), wait_until="networkidle")
            # дождаться шрифтов и полной загрузки/декодирования всех изображений
            page.evaluate("async () => { await document.fonts.ready; "
                          "await Promise.all(Array.from(document.images).map(i => "
                          "i.complete ? 1 : new Promise(r => { i.onload = i.onerror = r; }))); }")
            page.wait_for_timeout(300)
            n = page.eval_on_selector_all(".ds-page", "els => els.length")
            page.pdf(path=out_pdf, format="A4", print_background=True,
                     margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
            browser.close()
        print(f"OK: {n} pages -> {out_pdf}")
    finally:
        os.unlink(tmp)


if __name__ == "__main__":
    main()
