"use strict";
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const strip = (s) => String(s ?? "").replace(/\[\/?\d+\/?\]/g, "").replace(/⟦/g, "[").replace(/⟧/g, "]");
const TYPES = { person: "ตัวละคร", place: "สถานที่", organization: "องค์กร/ตระกูล", title: "ยศ/ตำแหน่ง", term: "ศัพท์/คำทั่วไป" };
const GENDERS = [["", "?"], ["female", "หญิง"], ["male", "ชาย"]];
const PARTICLES = [["", "อัตโนมัติ"], ["ค่ะ/คะ", "ค่ะ/คะ"], ["ครับ", "ครับ"], ["none", "ไม่ใช้"]];
const STATUS = {
  parsed: "อ่านไฟล์แล้ว", glossary: "กำลังทำ glossary", review: "รอตรวจ glossary", queued: "รอคิว",
  translating: "กำลังแปล", waiting: "รอเครื่องโมเดล", paused: "หยุดชั่วคราว", done: "เสร็จแล้ว",
  cancelled: "ยกเลิกแล้ว", error: "ผิดพลาด",
};
const STEP_OF = { parsed: 0, glossary: 1, review: 2, queued: 3, translating: 3, waiting: 3, paused: 3, done: 4, cancelled: 3, error: 3 };

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: opts.body && !(opts.body instanceof FormData) ? { "Content-Type": "application/json" } : {}, ...opts });
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return r.headers.get("content-type")?.includes("json") ? r.json() : r;
}

function toast(text, kind = "") {
  const t = document.createElement("div");
  t.className = "toast " + kind;
  t.textContent = text;
  $("#toasts").appendChild(t);
  setTimeout(() => t.remove(), kind === "err" ? 5000 : 2600);
}
const icon = (id) => `<svg><use href="#i-${id}"/></svg>`;

