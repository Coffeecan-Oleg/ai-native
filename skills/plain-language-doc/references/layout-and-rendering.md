## Как рисовать схемы

- **Сетка 20 px.** Все x, y, w, h кратны 20 (допустимо 10 для мелких отступов). viewBox шириной **≤ 700**, высота по содержимому.
- **Блоки** 180×60 или 220×70 (в колонках 160×50, в картах приложений 290×44). Заголовок блока 13 px жирный, подзаголовок 11 px серый. Заголовок ≤ 26 знаков, подзаголовок ≤ 30 — иначе вылезет.
- **Линии только прямые**: в `path` разрешены M, H, V (и L только по горизонтали/вертикали). Повороты — через промежуточную точку. Стрелка `marker-end`. Линия не должна проходить сквозь чужой блок; обходи по коридору между блоками.
- **Контейнеры** (`cont`) — серые скруглённые области с заголовком слева сверху; блоки внутри начинаются с y = верх + 50.
- Кегль ≥ 11 px везде. `font-size` — атрибутом на каждом `<text>` (иначе проверка и PDF не увидят размер).
- Один вопрос — одна схема. Если хочется вторую мысль — вторая схема.
- Цвет — смысл, не украшение: зелёный = работает, синий/фиолетовый = в работе / люди / важное, белый = план, розовый пунктир = не решено, серый контейнер = группа.

### Палитра

| kind | заливка | обводка | текст | смысл |
|---|---|---|---|---|
| cont | #EFEDE8 | #C8C4BA | #2A2A2A | контейнер, группа |
| box | #FFFFFF | #ADA9A0 | #2A2A2A | обычный блок, план |
| hum | #E7E5FB | #6F68D4 | #322C9E | в работе, люди, акцент |
| done | #D8F0E5 | #46A07A | #17663F | готово, работает |
| bad | #FBE9EC | #C0566F | #7E2B40 | проблема, не решено |

Подписи серые #757575, линии #6B6B6B. Шрифт Inter (Google Fonts, `<link>` в head), запасной Arial — в закрытом контейнере Google Fonts не грузится и Chromium подставит Liberation Sans, это нормально.

### Хелперы (вставить в build-скрипт)

```python
import html
C = {"cont": ("#EFEDE8", "#C8C4BA"), "box": ("#FFFFFF", "#ADA9A0"), "hum": ("#E7E5FB", "#6F68D4"),
     "done": ("#D8F0E5", "#46A07A"), "bad": ("#FBE9EC", "#C0566F")}
T = {"box": "#2A2A2A", "hum": "#322C9E", "done": "#17663F", "bad": "#7E2B40", "cont": "#2A2A2A"}
SUB, LINE = "#757575", "#6B6B6B"
def e(s): return html.escape(s, quote=True)
def rect(x, y, w, h, kind, rx=10, dash=False):
    f, s = C[kind]; d = ' stroke-dasharray="6 4"' if dash else ""
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{f}" stroke="{s}" stroke-width="1.5"{d}/>'
def txt(x, y, s, fs=12, fill=SUB, w=400, anchor="start"):
    a = f' text-anchor="{anchor}"' if anchor != "start" else ""
    return f'<text x="{x}" y="{y}" font-size="{fs}" font-weight="{w}" fill="{fill}"{a}>{e(s)}</text>'
def box(x, y, w, h, kind, title, sub=None, fs=13):
    cx = x + w // 2; out = [rect(x, y, w, h, kind)]
    if sub:
        out.append(txt(cx, y + round(h*0.42), title, fs, T[kind], 700, "middle"))
        out.append(txt(cx, y + round(h*0.74), sub, 11, SUB, 400, "middle"))
    else:
        out.append(txt(cx, y + h//2 + 5, title, fs, T[kind], 700, "middle"))
    return "\n".join(out)
def dbox(x, y, w, h, title, sub=None):  # пунктирный «не решено»
    out = [rect(x, y, w, h, "bad", 10, dash=True)]; cx = x + w // 2
    out.append(txt(cx, y + round(h*0.42) if sub else y + h//2 + 5, title, 13, T["bad"], 700, "middle"))
    if sub: out.append(txt(cx, y + round(h*0.74), sub, 11, SUB, 400, "middle"))
    return "\n".join(out)
def cont(x, y, w, h, title):
    return rect(x, y, w, h, "cont", 14) + "\n" + txt(x + 16, y + 26, title, 13, T["cont"], 700)
def path(d, color=LINE, marker=True, dash=False):
    m = ' marker-end="url(#ar)"' if marker else ""; dd = ' stroke-dasharray="6 4"' if dash else ""
    return f'<path d="{d}" stroke="{color}" stroke-width="1.5" fill="none"{m}{dd}/>'
DEFS = ('<defs><marker id="ar" markerWidth="9" markerHeight="9" refX="7" refY="4.5" orient="auto">'
        '<polygon points="0,0 9,4.5 0,9" fill="#6B6B6B"/></marker></defs>')
def svg(w, h, body, label):
    return (f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{e(label)}" '
            f'font-family="Inter, Arial, sans-serif">{DEFS}\n{body}\n</svg>')
def fig(s, caption): return f'<figure>{s}<figcaption>{e(caption)}</figcaption></figure>'
def card(title, text, kind=""): return f'<div class="card {kind}"><h3>{e(title)}</h3><p>{e(text)}</p></div>'
def column(x, title, items, kind):  # колонка роадмапа: [(title, sub), ...]
    h = 50 + len(items) * 58 + 8; out = [cont(x, 10, 200, h, title)]
    for i, (t, s) in enumerate(items): out.append(box(x + 20, 60 + i * 58, 160, 50, kind, t, s, 12))
    return "\n".join(out), h
```

