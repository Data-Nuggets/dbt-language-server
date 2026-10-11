const DATA = JSON.parse(document.getElementById("data").textContent);
const KEY = "dbt-ls-todo-v1";
let done = {};
try { done = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) { done = {}; }
// A tick already in todo.md counts unless this browser has its own answer for that item.
DATA.groups.forEach((g) => g.tasks.forEach((t) => [t, ...t.subs].forEach((u) => { if (u.checked && !(u.key in done)) done[u.key] = true; })));
const save = () => { try { localStorage.setItem(KEY, JSON.stringify(done)); } catch (e) {} };
const el = (tag, cls, html) => { const n = document.createElement(tag); if (cls) n.className = cls; if (html != null) n.innerHTML = html; return n; };
const units = (task) => (task.subs.length ? task.subs : [task]);
const isDone = (task) => units(task).every((u) => done[u.key]);
let filter = "all", query = "";
const groupsEl = document.getElementById("groups");
const views = [];

DATA.groups.forEach((group, gi) => {
  const section = el("section"); section.id = "g" + gi; section.dataset.toc = group.title;
  const head = el("div", "group-head");
  head.append(el("h2", "", group.title), el("span", "count"));
  const bar = el("div", "bar", "<i></i>"); bar.setAttribute("aria-hidden", "true");
  section.append(head, bar);
  const gv = { group, section, count: head.lastChild, fill: bar.firstChild, tasks: [], empty: el("p", "empty", "Nothing here matches the filter.") };
  group.tasks.forEach((task, ti) => {
    const card = el("div", "task"), row = el("div", "row");
    const box = el("input"); box.type = "checkbox"; box.id = `t${gi}-${ti}`;
    const main = el("div");
    const label = el("label", "title", task.title); label.htmlFor = box.id;
    main.append(label);
    const tags = el("div", "tags");
    if (task.id) tags.append(el("span", "badge b-id", task.id));
    task.findings.forEach((f) => tags.append(el("span", "badge b-" + f.kind, `${f.id} · ${f.kind === "confirmed" ? "confirmed" : "read from code"}`)));
    const subTag = task.subs.length ? el("span", "badge b-sub") : null;
    if (subTag) tags.append(subTag);
    if (tags.children.length) main.append(tags);
    row.append(box, main);
    const hasBody = task.body || task.subs.length;
    const body = el("div", "body"); body.id = box.id + "-body"; body.hidden = true;
    let more = null;
    if (hasBody) {
      more = el("button", "more", "Details"); more.type = "button";
      more.setAttribute("aria-expanded", "false"); more.setAttribute("aria-controls", body.id);
      more.onclick = () => setOpen(tv, body.hidden);
      row.append(more);
      if (task.body) body.append(el("p", "", task.body));
    }
    const subViews = [];
    if (task.subs.length) {
      const list = el("ul", "subs");
      task.subs.forEach((sub, si) => {
        const li = el("li"), sbox = el("input"); sbox.type = "checkbox"; sbox.id = `${box.id}-${si}`;
        const text = el("label", "", `<span>${sub.html}</span>`); text.htmlFor = sbox.id;
        li.append(sbox, text); list.append(li);
        sbox.onchange = () => { done[sub.key] = sbox.checked; save(); render(); };
        subViews.push({ sub, li, box: sbox });
      });
      body.append(list);
    }
    card.append(row); if (hasBody) card.append(body);
    box.onchange = () => { units(task).forEach((u) => (done[u.key] = box.checked)); save(); render(); };
    const tv = { task, card, box, body, more, subTag, subViews, text: (task.text + " " + task.subs.map((s) => s.text).join(" ")).toLowerCase() };
    gv.tasks.push(tv); section.append(card);
  });
  section.append(gv.empty); groupsEl.append(section); views.push(gv);
});

function setOpen(tv, open) {
  if (!tv.more) return;
  tv.body.hidden = !open; tv.more.setAttribute("aria-expanded", String(open)); tv.more.textContent = open ? "Hide" : "Details";
}

function render() {
  let total = 0, totalDone = 0;
  for (const gv of views) {
    let n = 0, d = 0, shown = 0;
    for (const tv of gv.tasks) {
      const finished = isDone(tv.task); n++; if (finished) d++;
      const subDone = tv.subViews.filter((s) => done[s.sub.key]).length;
      tv.box.checked = finished; tv.box.indeterminate = !finished && subDone > 0;
      tv.card.classList.toggle("done", finished);
      tv.subViews.forEach((s) => { s.box.checked = !!done[s.sub.key]; s.li.classList.toggle("done", s.box.checked); });
      if (tv.subTag) tv.subTag.textContent = `${subDone} of ${tv.subViews.length} parts`;
      const visible = (filter === "all" || (filter === "done") === finished) && (!query || tv.text.includes(query));
      tv.card.hidden = !visible; if (visible) shown++;
    }
    gv.count.textContent = `${d} of ${n} done`;
    gv.fill.style.width = (n ? (100 * d) / n : 0) + "%";
    gv.empty.hidden = shown > 0;
    total += n; totalDone += d;
  }
  document.getElementById("total-bar").style.width = (total ? (100 * totalDone) / total : 0) + "%";
  document.getElementById("total-text").innerHTML = `<b>${totalDone} of ${total}</b> tasks done, ${total - totalDone} to go.`;
}

document.querySelectorAll("[data-filter]").forEach((button) => {
  button.onclick = () => {
    filter = button.dataset.filter;
    document.querySelectorAll("[data-filter]").forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
    render();
  };
});
document.getElementById("search").oninput = (e) => { query = e.target.value.trim().toLowerCase(); render(); };
const expand = document.getElementById("expand");
expand.onclick = () => {
  const open = expand.textContent === "Expand all";
  views.forEach((gv) => gv.tasks.forEach((tv) => setOpen(tv, open)));
  expand.textContent = open ? "Collapse all" : "Expand all";
};
const exportButton = document.getElementById("export");
exportButton.onclick = () => {
  const lines = DATA.lines.slice();
  const mark = (item, checked) => { lines[item.line] = lines[item.line].replace(/\[[ x]\]/, checked ? "[x]" : "[ ]"); };
  DATA.groups.forEach((g) => g.tasks.forEach((t) => { mark(t, isDone(t)); t.subs.forEach((s) => mark(s, !!done[s.key])); }));
  const text = lines.join("\n") + "\n";
  const finish = (label) => { exportButton.textContent = label; setTimeout(() => (exportButton.textContent = "Copy as todo.md"), 1500); };
  navigator.clipboard.writeText(text).then(() => finish("Copied"), () => finish("Copy failed"));
};

render();
