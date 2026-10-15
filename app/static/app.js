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
async function loadJobs() {
  const jobs = await api("/api/jobs");
  const box = $("#jobs");
  if (!jobs.length) { box.innerHTML = '<div class="muted">ยังไม่มีงาน</div>'; return; }
  box.innerHTML = jobs.map((j) => `
    <div class="job" data-id="${j.id}">
      <div><b>${esc(j.name)}</b><div class="muted small">${esc(j.message || "")}</div></div>
      <div><span class="badge">${STATUS[j.status] || j.status}</span> <b>${j.percent}%</b></div>
      <div class="bar"><div style="width:${j.percent}%"></div></div>
    </div>`).join("");
  box.querySelectorAll(".job").forEach((el) => (el.onclick = () => openJob(el.dataset.id)));
}

// ------------------------------------------------------------------------------------------- job view
let current = null, currentJob = null, tab = "glossary";
let glossary = [], glossaryDirty = false;

function openJob(id) {
  current = id; glossary = []; glossaryDirty = false;
  location.hash = id;
  $("#list-view").classList.add("hidden"); $("#job-view").classList.remove("hidden");
  refreshJob().then(() => showTab(currentJob?.status === "done" || currentJob?.status === "translating" ? "preview" : "glossary"));
}
$("#back").onclick = () => { current = null; location.hash = ""; $("#job-view").classList.add("hidden"); $("#list-view").classList.remove("hidden"); loadJobs(); };

async function refreshJob() {
  if (!current) return;
  let j;
  try { j = await api(`/api/jobs/${current}`); } catch { $("#back").click(); return; }
  const prev = currentJob?.status;
  currentJob = j;
  $("#job-name").textContent = j.name;
  $("#job-status").textContent = STATUS[j.status] || j.status;
  const step = STEP_OF[j.status] ?? 0;
  document.querySelectorAll("#steps li").forEach((li, i) => { li.className = i < step || j.status === "done" ? "done" : i === step ? "now" : ""; });
  $("#job-msg").textContent = j.message || "";
  $("#job-msg").className = "msg" + (j.status === "error" ? " err" : "");
  const bar = $("#job-bar");
  if (j.status === "glossary" && j.glossary_progress?.[1]) {
    const [d, t] = j.glossary_progress;
    bar.firstElementChild.style.width = (100 * d / t) + "%";
    bar.lastElementChild.textContent = `glossary ${d}/${t}`;
  } else {
    bar.firstElementChild.style.width = j.percent + "%";
    bar.lastElementChild.textContent = `${j.percent}%`;
  }
  bar.classList.toggle("indeterminate", j.status === "glossary" && !j.glossary_progress?.[1]);
  const elapsed = j.started ? ((j.finished || Date.now() / 1000) - j.started) : null;
  $("#job-stats").innerHTML = [
    `ย่อหน้า <b>${j.done + j.flagged}/${j.segments}</b>`,
    `ต้องตรวจ <b>${j.flagged}</b>`,
    `glossary <b>${j.glossary_terms}</b> คำ`,
    j.chars_per_sec ? `ความเร็ว <b>${j.chars_per_sec}</b> ตัวอักษร/วินาที${j.gen_tps ? ` (${j.gen_tps} tok/s)` : ""}` : "",
    j.eta_seconds != null ? `เหลือประมาณ <b>${fmtDur(j.eta_seconds)}</b>` : "",
    elapsed ? `ใช้ไป <b>${fmtDur(elapsed)}</b>` : "",
    j.current_doc && j.status === "translating" ? `บท <b>${esc(j.current_doc)}</b>` : "",
  ].filter(Boolean).join("");
  $("#flag-count").textContent = j.flagged || "";
  renderActions(j);
  $("#g-start").classList.toggle("hidden", j.status !== "review");
  const prof = $("#g-profile");
  if (document.activeElement !== prof && !prof.dataset.dirty) prof.value = j.options.book_profile || "";
  if (prev !== j.status && (j.status === "review" || prev === "glossary")) loadGlossary();
  if (tab === "preview" && j.status === "translating") loadPreview(false);
}

function renderActions(j) {
  const a = [];
  const btn = (label, fn, cls = "") => a.push(`<button class="${cls}" data-fn="${fn}">${label}</button>`);
  if (j.status === "review") btn("เริ่มแปล", "start", "primary");
  if (["queued", "translating", "waiting", "glossary"].includes(j.status)) btn("หยุดชั่วคราว", "pause");
  if (j.status === "paused") btn("ทำต่อ", "resume", "primary");
  if (["error", "cancelled"].includes(j.status)) btn("ลองใหม่", "start", "primary");
  if (j.flagged && ["done", "paused", "review", "error"].includes(j.status)) btn(`แปลใหม่ ${j.flagged} ย่อหน้าที่มีปัญหา`, "retry-flagged");
  if (j.done + j.flagged > 0) {
    btn(j.status === "done" ? "⬇ ดาวน์โหลด EPUB ไทย" : "⬇ ดาวน์โหลด (เท่าที่แปลแล้ว)", "dl", j.status === "done" ? "primary" : "");
    btn("⬇ ฉบับสองภาษา", "dl-bi");
  }
  if (!["done", "cancelled"].includes(j.status)) btn("ยกเลิก", "cancel", "danger");
  const box = $("#job-actions");
  box.innerHTML = a.join("");
  box.querySelectorAll("button").forEach((b) => (b.onclick = () => action(b.dataset.fn)));
}

