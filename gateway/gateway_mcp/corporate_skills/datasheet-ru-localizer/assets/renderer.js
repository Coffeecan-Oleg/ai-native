/* ===========================================================================
   Акметрон — Даташит: рендерер (светлый табличный макет, стиль Акметрон).
   Дизайн целиком в datasheet.css. Здесь — структура DOM + АВТО-РАЗБИВКА
   контента по листам A4: длинные таблицы переносятся на следующий лист,
   шапка/подвал и заголовок раздела повторяются, шапка таблицы (h2/группа или
   заголовок матрицы) дублируется с пометкой «(продолж.)».
   =========================================================================== */
(function () {
  const SHEET_H = 1123;     // высота листа A4 @96dpi, px
  const FOOT_RESERVE = 74;  // запас под подвал + поля (с учётом округления px→mm при печати)

  const QUAL = {
    t:  { cls: "qual-typ",  txt: "тип." },
    n:  { cls: "qual-nom",  txt: "ном." },
    w:  { cls: "qual-war",  txt: "гарант." },
    m:  { cls: "qual-meas", txt: "изм." },
    nt: { cls: "qual-ntrc", txt: "н. трасс." },
  };

  const el = (tag, cls, html) => { const n = document.createElement(tag); if (cls) n.className = cls; if (html != null) n.innerHTML = html; return n; };
  const cell = (tag, cls, html, colspan) => { const c = el(tag, cls, html); if (colspan) c.setAttribute("colspan", String(colspan)); return c; };

  // chrome
  function runhead(brand) {
    const h = el("div", "ds-runhead");
    const logo = el("span", "akm-logo");
    const img = el("img"); img.src = (typeof window !== "undefined" && window.__AKM_LOGO__) || "logo-mark.png"; img.alt = "Акметрон";
    logo.appendChild(img); logo.appendChild(el("span", null, "АКМЕТРОН"));
    h.appendChild(logo); h.appendChild(el("span", "ds-site", brand.site));
    return h;
  }
  function foot(brand) {
    const f = el("div", "ds-foot");
    f.appendChild(el("span", null, "Перевод и оформление — " + brand.localizer + " · " + brand.marker));
    f.appendChild(el("span", "ds-pageno", ""));
    return f;
  }
  function sectionHead(title, cont) {
    const head = el("div", "ds-section-head");
    const h1 = el("h1", null, title);
    if (cont) h1.appendChild(el("span", "ds-cont", "  · " + cont));
    head.appendChild(h1); return head;
  }

  // rows / blocks
  const barRow  = (t) => { const tr = el("tr","h2bar"); tr.appendChild(cell("td",null,t,2)); return tr; };
  const grpRow  = (t) => { const tr = el("tr","grp");   tr.appendChild(cell("td",null,t,2)); return tr; };
  const condRow = (t) => { const tr = el("tr","cond");  tr.appendChild(cell("td",null,t,2)); return tr; };
  function dataRow(r, alt) {
    const tr = el("tr","row"); if (alt) tr.classList.add("alt");
    tr.appendChild(cell("td","param", r.l));
    const val = el("span", null, r.v || "");
    if (r.q && QUAL[r.q]) val.appendChild(el("span","qual " + QUAL[r.q].cls, QUAL[r.q].txt));
    const vtd = cell("td","val"); vtd.appendChild(val); tr.appendChild(vtd);
    return tr;
  }
  function listRow(items) {
    const tr = el("tr","row"); const td = cell("td",null,null,2);
    const ul = el("ul","lst"); items.forEach((it)=>ul.appendChild(el("li",null,it)));
    td.appendChild(ul); tr.appendChild(td); return tr;
  }
  function noteEl(n) { const d = el("div","ds-note"); d.appendChild(el("div","ds-note-label",n.label)); d.appendChild(el("p",null,n.text)); return d; }
  function proseList(l) { const w = el("div","ds-proselist"); w.appendChild(el("div","ds-note-label",l.label)); const ul=el("ul"); l.items.forEach((it)=>ul.appendChild(el("li",null,it))); w.appendChild(ul); return w; }
  function matrixTable(head, widths) {
    const t = el("table","ds-table ds-mtable");
    if (head) { const tr = el("tr","mhead"); head.forEach((h,i)=>{ const td=cell("td",null,h); if (widths&&widths[i]) td.style.width=widths[i]; tr.appendChild(td); }); t.appendChild(tr); }
    return t;
  }
  const matrixRow = (row, alt) => { const tr = el("tr","mrow"); if (alt) tr.classList.add("alt"); row.forEach((c)=>tr.appendChild(cell("td",null,c==null?"":c))); return tr; };

  // Линеаризуем spec-страницу в поток «единиц». Смежные таблицы делят runId.
  function buildUnits(p) {
    const units = []; let run = 0, bar = null, grp = null;
    const tablePage = (p.blocks||[]).some(x => x.group!=null || x.h2!=null || x.matrix);
    const newRun = () => { run++; bar = null; grp = null; };
    (p.blocks||[]).forEach((b) => {
      if (b.note)   { newRun(); units.push({ type:"block", node:()=>noteEl(b.note) }); return; }
      if (b.matrix) {
        const m = b.matrix; newRun(); const rid = run;
        (m.rows||[]).forEach((row)=>units.push({ type:"mrow", runId:rid, row:row.slice(), title:m.title, head:m.head, widths:m.widths }));
        newRun(); return;
      }
      if (b.h2 != null)   { bar = b.h2; grp = null; units.push({ type:"bar", runId:run, text:b.h2, bar, grp:null }); return; }
      if (b.group != null) {
        grp = b.group; units.push({ type:"grp", runId:run, text:b.group, bar, grp });
        (b.rows||[]).forEach((r)=>{ if (r.sub!=null) units.push({ type:"cond", runId:run, text:r.sub, bar, grp }); else units.push({ type:"data", runId:run, r, bar, grp }); });
        return;
      }
      if (b.list) {
        if (tablePage) { grp = b.list.label; units.push({ type:"grp", runId:run, text:b.list.label, bar, grp }); units.push({ type:"listrow", runId:run, items:b.list.items, bar, grp }); }
        else { newRun(); units.push({ type:"block", node:()=>proseList(b.list) }); }
        return;
      }
      if (b.rows) { b.rows.forEach((r)=>{ if (r.sub!=null) units.push({ type:"cond", runId:run, text:r.sub, bar, grp }); else units.push({ type:"data", runId:run, r, bar, grp }); }); return; }
    });
    return units;
  }

  function flowSpec(p, brand, stack, sheets) {
    const units = buildUnits(p);
    let idx = 0, first = true;
    const startedRuns = new Set();
    while (idx < units.length || first) {
      const page = el("div","ds-page"); page.appendChild(runhead(brand));
      const body = el("div","ds-body");
      const cont = first ? (p.continued || null) : (p.continued || "продолжение");
      body.appendChild(sectionHead(p.title, cont));
      page.appendChild(body); stack.appendChild(page); sheets.push(page);
      first = false;

      const pageTop = page.getBoundingClientRect().top;
      const limit = pageTop + SHEET_H - FOOT_RESERVE;
      const fits = (node) => node.getBoundingClientRect().bottom <= limit;

      let curRun = -1, curTable = null, zebra = 0, placedAny = false;

      while (idx < units.length) {
        const u = units[idx];

        if (u.type === "block") {
          curRun = -1; curTable = null;
          const node = u.node(); body.appendChild(node);
          if (!fits(node) && placedAny) { body.removeChild(node); break; }
          placedAny = true; idx++; continue;
        }

        // начать/перезапустить таблицу для текущего run
        if (u.runId !== curRun || !curTable) {
          curRun = u.runId; zebra = 0;
          if (u.type === "mrow") {
            curTable = el("table", "ds-table ds-mtable"); body.appendChild(curTable);
            const cols = (u.head && u.head.length) || (u.row && u.row.length) || 2;
            const contd = startedRuns.has(u.runId);
            if (u.title) { const tb = el("tr","h2bar"); tb.appendChild(cell("td", null, u.title + (contd?" (продолж.)":""), cols)); curTable.appendChild(tb); }
            if (u.head) { const tr = el("tr","mhead"); u.head.forEach((h,i)=>{ const td=cell("td",null,h); if (u.widths&&u.widths[i]) td.style.width=u.widths[i]; tr.appendChild(td); }); curTable.appendChild(tr); }
            startedRuns.add(u.runId);
          } else {
            curTable = el("table","ds-table"); body.appendChild(curTable);
            // повтор контекста при переносе таблицы на новый лист
            if (u.type !== "bar" && u.bar) curTable.appendChild(barRow(u.bar + " (продолж.)"));
            if (u.type !== "grp" && u.grp) { curTable.appendChild(grpRow(u.grp + " (продолж.)")); zebra = 0; }
          }
        }

        let node;
        if (u.type === "bar")       { node = barRow(u.text);  zebra = 0; }
        else if (u.type === "grp")  { node = grpRow(u.text);  zebra = 0; }
        else if (u.type === "cond") { node = condRow(u.text); }
        else if (u.type === "data") { node = dataRow(u.r, zebra % 2 === 1); }
        else if (u.type === "listrow") { node = listRow(u.items); zebra = 0; }
        else if (u.type === "mrow") { node = matrixRow(u.row, zebra % 2 === 1); }

        curTable.appendChild(node);
        if (!fits(node) && placedAny) {
          curTable.removeChild(node);
          if (curTable.querySelectorAll(".row, .mrow, .cond").length === 0) body.removeChild(curTable);
          curRun = -1; curTable = null; break;
        }
        if (u.type === "data" || u.type === "mrow") zebra++;
        placedAny = true; idx++;
      }

      page.appendChild(foot(brand));
      if (idx >= units.length) break;
    }
  }

  function renderCover(p, brand, stack, sheets) {
    const page = el("div","ds-page ds-cover");
    page.appendChild(el("div","ds-puzzle")); page.appendChild(runhead(brand));
    const body = el("div","ds-body");
    body.appendChild(el("div","ds-eyebrow", p.eyebrow));
    body.appendChild(el("div","ds-model", p.model));
    body.appendChild(el("div","ds-prodname", p.prodname));
    body.appendChild(el("p","ds-lede", p.lede));
    const photo = (typeof window !== "undefined" && window.__AKM_PHOTO__) || (p.hero && p.hero.src) || null;
    const hero = el("div","ds-hero");
    if (photo) { const im = el("img","ds-hero-img"); im.src = photo; im.alt = p.prodname || p.model || ""; hero.appendChild(im); }
    body.appendChild(hero);
    const hl = el("div","ds-highlights");
    p.highlights.forEach((h)=>hl.appendChild(el("div","v",h.v)));
    p.highlights.forEach((h)=>hl.appendChild(el("div","l",h.l)));
    body.appendChild(hl);
    const attrib = el("div","ds-attrib");
    attrib.appendChild(el("div", null, "<b>" + p.manufacturer + "</b><br>" + p.supplier));
    attrib.appendChild(el("div", null, brand.copyright));
    body.appendChild(attrib); page.appendChild(body);
    stack.appendChild(page); sheets.push(page);
  }

  function mount() {
    const data = window.DATASHEET; if (!data) return;
    const stack = document.getElementById("ds-stack");
    const sheets = [];
    data.pages.forEach((p) => { if (p.type === "cover") renderCover(p, data.brand, stack, sheets); else flowSpec(p, data.brand, stack, sheets); });
    const total = sheets.length;
    sheets.forEach((pg, i) => { const no = pg.querySelector(".ds-pageno"); if (no) no.textContent = String(i+1).padStart(2,"0") + " / " + String(total).padStart(2,"0"); });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount);
  else mount();
})();
