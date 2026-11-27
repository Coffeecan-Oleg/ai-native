#!/usr/bin/env python3
"""
Акметрон — Протокол встречи: рендер HTML → PDF (A4) через Playwright Chromium.

Usage:
    python render_pdf.py <content.js> <output.pdf> [photo1 photo2 ...]

  <content.js> — слой контента (window.PROTOCOL).
  <output.pdf> — путь результата.
  [photoN]     — НЕОБЯЗАТЕЛЬНЫЕ фото/снимки доски (png/jpg/webp). Если переданы,
                 добавляются галереей «Материалы со встречи» в конце протокола.

Логотип (assets/logo-mark.png) и фото встраиваются как data-URI, поэтому не
«теряются» при печати. Вёрстка, шрифты и рендерер фиксированы и лежат рядом.
"""
import sys, os, pathlib, tempfile, base64, mimetypes, json
from playwright.sync_api import sync_playwright

ASSETS = pathlib.Path(__file__).resolve().parent
CHROMIUM = os.environ.get("AKMETRON_CHROMIUM", "")


def data_uri(path: pathlib.Path) -> str:
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def build_html(content_js_path: str, photo_paths) -> str:
    css = (ASSETS / "protokol.css").read_text(encoding="utf-8")
    renderer = (ASSETS / "renderer.js").read_text(encoding="utf-8")
    content = pathlib.Path(content_js_path).read_text(encoding="utf-8")
    base = ASSETS.as_uri() + "/"

    inject = ""
    logo = ASSETS / "logo-mark.png"
    if logo.exists():
        inject += f"window.__PROTO_LOGO__ = {data_uri(logo)!r};\n"

    photos = []
    for i, p in enumerate(photo_paths, 1):
        pp = pathlib.Path(p)
        if not pp.exists():
            print(f"ВНИМАНИЕ: фото не найдено, пропускаю: {p}")
            continue
        photos.append({"src": data_uri(pp), "cap": f"Снимок со встречи {i}"})
    inject += "window.__PROTO_PHOTOS__ = " + json.dumps(photos, ensure_ascii=False) + ";\n"

    return f"""<!DOCTYPE html>
<html lang="ru"><head>
<meta charset="utf-8">
<base href="{base}">
<style>{css}</style>
<script>{inject}</script>
</head>
<body class="proto-doc">
<div id="proto-stack" class="proto-stack"></div>
<script>{content}</script>
<script>{renderer}</script>
</body></html>"""


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    content_js, out_pdf = sys.argv[1], sys.argv[2]
    photos = sys.argv[3:]
    html = build_html(content_js, photos)

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
            # дождаться шрифтов, изображений и завершения разбивки рендерером
            page.evaluate("async () => { await document.fonts.ready; "
                          "await Promise.all(Array.from(document.images).map(i => "
                          "i.complete ? 1 : new Promise(r => { i.onload = i.onerror = r; }))); }")
            page.wait_for_function("window.__PROTO_DONE__ === true", timeout=15000)
            page.wait_for_timeout(200)
            n = page.eval_on_selector_all(".proto-page", "els => els.length")
            page.pdf(path=out_pdf, format="A4", print_background=True,
                     margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
            browser.close()
        print(f"OK: {n} pages -> {out_pdf}")
    finally:
        os.unlink(tmp)


if __name__ == "__main__":
    main()