Типовые схемы, которые хорошо работают: уровни/слои (3 горизонтальные полосы + стрелки с подписями), матрица «сейчас / строим / придём» × уровни, роадмап в 3 колонки (`column`), карта приложений в 2 контейнера (где что живёт), путь пользователя в 3 колонки (автор / робот / люди), «три способа» (строки шагов слева направо), железо (контейнеры-машины), данные (источники → слой коннекторов → хранилище).

### CSS документа

```css
@page { size: A4; margin: 14mm; }
:root{--ink:#2A2A2A;--sub:#757575;--line:#C8C4BA;--cont:#EFEDE8}
*{box-sizing:border-box}
body{margin:0;background:#FFF;color:var(--ink);font-family:Inter,Arial,sans-serif;font-size:14.5px;line-height:1.5}
.wrap{max-width:760px;margin:0 auto;padding:36px 28px 60px}
h1{font-size:30px;font-weight:700;line-height:1.15;margin:0 0 10px}
h2{font-size:20px;font-weight:700;line-height:1.25;margin:0 0 6px}
h3{font-size:14px;font-weight:700;margin:0 0 4px}
p{margin:0 0 10px}
.meta{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--sub);margin:0 0 14px}
.lead{font-size:16px;color:#444;margin:0 0 6px}
section{margin-top:34px}
figure{margin:12px 0 14px} figure svg{display:block;width:100%;height:auto}
figcaption{font-size:12px;color:var(--sub);margin-top:6px}
.cards{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:12px 0 6px}
.card{background:var(--cont);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.card p{margin:0;font-size:13.5px;color:#444}
.card.bad{background:#FBE9EC;border-color:rgba(192,86,111,.45)}
.card.done{background:#D8F0E5;border-color:rgba(70,160,122,.5)}
.card.hum{background:#E7E5FB;border-color:rgba(111,104,212,.45)}
table{border-collapse:collapse;width:100%;font-size:13px;margin:10px 0}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--sub);font-weight:600}
dl{margin:8px 0 0;font-size:13.5px} dt{font-weight:700;margin-top:8px} dd{margin:2px 0 0;color:#444}
@media print{.wrap{max-width:none;padding:0} figure,.card,table,.cards{break-inside:avoid} h1,h2,h3{break-after:avoid} p{orphans:3;widows:3} section.pb{break-before:page}}
```

Класс `pb` на `<section>` — принудительный разрыв страницы перед разделом; ставь его перед разделами с большими схемами, чтобы схема не ложилась на границу.

## check_svg.py — проверка наложений

Сохрани рядом со скриптом сборки и запускай на HTML. Ловит: перекрытие блоков, подпись шире блока, подписи друг на друге, линию сквозь блок (контейнеры исключены — линии внутри них законны), наклонные path, выход за viewBox, viewBox > 700, кегль < 11. Ширина текста оценивается как `len × кегль × 0.58` — для Inter/Arial это точно в ±10 %.