async function action(fn) {
  try {
    if (fn === "dl" || fn === "dl-bi") {
      location.href = `/api/jobs/${current}/download${fn === "dl-bi" ? "?bilingual=true" : "?bilingual=false"}`;
      return;
    }
    if (fn === "cancel" && !confirm("ยกเลิกงานนี้? ย่อหน้าที่แปลแล้วยังอยู่ ดาวน์โหลดได้")) return;
    if (fn === "start" && glossaryDirty) await saveGlossary();
    await api(`/api/jobs/${current}/${fn}`, { method: "POST" });
    refreshJob();
  } catch (e) { $("#job-msg").className = "msg err"; $("#job-msg").textContent = e.message; }
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
    <tr data-i="${i}" class="${g.source === "user" ? "user" : ""}">
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
  $("#g-msg").textContent = `บันทึกแล้ว ${r.terms} คำ`;
  await loadGlossary();
}
$("#g-profile").oninput = () => { $("#g-profile").dataset.dirty = "1"; $("#g-profile-msg").textContent = "ยังไม่ได้บันทึก"; };
$("#g-profile-save").onclick = async () => {
  try {
    await api(`/api/jobs/${current}`, { method: "PATCH", body: JSON.stringify({ book_profile: $("#g-profile").value }) });
    delete $("#g-profile").dataset.dirty;
    $("#g-profile-msg").textContent = "บันทึกแล้ว ใช้กับย่อหน้าที่ยังไม่ได้แปล";
    refreshJob();
  } catch (e) { $("#g-profile-msg").textContent = e.message; }
};
$("#g-save").onclick = () => saveGlossary().catch((e) => ($("#g-msg").textContent = e.message));
$("#g-start").onclick = async () => { await saveGlossary(); await action("start"); };

// ------------------------------------------------------------------------------------------- flagged
async function loadFlagged(offset) {
  const r = await api(`/api/jobs/${current}/segments?status=flagged&offset=${offset}&limit=20`);
  $("#flagged").innerHTML = r.items.length ? r.items.map(segHtml).join("") : '<div class="muted">ไม่มีย่อหน้าที่ต้องตรวจ 🎉</div>';
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
      try { await api(`/api/jobs/${current}/segments/${id}`, { method: "PUT", body: JSON.stringify({ out: el.querySelector("textarea").value }) }); reload(); refreshJob(); }
      catch (e) { msg.textContent = e.message; }
    };
    if (re) re.onclick = async () => {
      msg.textContent = "กำลังแปลใหม่…"; re.disabled = true;
      try { await api(`/api/jobs/${current}/segments/${id}/retranslate`, { method: "POST" }); reload(); refreshJob(); }
      catch (e) { msg.textContent = e.message; re.disabled = false; }
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
  box.innerHTML = r.items.filter((s) => s.kind !== "title").map(segHtml).join("");
  bindSegs(box, () => loadPreview(false));
  pager($("#p-pager"), r.total, previewOffset, 40, (o) => { previewOffset = o; loadPreview(true); });
}

// ------------------------------------------------------------------------------------------- settings
$("#s-save").onclick = async () => {
  try {
    await api(`/api/jobs/${current}`, { method: "PATCH", body: JSON.stringify({ bilingual: $("#s-bilingual").checked, style_notes: $("#s-notes").value }) });
    $("#s-msg").textContent = "บันทึกแล้ว"; refreshJob();
  } catch (e) { $("#s-msg").textContent = e.message; }
};
$("#s-regloss").onclick = () => confirm("ทำ glossary ใหม่? คำที่คุณแก้เองจะยังอยู่") && action("regenerate-glossary");
$("#s-delete").onclick = async () => {
  if (!confirm("ลบงานนี้และไฟล์ทั้งหมด?")) return;
  await api(`/api/jobs/${current}`, { method: "DELETE" });
  $("#back").click();
};

// ------------------------------------------------------------------------------------------- loop
health(); loadJobs();
if (location.hash.length > 1) openJob(location.hash.slice(1));
setInterval(() => (current ? refreshJob() : loadJobs()).catch(() => {}), 2000);
setInterval(health, 15000);