function fmtDur(s) {
  if (s == null) return "–";
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h} ชม. ${m} นาที` : m ? `${m} นาที` : `${s} วินาที`;
}

// ------------------------------------------------------------------------------------------- health
async function health() {
  const el = $("#health");
  try {
    const h = await api("/api/health");
    if (h.ok) {
      el.className = "pill ok";
      el.textContent = "เครื่องโมเดลพร้อม" + (h.models?.includes(h.translate_model) ? "" : " (โมเดลแปลยังไม่อยู่ในรายการ)");
      el.title = (h.models || []).join(", ");
    } else { el.className = "pill bad"; el.textContent = "ต่อเครื่องโมเดลไม่ได้"; el.title = h.error || ""; }
  } catch { el.className = "pill bad"; el.textContent = "ต่อเซิร์ฟเวอร์ไม่ได้"; }
}

// ------------------------------------------------------------------------------------------- upload
let pickedFile = null;
function pick(f) {
  if (!f) return;
  if (!/\.epub$/i.test(f.name)) { $("#upload-msg").textContent = "รองรับเฉพาะไฟล์ .epub"; return; }
  pickedFile = f;
  $("#file-name").textContent = `${f.name} (${(f.size / 1048576).toFixed(1)} MB)`;
  $("#upload").disabled = false;
}
$("#file").onchange = (e) => pick(e.target.files[0]);
const drop = $("#drop");
drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
drop.ondragleave = () => drop.classList.remove("over");
drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); pick(e.dataTransfer.files[0]); };

$("#upload").onclick = () => {
  if (!pickedFile) return;
  const fd = new FormData();
  fd.append("file", pickedFile);
  fd.append("bilingual", $("#opt-bilingual").checked);
  fd.append("auto_glossary", $("#opt-auto").checked);
  fd.append("style_notes", $("#opt-notes").value);
  fd.append("series_from", $("#opt-series").value);
  const bar = $("#upload-bar"), msg = $("#upload-msg");
  bar.classList.remove("hidden"); msg.className = "msg"; msg.textContent = "กำลังอัปโหลด…";
  $("#upload").disabled = true;
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/jobs");
  xhr.upload.onprogress = (e) => { if (e.lengthComputable) bar.firstElementChild.style.width = (100 * e.loaded / e.total) + "%"; };
  xhr.upload.onload = () => { bar.classList.add("indeterminate"); msg.textContent = "กำลังอ่านไฟล์และแยกย่อหน้า…"; };
  xhr.onload = () => {
    bar.classList.add("hidden"); bar.classList.remove("indeterminate"); bar.firstElementChild.style.width = "0";
    if (xhr.status === 200) {
      const job = JSON.parse(xhr.responseText);
      msg.textContent = ""; pickedFile = null; $("#file-name").textContent = ""; $("#file").value = "";
      toast("อัปโหลดแล้ว กำลังหาคำเฉพาะ");
      openJob(job.id);
    } else {
      let d = xhr.statusText; try { d = JSON.parse(xhr.responseText).detail; } catch {}
      msg.className = "msg err"; msg.textContent = d; $("#upload").disabled = false;
    }
  };
  xhr.onerror = () => { msg.className = "msg err"; msg.textContent = "อัปโหลดไม่สำเร็จ"; $("#upload").disabled = false; };
  xhr.send(fd);
};

// ------------------------------------------------------------------------------------------- job list
let allJobs = [];
// earlier books whose glossary can carry over to the next book of a series
function seriesOptions(sel, exclude) {
  const keep = sel.value, first = sel.options[0].outerHTML;
  const books = allJobs.filter((j) => j.id !== exclude && j.glossary_terms && !["parsed", "glossary"].includes(j.status));
  if (document.activeElement === sel) return books.length;     // rebuilding would close the open list
  sel.innerHTML = first + books.map((j) => `<option value="${j.id}">${esc(j.name)} (${j.glossary_terms} คำ)</option>`).join("");
  if (books.some((j) => j.id === keep)) sel.value = keep;
  return books.length;
}
async function loadJobs() {
  const jobs = await api("/api/jobs");
  allJobs = jobs;
  $("#series-field").classList.toggle("hidden", !seriesOptions($("#opt-series")));
  const box = $("#jobs");
  if (!jobs.length) {
    box.innerHTML = `<div class="empty">${icon("book")}<b>ยังไม่มีงานแปล</b><span>เพิ่มไฟล์ .epub ทางซ้ายเพื่อเริ่ม</span></div>`;
    return;
  }
  box.innerHTML = jobs.map((j) => {
    const meta = j.status === "translating" && j.eta_seconds != null ? `เหลือประมาณ ${fmtDur(j.eta_seconds)}` : (j.message || "");
    return `
    <div class="job" data-id="${j.id}" role="button" tabindex="0">
      <div class="job-title">${esc(j.name)}</div>
      <div class="job-side"><span class="chip" data-s="${j.status}">${STATUS[j.status] || j.status}</span><span class="job-pct">${j.percent}%</span></div>
      <div class="job-meta">${esc(meta)}</div>
      <div class="bar"><div style="width:${j.percent}%"></div></div>
    </div>`;
  }).join("");
  box.querySelectorAll(".job").forEach((el) => (el.onkeydown = (e) => { if (e.key === "Enter") openJob(el.dataset.id); }));
  box.querySelectorAll(".job").forEach((el) => (el.onclick = () => openJob(el.dataset.id)));
}

// ------------------------------------------------------------------------------------------- job view
let current = null, currentJob = null, tab = "glossary";
let glossary = [], glossaryDirty = false;

function openJob(id) {
  current = id; glossary = []; glossaryDirty = false;
  location.hash = id;
  setView("books");
  $("#list-view").classList.add("hidden"); $("#job-view").classList.remove("hidden");
  window.scrollTo(0, 0);
  refreshJob().then(() => showTab(currentJob?.status === "done" || currentJob?.status === "translating" ? "preview" : "glossary"));
}
$("#back").onclick = () => { current = null; location.hash = ""; document.title = "แปลหนังสือ EPUB"; $("#job-view").classList.add("hidden"); $("#list-view").classList.remove("hidden"); loadJobs(); };

async function refreshJob() {
  if (!current) return;
  let j;
  try { j = await api(`/api/jobs/${current}`); } catch { $("#back").click(); return; }
  const prev = currentJob?.status;
  currentJob = j;
  $("#job-name").textContent = j.name;
  document.title = `${j.percent}% · ${j.name}`;
  $("#job-status").textContent = STATUS[j.status] || j.status;
  $("#job-status").dataset.s = j.status;
  const step = STEP_OF[j.status] ?? 0;
  document.querySelectorAll("#steps li").forEach((li, i) => { li.className = i < step || j.status === "done" ? "done" : i === step ? "now" : ""; });
  $("#job-msg").textContent = j.message || "";
  $("#job-msg").className = "msg" + (j.status === "error" ? " err" : "");
  const bar = $("#job-bar");
  const inGlossary = j.status === "glossary";
  if (inGlossary && j.glossary_progress?.[1]) {
    const [d, t] = j.glossary_progress;
    bar.firstElementChild.style.width = (100 * d / t) + "%";
    $("#job-pct").textContent = `${Math.round(100 * d / t)}%`;
  } else {
    bar.firstElementChild.style.width = j.percent + "%";
    $("#job-pct").textContent = `${j.percent}%`;
  }
  bar.classList.toggle("indeterminate", inGlossary && !j.glossary_progress?.[1]);
  $("#job-eta").textContent = inGlossary ? "กำลังทำ glossary"
    : j.eta_seconds != null && ["translating", "queued", "waiting"].includes(j.status) ? `เหลือประมาณ ${fmtDur(j.eta_seconds)}`
    : j.status === "done" ? "แปลครบแล้ว" : "";
  const elapsed = j.started ? ((j.finished || Date.now() / 1000) - j.started) : null;
  const stat = (label, value, cls = "") => `<div class="stat ${cls}"><span class="stat-label">${label}</span><span class="stat-value" title="${esc(value)}">${value}</span></div>`;
  $("#job-stats").innerHTML = [
    stat("ย่อหน้า", `${j.done + j.flagged} / ${j.segments}`),
    stat("ต้องตรวจ", j.flagged, j.flagged ? "warn" : ""),
    stat("Glossary", `${j.glossary_terms} คำ`),
    j.chars_per_sec ? stat("ความเร็ว (5 นาทีล่าสุด)", `${j.chars_per_sec} ตัวอักษร/วิ${j.gen_tps ? ` · ${j.gen_tps} tok/s` : ""}`) : "",
    elapsed ? stat("ใช้เวลาไป", fmtDur(elapsed)) : "",
    j.current_doc && j.status === "translating" ? stat("กำลังแปล", esc(j.current_doc)) : "",
    j.status !== "done" && j.docs_ready ? stat("อ่านต่อเนื่องได้แล้ว", `${j.docs_ready}/${j.docs_total} บท · ถึง ${esc(j.readable_until)}`, "ok") : "",
  ].filter(Boolean).join("");
  $("#flag-count").textContent = j.flagged || "";
  await refreshSends();
  renderActions(j);
  $("#g-start").classList.toggle("hidden", j.status !== "review");
  const imp = $("#g-import");
  if (document.activeElement !== imp) {
    if (!allJobs.length) api("/api/jobs").then((x) => { allJobs = x; }).catch(() => {});
    imp.classList.toggle("hidden", !seriesOptions(imp, j.id) || ["glossary", "translating"].includes(j.status));
  }
  const prof = $("#g-profile");
  if (document.activeElement !== prof && !prof.dataset.dirty) prof.value = j.options.book_profile || "";
  if (prev !== j.status && (j.status === "review" || prev === "glossary")) loadGlossary();
  if (tab === "preview" && j.status === "translating") loadPreview(false);
}

function renderActions(j) {
  const a = [], more = [];
  // main actions (bottom bar on phones) vs occasional ones (stay in the card)
  const btn = (label, fn, cls = "") => (["dl-bi", "retry-flagged", "cancel", "x4", "rename"].includes(fn) ? more : a)
    .push(`<button class="${cls}" data-fn="${fn}">${label}</button>`);
  // primary action first: it gets the most room in the bottom bar on phones
  if (j.status === "review") btn("เริ่มแปล", "start", "primary");
  if (j.status === "paused") btn("ทำต่อ", "resume", "primary");
  if (["error", "cancelled"].includes(j.status)) btn("ลองใหม่", "start", "primary");
  if (j.status === "done") btn(`${icon("download")}ดาวน์โหลด EPUB ไทย`, "dl", "primary");
  if (["queued", "translating", "waiting", "glossary"].includes(j.status)) btn("หยุดชั่วคราว", "pause");
  if (j.done + j.flagged > 0) {
    if (j.status !== "done") btn(`${icon("download")}อ่านเท่าที่แปลแล้ว`, "dl");
    btn(`${icon("download")}สองภาษา`, "dl-bi");
    btn(x4Label(`book:${j.id}`), "x4");
  }
  if (j.flagged && ["done", "paused", "review", "error"].includes(j.status)) btn(`แปลใหม่ ${j.flagged} ย่อหน้า`, "retry-flagged");
  btn("เปลี่ยนชื่อ", "rename");
  if (!["done", "cancelled"].includes(j.status)) btn("ยกเลิก", "cancel", "danger");
  $("#job-actions").innerHTML = a.join("");
  $("#job-more").innerHTML = more.join("");
  document.querySelectorAll("#job-actions button, #job-more button").forEach((b) => (b.onclick = () => action(b.dataset.fn)));
}

async function action(fn) {
  try {
    if (fn === "x4") return sendX4("book", current);
    if (fn === "rename") return renameJob();
    if (fn === "dl" || fn === "dl-bi") {
      location.href = `/api/jobs/${current}/download${fn === "dl-bi" ? "?bilingual=true" : "?bilingual=false"}`;
      return;
    }
    if (fn === "cancel" && !confirm("ยกเลิกงานนี้? ย่อหน้าที่แปลแล้วยังอยู่ ดาวน์โหลดได้")) return;
    if (fn === "start" && glossaryDirty) await saveGlossary();
    await api(`/api/jobs/${current}/${fn}`, { method: "POST" });
    toast({ start: "เริ่มแปลแล้ว", pause: "หยุดชั่วคราวแล้ว", resume: "ทำต่อแล้ว", cancel: "ยกเลิกแล้ว",
            "retry-flagged": "กำลังแปลย่อหน้าที่มีปัญหาใหม่", "regenerate-glossary": "กำลังทำ glossary ใหม่" }[fn] || "เรียบร้อย");
    refreshJob();
  } catch (e) { toast(e.message, "err"); }
}

// ------------------------------------------------------------------------------------------- tabs
document.querySelectorAll("#tabs button").forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));
function showTab(t) {
  tab = t;
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === t));
  document.querySelectorAll(".tab").forEach((el) => el.classList.toggle("hidden", el.id !== "tab-" + t));
  if (t === "glossary") loadGlossary();
  if (t === "flagged") loadFlagged(0);
  if (t === "preview") loadDocs();
  if (t === "settings" && currentJob) {
    $("#s-bilingual").checked = !!currentJob.options.bilingual;
    $("#s-notes").value = currentJob.options.style_notes || "";
  }
}

// ------------------------------------------------------------------------------------------- glossary
$("#g-type").innerHTML += Object.entries(TYPES).map(([k, v]) => `<option value="${k}">${v}</option>`).join("");
async function loadGlossary() {
  if (glossaryDirty) return;
  glossary = await api(`/api/jobs/${current}/glossary`);
  renderGlossary();
}
function renderGlossary() {
  const q = $("#g-search").value.trim().toLowerCase(), ty = $("#g-type").value;
  const tb = $("#g-table tbody");
  const rows = glossary.map((g, i) => [g, i]).filter(([g]) => (!q || g.en.toLowerCase().includes(q) || (g.th || "").includes(q)) && (!ty || g.type === ty));
  if (!glossary.length) {
    tb.innerHTML = `<tr><td colspan="11" class="muted">${currentJob?.status === "glossary" ? "กำลังหาคำเฉพาะ ข้อมูลตัวละคร และเสนอคำแปล…" : "ยังไม่มีคำ"}</td></tr>`;
    return;
  }
  const sel = (k, val, opts) => `<select data-k="${k}">${opts.map(([v, l]) => `<option value="${v}" ${val === v ? "selected" : ""}>${l}</option>`).join("")}</select>`;
  const txt = (k, val, ph, w) => `<input type="text" data-k="${k}" value="${esc(val || "")}" placeholder="${ph}" style="min-width:${w}px">`;
  tb.innerHTML = rows.map(([g, i]) => {
    const person = g.type === "person";
    const ch = person ? `
      <td data-label="เพศ">${sel("gender", g.gender || "", GENDERS)}</td>
      <td data-label="คำลงท้าย">${sel("particle", g.particle || "", PARTICLES)}</td>
      <td data-label="แทนตัว">${txt("self_pronoun", g.self_pronoun, "ฉัน/ผม/ข้า", 70)}</td>
      <td data-label="บทบาท">${txt("role", g.role, "เช่น จักรพรรดิ", 110)}</td>
      <td data-label="คนอื่นเรียก">${txt("addressed_as", g.addressed_as, "เช่น ราชาศัพท์", 90)}</td>` : `<td colspan="5" class="muted small nochar">—</td>`;
    return `
    <tr data-i="${i}" class="${g.source === "user" ? "user" : g.source === "series" ? "series" : ""}">
      <td class="en" data-label="อังกฤษ" title="${esc(g.example || "")}">${g.isNew ? `<input type="text" data-k="en" value="${esc(g.en)}" placeholder="คำอังกฤษ">` : esc(g.en)}</td>
      <td data-label="ประเภท"><select data-k="type">${Object.entries(TYPES).map(([k, v]) => `<option value="${k}" ${g.type === k ? "selected" : ""}>${v}</option>`).join("")}</select></td>
      <td data-label="ไทย"><input type="text" data-k="th" value="${esc(g.th)}"></td>
      <td data-label="คงอังกฤษ"><input type="checkbox" data-k="keep_english" ${g.keep_english ? "checked" : ""}></td>
      <td class="muted count-cell" data-label="พบ">${g.count || ""}</td>${ch}
      <td class="del-cell"><button class="link" data-del>ลบ</button></td>
    </tr>`;
  }).join("");
  tb.querySelectorAll("tr[data-i]").forEach((tr) => {
    const g = glossary[+tr.dataset.i];
    tr.querySelectorAll("[data-k]").forEach((inp) => (inp.oninput = inp.onchange = () => {
      g[inp.dataset.k] = inp.type === "checkbox" ? inp.checked : inp.value;
      g.source = "user"; glossaryDirty = true; $("#g-msg").textContent = "ยังไม่ได้บันทึก";
      if (inp.dataset.k === "type") renderGlossary();        // show / hide the character columns
    }));
    tr.querySelector("[data-del]").onclick = () => { glossary.splice(+tr.dataset.i, 1); glossaryDirty = true; renderGlossary(); };
  });
}
$("#g-search").oninput = renderGlossary;
$("#g-type").onchange = renderGlossary;
$("#g-add").onclick = () => { glossary.unshift({ en: "", type: "person", th: "", keep_english: false, isNew: true, source: "user" }); glossaryDirty = true; renderGlossary(); };
async function saveGlossary() {
  const terms = glossary.filter((g) => g.en.trim()).map((g) => ({
    en: g.en, type: g.type, th: g.th, keep_english: !!g.keep_english, gender: g.gender || "", particle: g.particle || "",
    self_pronoun: g.self_pronoun || "", role: g.role || "", addressed_as: g.addressed_as || "",
  }));
  const r = await api(`/api/jobs/${current}/glossary`, { method: "PUT", body: JSON.stringify(terms) });
  glossaryDirty = false;
  $("#g-msg").textContent = "";
  toast(`บันทึก glossary แล้ว ${r.terms} คำ`);
  await loadGlossary();
}
$("#g-profile").oninput = () => { $("#g-profile").dataset.dirty = "1"; $("#g-profile-msg").textContent = "ยังไม่ได้บันทึก"; };
$("#g-profile-save").onclick = async () => {
  try {
    await api(`/api/jobs/${current}`, { method: "PATCH", body: JSON.stringify({ book_profile: $("#g-profile").value }) });
    delete $("#g-profile").dataset.dirty;
    $("#g-profile-msg").textContent = "";
    toast("บันทึกข้อมูลหนังสือแล้ว ใช้กับย่อหน้าที่ยังไม่ได้แปล");
    refreshJob();
  } catch (e) { toast(e.message, "err"); }
};
$("#g-import").onchange = async () => {
  const sel = $("#g-import"), src = sel.value;
  if (!src) return;
  const name = sel.selectedOptions[0].textContent;
  sel.value = "";
  if (!confirm(`ใช้ชื่อ คำแปล และข้อมูลตัวละครจาก "${name}"? คำที่คุณแก้เองในเล่มนี้จะไม่ถูกทับ`)) return;
  try {
    if (glossaryDirty) await saveGlossary();
    const r = await api(`/api/jobs/${current}/glossary/import?source=${encodeURIComponent(src)}`, { method: "POST" });
    toast(`นำเข้าจากเล่มก่อนแล้ว ${r.terms} คำ`);
    glossaryDirty = false; await loadGlossary(); refreshJob();
  } catch (e) { toast(e.message, "err"); }
};
$("#g-save").onclick = () => saveGlossary().catch((e) => toast(e.message, "err"));
$("#g-start").onclick = async () => { await saveGlossary(); await action("start"); };

// ------------------------------------------------------------------------------------------- flagged
async function loadFlagged(offset) {
  const r = await api(`/api/jobs/${current}/segments?status=flagged&offset=${offset}&limit=20`);
  $("#flagged").innerHTML = r.items.length ? r.items.map(segHtml).join("") : `<div class="empty">${icon("check")}<b>ไม่มีย่อหน้าที่ต้องตรวจ</b><span>ทุกย่อหน้าผ่านการตรวจแล้ว</span></div>`;
  bindSegs($("#flagged"), () => loadFlagged(offset));
  pager($("#flag-pager"), r.total, offset, 20, loadFlagged);
}
function segHtml(s) {
  return `<div class="seg ${s.status}" data-id="${s.id}">
    <div class="src">${esc(strip(s.marked))}</div>
    <div class="tgt">${s.out ? esc(strip(s.out)) : "(ยังไม่ได้แปล)"}
      ${s.status !== "pending" ? `<details><summary class="small muted">แก้ไข</summary>
        <textarea rows="4">${esc(s.out || "")}</textarea>
        <div class="row"><button data-save>บันทึก</button><button data-re>แปลใหม่</button><span class="msg small"></span></div>
      </details>` : ""}</div>
    ${s.note ? `<div class="note">⚠ ${esc(s.note)}</div>` : ""}
  </div>`;
}
function bindSegs(box, reload) {
  box.querySelectorAll(".seg").forEach((el) => {
    const id = el.dataset.id, msg = el.querySelector(".msg");
    const save = el.querySelector("[data-save]"), re = el.querySelector("[data-re]");
    if (save) save.onclick = async () => {
      try { await api(`/api/jobs/${current}/segments/${id}`, { method: "PUT", body: JSON.stringify({ out: el.querySelector("textarea").value }) }); toast("บันทึกย่อหน้าแล้ว"); reload(); refreshJob(); }
      catch (e) { toast(e.message, "err"); }
    };
    if (re) re.onclick = async () => {
      msg.textContent = "กำลังแปลใหม่…"; re.disabled = true;
      try { await api(`/api/jobs/${current}/segments/${id}/retranslate`, { method: "POST" }); toast("แปลใหม่แล้ว"); reload(); refreshJob(); }
      catch (e) { toast(e.message, "err"); msg.textContent = ""; re.disabled = false; }
    };
  });
}
function pager(box, total, offset, limit, load) {
  const pages = Math.ceil(total / limit), page = Math.floor(offset / limit);
  if (pages <= 1) { box.innerHTML = ""; return; }
  box.innerHTML = `<button ${page === 0 ? "disabled" : ""} data-p="${page - 1}">‹</button>
    <span class="muted">หน้า ${page + 1}/${pages}</span>
    <button ${page >= pages - 1 ? "disabled" : ""} data-p="${page + 1}">›</button>`;
  box.querySelectorAll("button").forEach((b) => (b.onclick = () => load(+b.dataset.p * limit)));
}

// ------------------------------------------------------------------------------------------- preview
let previewOffset = 0;
async function loadDocs() {
  const docs = await api(`/api/jobs/${current}/docs`);
  const sel = $("#p-doc"), keep = sel.value;
  sel.innerHTML = docs.map((d) => `<option value="${esc(d.doc)}">${esc(d.title)} — ${d.done + d.flagged}/${d.n}${d.flagged ? ` (ตรวจ ${d.flagged})` : ""}</option>`).join("");
  if (keep && docs.some((d) => d.doc === keep)) sel.value = keep;
  else {
    // open where there is something to read: the chapter being translated, else the first with real text done
    const now = docs.find((d) => d.title === currentJob?.current_doc && currentJob?.status === "translating");
    const first = docs.find((d) => d.n > 2 && d.done + d.flagged > 0) || docs.find((d) => d.n > 2) || docs[0];
    if (now || first) sel.value = (now || first).doc;
  }
  previewOffset = 0;
  loadPreview(true);
}
$("#p-doc").onchange = () => { previewOffset = 0; loadPreview(true); };
async function loadPreview(scroll) {
  const doc = $("#p-doc").value;
  if (!doc) return;
  const r = await api(`/api/jobs/${current}/segments?doc=${encodeURIComponent(doc)}&offset=${previewOffset}&limit=40`);
  const box = $("#preview");
  if (!scroll && box.querySelector("textarea:focus, details[open]")) return;   // don't disturb an edit in progress
  const items = r.items.filter((s) => s.kind !== "title");
  box.innerHTML = items.length ? items.map(segHtml).join("")
    : `<div class="empty">${icon("book")}<b>บทนี้ไม่มีย่อหน้าให้แสดง</b><span>เลือกบทอื่นจากรายการด้านบน</span></div>`;
  bindSegs(box, () => loadPreview(false));
  pager($("#p-pager"), r.total, previewOffset, 40, (o) => { previewOffset = o; loadPreview(true); });
}

// ------------------------------------------------------------------------------------------- settings
$("#s-save").onclick = async () => {
  try {
    await api(`/api/jobs/${current}`, { method: "PATCH", body: JSON.stringify({ bilingual: $("#s-bilingual").checked, style_notes: $("#s-notes").value }) });
    toast("บันทึกการตั้งค่าแล้ว"); refreshJob();
  } catch (e) { toast(e.message, "err"); }
};
$("#s-regloss").onclick = () => confirm("ทำ glossary ใหม่? คำที่คุณแก้เองจะยังอยู่") && action("regenerate-glossary");
$("#s-delete").onclick = async () => {
  if (!confirm("ลบงานนี้และไฟล์ทั้งหมด?")) return;
  await api(`/api/jobs/${current}`, { method: "DELETE" });
  $("#back").click();
};

// ------------------------------------------------------------------------------------------- menu
let view = "books";
function setView(v) {
  view = v;
  document.querySelectorAll(".modes a").forEach((a) => a.classList.toggle("active", a.dataset.view === v));
  $("#pdf-view").classList.toggle("hidden", v !== "pdf");
  if (v === "pdf") { $("#list-view").classList.add("hidden"); $("#job-view").classList.add("hidden"); }
}
document.querySelectorAll(".modes a").forEach((a) => (a.onclick = (e) => {
  e.preventDefault();
  current = null;
  document.title = "แปลหนังสือ EPUB";
  if (a.dataset.view === "pdf") { location.hash = "pdf"; setView("pdf"); pdfSig = ""; loadPdfs(); checkX4(); }
  else { location.hash = ""; setView("books"); $("#job-view").classList.add("hidden"); $("#list-view").classList.remove("hidden"); loadJobs(); }
}));

// ------------------------------------------------------------------------------------------- PDF -> Xteink
const MODE_LABEL = { auto: "แถบแนวนอน", 3: "ไม่เกิน 3 แถบ/หน้า", 4: "ไม่เกิน 4 แถบ/หน้า", page: "ทั้งหน้า" };
const PDF_STATUS = { queued: "รอคิว", converting: "กำลังแปลง", done: "พร้อมแล้ว", error: "ผิดพลาด", cancelled: "ยกเลิก" };
let pdfPicked = null, pdfSig = "", pdfOpen = null;          // pdfOpen: {id, n} = preview panel shown

function pdfPick(f) {
  if (!f) return;
  if (!/\.pdf$/i.test(f.name)) { $("#pdf-msg").textContent = "รองรับเฉพาะไฟล์ .pdf"; return; }
  pdfPicked = f;
  $("#pdf-name").textContent = `${f.name} (${(f.size / 1048576).toFixed(1)} MB)`;
  $("#pdf-upload").disabled = false; $("#pdf-msg").textContent = "";
}
$("#pdf-file").onchange = (e) => pdfPick(e.target.files[0]);
const pdrop = $("#pdf-drop");
pdrop.ondragover = (e) => { e.preventDefault(); pdrop.classList.add("over"); };
pdrop.ondragleave = () => pdrop.classList.remove("over");
pdrop.ondrop = (e) => { e.preventDefault(); pdrop.classList.remove("over"); pdfPick(e.dataTransfer.files[0]); };

$("#pdf-upload").onclick = () => {
  if (!pdfPicked) return;
  const fd = new FormData();
  fd.append("file", pdfPicked);
  fd.append("mode", $("#pdf-mode").value);
  fd.append("rotate", $("#pdf-rotate").value);
  fd.append("depth", $("#pdf-depth").value);
  fd.append("trim", $("#pdf-trim").checked);
  const bar = $("#pdf-bar"), msg = $("#pdf-msg");
  bar.classList.remove("hidden"); msg.className = "msg"; msg.textContent = "กำลังอัปโหลด…";
  $("#pdf-upload").disabled = true;
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/pdf");
  xhr.upload.onprogress = (e) => { if (e.lengthComputable) bar.firstElementChild.style.width = (100 * e.loaded / e.total) + "%"; };
  xhr.upload.onload = () => { bar.classList.add("indeterminate"); msg.textContent = "กำลังเปิดไฟล์…"; };
  xhr.onload = () => {
    bar.classList.add("hidden"); bar.classList.remove("indeterminate"); bar.firstElementChild.style.width = "0";
    if (xhr.status === 200) {
      msg.textContent = ""; pdfPicked = null; $("#pdf-name").textContent = ""; $("#pdf-file").value = "";
      toast("อัปโหลดแล้ว กำลังแปลง"); pdfSig = ""; loadPdfs();
    } else {
      let d = xhr.statusText; try { d = JSON.parse(xhr.responseText).detail; } catch {}
      msg.className = "msg err"; msg.textContent = d; $("#pdf-upload").disabled = false;
    }
  };
  xhr.onerror = () => { msg.className = "msg err"; msg.textContent = "อัปโหลดไม่สำเร็จ"; $("#pdf-upload").disabled = false; };
  xhr.send(fd);
};

const optSel = (cls, val, opts) => `<select class="${cls}">${opts.map(([v, l]) => `<option value="${v}" ${String(val) === String(v) ? "selected" : ""}>${l}</option>`).join("")}</select>`;

async function loadPdfs() {
  const items = await api("/api/pdf");
  await refreshSends();
  const sig = JSON.stringify([items, pdfOpen, x4Sends]);
  if (sig === pdfSig) return;                       // nothing changed: keep the preview image as it is
  if (document.activeElement?.closest?.("#pdfs select")) return;
  pdfSig = sig;
  const box = $("#pdfs");
  if (!items.length) {
    box.innerHTML = `<div class="empty">${icon("book")}<b>ยังไม่มีไฟล์</b><span>เพิ่ม PDF ทางซ้ายเพื่อแปลงให้ X4</span></div>`;
    return;
  }
  box.innerHTML = items.map((d) => {
    const o = d.options, pct = d.pdf_pages ? Math.round(100 * d.done_pages / d.pdf_pages) : 0;
    const mb = (d.out_size ? ` · ${(d.out_size / 1048576).toFixed(1)} MB` : "") + (o.folder ? ` · 📁 ${o.folder}` : "");
    const nparts = d.parts?.length || 0;
    const meta = d.status === "done" ? `${d.pdf_pages} หน้า PDF → ${d.out_pages} หน้าจอ${nparts > 1 ? ` (${nparts} ไฟล์)` : ""}${mb} · ${MODE_LABEL[o.mode] || o.mode}${o.depth == 2 ? " · เทา 4 ระดับ" : " · ขาวดำ"}`
      : `${d.pdf_pages} หน้า PDF · ${d.message || ""}`;
    const open = pdfOpen?.id === d.id;
    // made before the X4 fixes: one file too long for its memory, or 4 greys (96 KB per page)
    const risky = d.status === "done" && (d.parts?.some((p) => p.pages > 600) || o.depth == 2);
    const warn = risky ? `<div class="note">⚠ ${o.depth == 2 ? "เทา 4 ระดับใช้หน่วยความจำเครื่องมาก" : "ไฟล์ยาวเกินที่ X4 เปิดได้"} อาจขึ้น Memory error บน X4 · กด "ดูตัวอย่าง / ปรับ" แล้วแปลงใหม่เป็นขาวดำ</div>` : "";
    const partList = nparts > 1 ? `<details class="pdf-parts"><summary class="small">${nparts} ไฟล์ (ส่งเข้าโฟลเดอร์ Books/${esc(d.name)}) · ดาวน์โหลดทีละไฟล์</summary>
        <div class="pdf-part-links">${d.parts.map((p, k) => `<a href="/api/pdf/${d.id}/download?part=${k + 1}">${esc(p.name || String(k + 1).padStart(2, "0"))} · ${p.pages} หน้าจอ · ${(p.size / 1048576).toFixed(1)} MB</a>`).join("")}</div></details>` : "";
    const panel = open && d.status === "done" ? `
      <div class="pdf-panel">
        <img src="/api/pdf/${d.id}/preview/${pdfOpen.n}" alt="ตัวอย่างหน้าจอ ${pdfOpen.n + 1}">
        <div class="row"><button data-p="-1" aria-label="หน้าก่อน">‹</button><span class="muted small">หน้าจอ ${pdfOpen.n + 1}/${d.out_pages} · แสดงแบบตั้งตรงตามที่อ่าน</span><button data-p="1" aria-label="หน้าถัดไป">›</button></div>
        <div class="row">
          ${optSel("o-mode", o.mode, Object.entries(MODE_LABEL))}
          ${optSel("o-rotate", o.rotate, [["cw", "หมุนทวนเข็ม"], ["ccw", "หมุนตามเข็ม"]])}
          ${optSel("o-depth", o.depth, [["1", "ขาวดำ"], ["2", "เทา 4 ระดับ"]])}
          <label class="small"><input type="checkbox" class="o-trim" ${o.trim ? "checked" : ""}> ตัดหัว/เลขหน้า</label>
          <button data-a="redo">แปลงใหม่ด้วยค่านี้</button>
        </div>
      </div>` : "";
    return `
    <div class="job pdf-item" data-id="${d.id}">
      <div class="job-title">${esc(d.name)}</div>
      <div class="job-side"><span class="chip" data-s="${d.status}">${PDF_STATUS[d.status] || d.status}</span></div>
      <div class="job-meta">${esc(meta)}</div>
      ${warn}
      ${["queued", "converting"].includes(d.status) ? `<div class="bar"><div style="width:${pct}%"></div></div>` : ""}
      <div class="pdf-actions">
        ${d.status === "done" ? `<button class="primary" data-a="dl">${icon("download")}${nparts > 1 ? "ดาวน์โหลดทั้งหมด (.zip)" : "ดาวน์โหลด"}</button>
          <button data-a="preview">${open ? "ซ่อนตัวอย่าง" : "ดูตัวอย่าง / ปรับ"}</button>` : ""}
        ${d.status === "error" ? `<button data-a="retry">ลองใหม่</button>` : ""}
        ${d.status === "done" ? `<button data-a="x4">${x4Label(`pdf:${d.id}`)}</button>` : ""}
        <button data-a="rename">ชื่อ / โฟลเดอร์</button>
        <button class="danger" data-a="del">ลบ</button>
      </div>
      ${partList}
      ${panel}
    </div>`;
  }).join("");
  box.querySelectorAll(".pdf-item").forEach((el) => {
    const d = items.find((x) => x.id === el.dataset.id);
    el.querySelectorAll("[data-a]").forEach((b) => (b.onclick = () => pdfAction(d, b.dataset.a, el)));
    el.querySelectorAll("[data-p]").forEach((b) => (b.onclick = () => {
      pdfOpen.n = Math.min(d.out_pages - 1, Math.max(0, pdfOpen.n + +b.dataset.p)); loadPdfs();
    }));
  });
}

async function pdfAction(d, a, el) {
  try {
    if (a === "dl") { location.href = `/api/pdf/${d.id}/download`; return; }
    if (a === "x4") return sendX4("pdf", d.id);
    if (a === "rename") {
      const name = prompt("ชื่อไฟล์บน X4 (ใส่เลขลำดับข้างหน้าเพื่อเรียง เช่น 01 แค้นพยาบาท)", d.name);
      if (name === null) return;
      const folder = prompt("โฟลเดอร์ใน Books สำหรับหนังสือชุด (เช่น Sherlock Holmes) · เว้นว่าง = Books", d.options.folder || "");
      if (folder === null) return;
      await api(`/api/pdf/${d.id}`, { method: "PATCH", body: JSON.stringify({ name: name.trim() || d.name, folder }) });
      toast("บันทึกแล้ว"); pdfSig = ""; return loadPdfs();
    }
    if (a === "preview") { pdfOpen = pdfOpen?.id === d.id ? null : { id: d.id, n: 0 }; return loadPdfs(); }
    if (a === "del") {
      if (!confirm(`ลบ "${d.name}"?`)) return;
      await api(`/api/pdf/${d.id}`, { method: "DELETE" });
      if (pdfOpen?.id === d.id) pdfOpen = null;
    }
    if (a === "redo" || a === "retry") {
      const o = a === "retry" ? d.options : {
        mode: el.querySelector(".o-mode").value, rotate: el.querySelector(".o-rotate").value,
        depth: +el.querySelector(".o-depth").value, trim: el.querySelector(".o-trim").checked,
      };
      await api(`/api/pdf/${d.id}/convert`, { method: "POST", body: JSON.stringify(o) });
      if (a === "redo") pdfOpen = { id: d.id, n: 0 };
      toast("กำลังแปลงใหม่");
    }
    loadPdfs();
  } catch (e) { toast(e.message, "err"); }
}

// ------------------------------------------------------------------------------------------- send to X4
let x4Sends = {};
async function refreshSends() { try { x4Sends = await api("/api/x4/sends"); } catch { x4Sends = {}; } }
function x4Label(key) {
  const s = x4Sends[key];
  if (s?.state === "queued") return `ในคิว ลำดับ ${s.place || "?"}`;
  if (s?.state === "sending") return `ส่งอยู่ ${s.total ? Math.round(100 * s.sent / s.total) : 0}%`;
  if (s?.state === "done") return "ส่งแล้ว ✓ ส่งอีกครั้ง";
  if (s?.state === "error") return "ส่งไม่สำเร็จ ลองใหม่";
  return "ส่งเข้า X4";
}
async function sendX4(kind, id) {
  try {
    const st = await api("/api/x4");
    if (!st.ok) { toast(st.error || "ต่อ X4 ไม่ได้", "err"); return; }
    const s = await api(`/api/x4/send/${kind}/${id}`, { method: "POST" });
    toast(s.state === "queued" ? "เข้าคิวส่งแล้ว อย่าเพิ่งปิดหน้า File Transfer บน X4" : "กำลังส่งเข้า X4…");
    watchQueue();
  } catch (e) { toast(e.message, "err"); }
}
let queueTimer = null, queueSeen = {};
function watchQueue() {
  if (queueTimer) return;
  queueTimer = setInterval(async () => {
    await refreshSends();
    renderQueue();
    if (view === "pdf") { pdfSig = ""; loadPdfs(); } else if (current) refreshJob();
    for (const [k, s] of Object.entries(x4Sends)) {       // tell once when an item finishes
      if (queueSeen[k] && queueSeen[k] !== s.state && ["done", "error"].includes(s.state)) toast(`${s.title}: ${s.message}`, s.state === "error" ? "err" : "");
      queueSeen[k] = s.state;
    }
    if (!Object.values(x4Sends).some((s) => ["queued", "sending"].includes(s.state))) { clearInterval(queueTimer); queueTimer = null; }
  }, 1500);
}
function renderQueue() {
  const box = $("#x4-queue");
  if (!box) return;
  const items = Object.entries(x4Sends).sort((a, b) => (a[1].place ?? (a[1].state === "sending" ? 0 : 999)) - (b[1].place ?? (b[1].state === "sending" ? 0 : 999)));
  const active = items.filter(([, s]) => ["queued", "sending"].includes(s.state));
  const recent = items.filter(([, s]) => !["queued", "sending"].includes(s.state)).slice(-4);
  box.innerHTML = [...active, ...recent].map(([k, s]) => {
    const pct = s.total ? Math.round(100 * s.sent / s.total) : 0;
    const files = s.files > 1 ? ` · ${s.done_files}/${s.files} ไฟล์` : "";
    const what = s.state === "queued" ? `รอคิว ลำดับ ${s.place}` : s.state === "sending" ? `${pct}%${files}` : s.message;
    return `<div class="q-item" data-s="${s.state}"><div class="q-title">${esc(s.title)}</div>
      <div class="q-meta">${esc(what)}${s.state === "queued" ? ` <button class="link" data-cancel="${k}">ยกเลิก</button>` : ""}</div>
      ${s.state === "sending" ? `<div class="bar"><div style="width:${pct}%"></div></div>` : ""}</div>`;
  }).join("");
  box.querySelectorAll("[data-cancel]").forEach((b) => (b.onclick = async () => {
    const [kind, id] = b.dataset.cancel.split(":");
    await api(`/api/x4/cancel/${kind}/${id}`, { method: "POST" }); await refreshSends(); renderQueue();
  }));
}
$("#x4-all").onclick = async () => {
  try {
    const st = await api("/api/x4");
    if (!st.ok) { toast(st.error || "ต่อ X4 ไม่ได้", "err"); return; }
    const r = await api("/api/x4/send-all", { method: "POST" });
    toast(`เข้าคิว ${r.queued.length} เล่ม · ไฟล์ที่มีในเครื่องแล้วจะข้ามไป`);
    watchQueue();
  } catch (e) { toast(e.message, "err"); }
};
async function renameJob() {
  const name = prompt("ชื่อหนังสือ", currentJob?.name || "");
  if (!name || name.trim() === currentJob?.name) return;
  await api(`/api/jobs/${current}`, { method: "PATCH", body: JSON.stringify({ name: name.trim() }) });
  toast("เปลี่ยนชื่อแล้ว"); refreshJob();
}
async function checkX4() {
  const el = $("#x4-state");
  el.textContent = "กำลังเช็ก…"; el.className = "muted small";
  try {
    const s = await api("/api/x4");
    if (document.activeElement !== $("#x4-addr")) $("#x4-addr").value = s.address || "";
    x4Sends = s.sends || {}; renderQueue();
    if (Object.values(x4Sends).some((q) => ["queued", "sending"].includes(q.state))) watchQueue();
    el.textContent = s.ok ? `ต่อได้ · ${s.version ? "CrossPoint " + s.version : ""} · Wi-Fi ${s.rssi ?? "?"} dBm` : (s.error || "ต่อไม่ได้");
    el.className = "small " + (s.ok ? "ok-text" : "muted");
  } catch (e) { el.textContent = e.message; }
}
$("#x4-check").onclick = async () => {
  const addr = $("#x4-addr").value.trim();
  if (addr) await api("/api/x4", { method: "PUT", body: JSON.stringify({ address: addr }) });
  checkX4();
};
$("#opds-url").textContent = `${location.protocol}//${location.host}/opds`;

// ------------------------------------------------------------------------------------------- loop
health(); loadJobs();
if (location.hash === "#pdf") { setView("pdf"); loadPdfs(); checkX4(); }
else if (location.hash.length > 1) openJob(location.hash.slice(1));
else setView("books");
setInterval(() => (current ? refreshJob() : view === "pdf" ? loadPdfs() : loadJobs()).catch(() => {}), 2000);