```python
#!/usr/bin/env python3
"""check_svg.py <файл.html|.svg> — ищет наложения на схемах."""
import sys, re
import xml.etree.ElementTree as ET
NS = '{http://www.w3.org/2000/svg}'
CW = 0.58
def num(v, d=0.0):
    try: return float(re.sub(r'[a-z%]+$', '', (v or '').strip()))
    except Exception: return d
def path_segs(d):
    out, diag = [], False; x = y = 0.0
    for c, arg in re.findall(r'([MLHVmlhv])([^MLHVmlhvZz]*)', d):
        v = [float(t) for t in re.findall(r'-?\d+\.?\d*', arg)]
        if not v: continue
        if c in 'Mm': x, y = (v[0], v[1]) if c == 'M' else (x + v[0], y + v[1])
        elif c in 'Hh': nx = v[0] if c == 'H' else x + v[0]; out.append((x, y, nx, y)); x = nx
        elif c in 'Vv': ny = v[0] if c == 'V' else y + v[0]; out.append((x, y, x, ny)); y = ny
        elif c in 'Ll':
            nx, ny = (v[0], v[1]) if c == 'L' else (x + v[0], y + v[1])
            if abs(nx - x) > 0.5 and abs(ny - y) > 0.5: diag = True
            out.append((x, y, nx, ny)); x, y = nx, ny
    return out, diag
def collect(svg):
    rects, texts, segs, bad = [], [], [], []
    for el in svg.iter():
        tag = el.tag.replace(NS, '')
        if tag == 'rect':
            rects.append([num(el.get('x')), num(el.get('y')), num(el.get('width')), num(el.get('height'))])
        elif tag == 'text':
            s = ' '.join(''.join(el.itertext()).split())
            if not s: continue
            fs = num(el.get('font-size'), 0) or 12.0; w = len(s) * fs * CW
            x, y = num(el.get('x')), num(el.get('y')); a = el.get('text-anchor') or 'start'
            if a == 'middle': x -= w / 2
            elif a == 'end': x -= w
            texts.append([x, y - fs * 0.80, w, fs * 1.15, s, fs])
        elif tag == 'line':
            segs.append((num(el.get('x1')), num(el.get('y1')), num(el.get('x2')), num(el.get('y2'))))
        elif tag == 'path':
            s, diag = path_segs(el.get('d', '')); segs.extend(s)
            if diag: bad.append(el.get('d', ''))
    return rects, texts, segs, bad
def over(a, b):
    return (min(a[0]+a[2], b[0]+b[2]) - max(a[0], b[0]), min(a[1]+a[3], b[1]+b[3]) - max(a[1], b[1]))
def holds(a, b, pad=0.5):
    return (a[0]-pad <= b[0] and a[1]-pad <= b[1] and a[0]+a[2]+pad >= b[0]+b[2] and a[1]+a[3]+pad >= b[1]+b[3])
def pierces(s, r, pad=2.0):
    x1, y1, x2, y2 = s; rx, ry, rw, rh = r[0]-pad, r[1]-pad, r[2]+2*pad, r[3]+2*pad
    if abs(x1-x2) < 0.5:
        if not (rx < x1 < rx+rw): return 0.0
        return max(0.0, min(max(y1, y2), ry+rh) - max(min(y1, y2), ry))
    if abs(y1-y2) < 0.5:
        if not (ry < y1 < ry+rh): return 0.0
        return max(0.0, min(max(x1, x2), rx+rw) - max(min(x1, x2), rx))
    return 0.0
def check(path):
    raw = open(path, encoding='utf-8').read(); problems = []
    for i, frag in enumerate(re.findall(r'<svg\b.*?</svg>', raw, re.S), 1):
        try: svg = ET.fromstring(frag)
        except ET.ParseError as ex: problems.append(f'схема {i}: не разбирается как XML — {ex}'); continue
        vb = [num(t) for t in (svg.get('viewBox') or '0 0 0 0').split()]
        rects, texts, segs, bad = collect(svg); tag = f'схема {i}'
        if len(vb) == 4 and vb[2] > 700: problems.append(f'{tag}: viewBox шире 700 ({vb[2]:.0f})')
        for d in bad: problems.append(f'{tag}: наклонная линия в path — только H и V: {d[:60]}')
        for a in range(len(rects)):
            for b in range(a+1, len(rects)):
                ox, oy = over(rects[a], rects[b])
                if ox > 1 and oy > 1 and not holds(rects[a], rects[b]) and not holds(rects[b], rects[a]):
                    problems.append(f'{tag}: блоки перекрываются на {ox:.0f}x{oy:.0f}px — {rects[a][:2]} и {rects[b][:2]}')
        for t in texts:
            if t[5] < 11: problems.append(f'{tag}: кегль {t[5]:.0f} < 11 у «{t[4][:30]}»')
            for r in rects:
                ox, oy = over(t, r)
                if ox > 1 and oy > 1 and not holds(r, t):
                    problems.append(f'{tag}: подпись «{t[4][:34]}» вылезает за блок на {ox:.0f}x{oy:.0f}px'); break
        for a in range(len(texts)):
            for b in range(a+1, len(texts)):
                ox, oy = over(texts[a], texts[b])
                if ox > 1 and oy > 1: problems.append(f'{tag}: подписи налезают — «{texts[a][4][:24]}» и «{texts[b][4][:24]}»')
        containers = {a for a in range(len(rects)) for b in range(len(rects)) if a != b and holds(rects[a], rects[b])}
        for s in segs:
            for k, r in enumerate(rects):
                if k in containers: continue
                p = pierces(s, r)
                if p > 5: problems.append(f'{tag}: линия проходит сквозь блок на {p:.0f}px (блок в {r[0]:.0f},{r[1]:.0f})')
        if len(vb) == 4 and vb[2] and vb[3]:
            for t in texts:
                if t[0] < vb[0]-1 or t[1] < vb[1]-1 or t[0]+t[2] > vb[0]+vb[2]+1 or t[1]+t[3] > vb[1]+vb[3]+1:
                    problems.append(f'{tag}: подпись «{t[4][:34]}» выходит за границы viewBox')
            for r in rects:
                if r[0] < vb[0]-1 or r[1] < vb[1]-1 or r[0]+r[2] > vb[0]+vb[2]+1 or r[1]+r[3] > vb[1]+vb[3]+1:
                    problems.append(f'{tag}: блок в {r[0]:.0f},{r[1]:.0f} выходит за границы viewBox')
    return problems
if __name__ == '__main__':
    bad = check(sys.argv[1]); seen = []
    for p in bad:
        if p not in seen: seen.append(p)
    print('OK — наложений не найдено' if not seen else f'НАЙДЕНО ПРОБЛЕМ: {len(seen)}')
    for p in seen: print(' -', p)
    sys.exit(1 if seen else 0)
```

