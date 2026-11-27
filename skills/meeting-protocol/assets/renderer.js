/* ===========================================================================
   Акметрон — Протокол встречи: рендерер.
   Читает window.PROTOCOL (+ window.__PROTO_PHOTOS__), строит титульный блок и
   разделы, затем АВТО-РАЗБИВАЕТ поток по листам A4: колонтитул и подвал
   повторяются, заголовок раздела и шапка владельца дублируются с пометкой
   «· продолжение» при переносе. Дизайн целиком в protokol.css. Не трогать
   под конкретную встречу — меняется только слой контента.
   =========================================================================== */
(function () {
  const SHEET_H = 1123;            // A4 @96dpi, px
  const PAD_V = 26 + 18;           // .proto-body padding (top + bottom) из CSS
  const SAFETY = 10;               // запас на округление px→mm при печати

  const D = window.PROTOCOL || {};
  const PHOTOS = Array.isArray(window.__PROTO_PHOTOS__) ? window.__PROTO_PHOTOS__ : [];
  const LOGO = window.__PROTO_LOGO__ || "logo-mark.png";

  const el = (tag, cls, html) => { const n = document.createElement(tag); if (cls) n.className = cls; if (html != null) n.innerHTML = html; return n; };
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

  /* ── chrome ────────────────────────────────────────────────────────────── */
  function logoLockup(size) {
    const l = el("span", "akm-logo"); if (size) l.style.fontSize = size;
    const img = el("img"); img.src = LOGO; img.alt = "Акметрон";
    l.appendChild(img); l.appendChild(el("span", null, "АКМЕТРОН")); return l;
  }
  function runhead() {
    const h = el("div", "proto-runhead");
    h.appendChild(logoLockup());
    h.appendChild(el("span", "proto-site", (D.meta && D.meta.site) || "akmetron.ru"));
    return h;
  }
  function foot() {
    const f = el("div", "proto-foot");
    const left = (D.meta && D.meta.docCode) ? D.meta.docCode : "Протокол встречи";
    const date = (D.meta && D.meta.date) ? " · " + D.meta.date : "";
    f.appendChild(el("span", null, esc(left) + esc(date)));
    f.appendChild(el("span", "proto-pageno", ""));   // номер проставляется после разбивки
    return f;
  }
  function makePage(first) {
    const p = el("div", "proto-page");
    if (first) p.appendChild(el("div", "proto-puzzle"));
    p.appendChild(runhead());
    const body = el("div", "proto-body");
    const flow = el("div", "proto-flow");
    body.appendChild(flow); p.appendChild(body); p.appendChild(foot());
    p._flow = flow;
    return p;
  }

  /* ── builders для контентных узлов ─────────────────────────────────────── */
  function titleBlock() {
    const w = el("div", "proto-title");
    w.appendChild(el("div", "proto-eyebrow", "Протокол встречи"));
    w.appendChild(el("h1", "proto-h1", esc((D.meta && D.meta.title) || "Встреча")));
    const meta = el("div", "proto-meta");
    const m = D.meta || {};
    if (m.date) meta.appendChild(el("span", null, "Дата: <b>" + esc(m.date) + "</b>"));
    if (m.location) { meta.appendChild(el("span", "dot", "·")); meta.appendChild(el("span", null, "Место: <b>" + esc(m.location) + "</b>")); }
    if (m.org) { meta.appendChild(el("span", "dot", "·")); meta.appendChild(el("span", null, esc(m.org))); }
    if (meta.childNodes.length) w.appendChild(meta);
    if (D.lead) w.appendChild(el("p", "proto-lead", esc(D.lead)));
    if (Array.isArray(D.participants) && D.participants.length) {
      const row = el("div", "proto-parties");
      D.participants.forEach((p) => {
        const c = el("span", "proto-chip");
        c.appendChild(el("span", null, esc(p.name)));
        if (p.role) c.appendChild(el("span", "role", "· " + esc(p.role)));
        row.appendChild(c);
      });
      w.appendChild(row);
    }
    w.appendChild(el("div", "proto-title-rule"));
    return w;
  }
  function sectionHead(sec, cont) {
    const wrap = el("div");
    const h = el("div", "proto-section-head");
    h.appendChild(el("span", "proto-section-num", String(sec._num)));
    const t = el("h2", null, esc(sec.title));
    if (cont) t.appendChild(el("span", "cont", "  · продолжение"));
    h.appendChild(t);
    wrap.appendChild(h);
    wrap.appendChild(el("div", "proto-section-rule"));
    return wrap;
  }
  function numRow(n, text) {
    const r = el("div", "proto-num");
    r.appendChild(el("span", "n", n != null ? esc(n) : "•"));
    r.appendChild(el("span", "t", esc(text)));
    return r;
  }
  function topicCard(it) {
    const c = el("div", "proto-topic");
    if (it.h) c.appendChild(el("h3", null, esc(it.h)));
    if (it.text) c.appendChild(el("p", null, esc(it.text)));
    if (Array.isArray(it.bullets) && it.bullets.length) {
      const ul = el("ul"); it.bullets.forEach((b) => ul.appendChild(el("li", null, esc(b)))); c.appendChild(ul);
    }
    return c;
  }
  function ownerRow(grp, cont) {
    const tr = el("tr", "owner");
    const td = el("td"); td.setAttribute("colspan", "2");
    td.innerHTML = esc(grp.owner) + (grp.role ? '<span class="role">· ' + esc(grp.role) + "</span>" : "") + (cont ? '<span class="role">· продолжение</span>' : "");
    tr.appendChild(td); return tr;
  }
  function taskRow(t, alt) {
    const tr = el("tr", "task" + (alt ? " alt" : ""));
    const d = el("td", "desc");
    d.innerHTML = (t.num != null ? '<span class="tnum">' + esc(t.num) + "</span>" : "") + esc(t.task);
    const due = el("td", "due" + (t.due ? "" : " empty"));
    due.textContent = t.due || "—";
    tr.appendChild(d); tr.appendChild(due); return tr;
  }
  function calloutNode(s) {
    const c = el("div", "proto-callout");
    c.appendChild(el("div", "label", esc(s.label || "Следующий шаг")));
    c.appendChild(el("p", null, esc(s.text)));
    return c;
  }
  function paraNode(text) { return el("p", "proto-para", esc(text)); }
  function photoNode(ph) {
    const w = el("div", "proto-photo");
    const fr = el("div", "frame");
    const img = el("img"); img.src = ph.src; if (ph._h) img.style.height = ph._h + "px";
    fr.appendChild(img); w.appendChild(fr);
    if (ph.cap) w.appendChild(el("div", "cap", esc(ph.cap)));
    return w;
  }

  /* ── линеаризация в units ──────────────────────────────────────────────── */
  // unit: { type, node?(builds standalone node), sec?, grp?, alt?, keepNext? }
  function buildUnits() {
    const units = [];
    units.push({ type: "title", node: titleBlock });

    const sections = Array.isArray(D.sections) ? D.sections : [];
    sections.forEach((sec, si) => {
      sec._num = si + 1;
      units.push({ type: "sechead", sec, keepNext: true, node: () => sectionHead(sec, false) });

      if (sec.kind === "agenda") {
        (sec.items || []).forEach((it, i) => units.push({ type: "num", sec, node: () => numRow(i + 1 + ".", it) }));
      } else if (sec.kind === "topics") {
        (sec.items || []).forEach((it) => units.push({ type: "topic", sec, node: () => topicCard(it) }));
      } else if (sec.kind === "decisions") {
        (sec.groups || []).forEach((g) => {
          units.push({ type: "owner", sec, grp: g, keepNext: true, node: () => ownerRow(g, false) });
          (g.tasks || []).forEach((t, i) => units.push({ type: "task", sec, grp: g, alt: i % 2 === 1, t, node: () => taskRow(t, i % 2 === 1) }));
        });
      } else if (sec.kind === "list") {
        (sec.items || []).forEach((it, i) => units.push({ type: "num", sec, node: () => numRow(sec.ordered === false ? null : i + 1 + ".", it) }));
      } else if (sec.kind === "callout") {
        units.push({ type: "block", sec, node: () => calloutNode(sec) });
      } else if (sec.kind === "text") {
        units.push({ type: "block", sec, node: () => paraNode(sec.text) });
      }
    });

    if (PHOTOS.length) {
      const psec = { title: "Материалы со встречи", _num: sections.length + 1, kind: "photos" };
      units.push({ type: "sechead", sec: psec, keepNext: true, node: () => sectionHead(psec, false) });
      PHOTOS.forEach((ph) => units.push({ type: "photo", sec: psec, node: () => photoNode(ph) }));
    }
    return units;
  }

  /* ── измеритель высоты ─────────────────────────────────────────────────── */
  let measurer, measureFlow, measureTable;
  function setupMeasurer() {
    measurer = el("div", "proto-body");
    measurer.style.cssText = "position:absolute;left:-10000px;top:0;width:794px;visibility:hidden;";
    measureFlow = el("div", "proto-flow");
    measurer.appendChild(measureFlow);
    document.body.appendChild(measurer);
  }
  function measure(unit) {
    let node, host = measureFlow;
    if (unit.type === "owner" || unit.type === "task") {
      const tbl = el("table", "proto-tasktable");
      tbl.appendChild(unit.node());
      node = tbl;
    } else {
      node = unit.node();
    }
    measureFlow.appendChild(node);
    const rect = node.getBoundingClientRect();
    const mb = parseFloat(getComputedStyle(node).marginBottom) || 0;
    const h = rect.height + mb;
    measureFlow.removeChild(node);
    return h;
  }

  /* ── разбивка по листам ────────────────────────────────────────────────── */
  function paginate(units, avail) {
    const stack = document.getElementById("proto-stack");
    let page, flow, used, ctx;
    let activeSec = null, activeGrp = null;

    function startPage(first) {
      page = makePage(first); flow = page._flow; used = 0;
      ctx = { table: null };
      stack.appendChild(page);
    }
    function ensureTable() {
      if (!ctx.table) { ctx.table = el("table", "proto-tasktable"); flow.appendChild(ctx.table); }
      return ctx.table;
    }
    function add(node, h) { flow.appendChild(node); used += h; }
    function addRow(tr, h) { ensureTable().appendChild(tr); used += h; }

    startPage(true);

    for (let i = 0; i < units.length; i++) {
      const u = units[i];
      const h = u._h;

      // keep-with-next: не оставляем заголовок раздела/владельца внизу листа
      if (u.keepNext && i + 1 < units.length) {
        const nh = units[i + 1]._h;
        if (used > 0 && used + h + nh > avail) { startPage(false); }
      } else if (used > 0 && used + h > avail) {
        startPage(false);
      }

      // если только что начали новый лист в середине раздела/группы — повторяем шапки
      if (used === 0 && u.type !== "title" && u.type !== "sechead") {
        if (u.sec && u.sec === activeSec) {
          const ch = sectionHead(u.sec, true);
          used += measureNode(ch.cloneNode(true));   // измеряем клон, оригинал не трогаем
          flow.appendChild(ch);
          ctx.table = null;
          if ((u.type === "task") && u.grp && u.grp === activeGrp) {
            const orow = ownerRow(u.grp, true);
            used += measureRowNode(orow.cloneNode(true));
            ensureTable().appendChild(orow);
          }
        }
      }

      // постановка узла
      if (u.type === "owner") { activeGrp = u.grp; const tr = u.node(); addRow(tr, h); }
      else if (u.type === "task") { const tr = u.node(); addRow(tr, h); }
      else {
        if (u.type === "sechead") { activeSec = u.sec; activeGrp = null; ctx.table = null; }
        else if (u.type !== "title") { ctx.table = null; }   // любой не-табличный блок закрывает текущую таблицу
        const node = u.node(); add(node, h);
      }
    }

    // нумерация страниц
    const pages = stack.querySelectorAll(".proto-page");
    pages.forEach((pg, idx) => {
      const no = pg.querySelector(".proto-pageno");
      if (no) no.textContent = (idx + 1) + " / " + pages.length;
    });
    return pages.length;
  }

  // измерение уже построенного узла (для continuation-шапок)
  function measureNode(node) {
    measureFlow.appendChild(node);
    const r = node.getBoundingClientRect();
    const mb = parseFloat(getComputedStyle(node).marginBottom) || 0;
    measureFlow.removeChild(node);
    return r.height + mb;
  }
  function measureRowNode(tr) {
    const tbl = el("table", "proto-tasktable"); tbl.appendChild(tr);
    measureFlow.appendChild(tbl);
    const r = tr.getBoundingClientRect();
    measureFlow.removeChild(tbl);
    tbl.removeChild(tr);
    return r.height;
  }

  /* ── предзагрузка фото и расчёт высоты под ширину контента ─────────────── */
  function preparePhotos(contentW, maxH) {
    return Promise.all(PHOTOS.map((ph) => new Promise((res) => {
      const img = new Image();
      img.onload = () => {
        const ratio = img.naturalHeight / img.naturalWidth;
        let h = contentW * ratio;
        if (h > maxH) h = maxH;       // высокие снимки ужимаем по высоте
        ph._h = Math.round(h);
        res();
      };
      img.onerror = () => { ph._h = Math.round(contentW * 0.66); res(); };
      img.src = ph.src;
    })));
  }

  /* ── запуск ────────────────────────────────────────────────────────────── */
  async function run() {
    await document.fonts.ready;
    setupMeasurer();

    // вычисляем доступную высоту тела: измеряем реальные колонтитул/подвал
    const probe = makePage(false);
    probe.style.cssText = "position:absolute;left:-10000px;top:0;";
    document.body.appendChild(probe);
    const rhH = probe.querySelector(".proto-runhead").getBoundingClientRect().height;
    const ftH = probe.querySelector(".proto-foot").getBoundingClientRect().height;
    document.body.removeChild(probe);
    const avail = SHEET_H - rhH - ftH - PAD_V - SAFETY;

    const contentW = 794 - 60 * 2;   // ширина тела минус --pad-x
    await preparePhotos(contentW, avail - 24);

    const units = buildUnits();
    units.forEach((u) => { u._h = measure(u); });

    const n = paginate(units, avail);
    document.body.removeChild(measurer);

    window.__PROTO_PAGES__ = n;
    window.__PROTO_DONE__ = true;
    document.body.dataset.ready = "1";
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", run);
  else run();
})();