Типичные лечения: подпись вылезает → сократи текст или расширь блок до следующей клетки сетки; линия сквозь блок → веди её по коридору (`M x,y H x2 V y2 H x3`); подписи налезают → разнеси по 16 px по вертикали; блок за viewBox → увеличь высоту svg.

## to_pdf.py — печать в PDF

```python
#!/usr/bin/env python3
"""to_pdf.py <файл.html> [выход.pdf] — A4, поля 14/16 мм, номера страниц."""
import sys, pathlib
from playwright.sync_api import sync_playwright
src = pathlib.Path(sys.argv[1]).resolve()
out = sys.argv[2] if len(sys.argv) > 2 else str(src.with_suffix('.pdf'))
FOOT = ('<div style="font:9px Inter,Arial,sans-serif;color:#757575;width:100%;text-align:center">'
        '<span class="pageNumber"></span> / <span class="totalPages"></span></div>')
with sync_playwright() as p:
    b = p.chromium.launch(); pg = b.new_page()
    pg.goto(src.as_uri(), wait_until='networkidle'); pg.emulate_media(media='print'); pg.wait_for_timeout(800)
    pg.pdf(path=out, format='A4', print_background=True,
           margin={'top': '14mm', 'bottom': '16mm', 'left': '14mm', 'right': '14mm'},
           display_header_footer=True, header_template='<div></div>', footer_template=FOOT)
    b.close()
print('готово:', out)
```

Контрольный просмотр: `pdf2image.convert_from_path(pdf, dpi=60)` → склеить в лист 4×N через PIL → открыть Read. На это уходит минута, а ловит половину ошибок.

## Чего не делать

- Не рисовать диагональные стрелки и кривые Безье — они читаются хуже и ломают проверку.
- Не ставить в один блок больше двух строк текста.
- Не оставлять «висящие» подписи без блока рядом — читатель не поймёт, к чему они.
- Не писать «система оптимизирует процессы» — писать, что именно она делает: «заполняет карточку товара из даташита».
- Не менять палитру под настроение: цвет = статус, всегда один и тот же.