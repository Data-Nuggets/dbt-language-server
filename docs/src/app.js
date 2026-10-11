
// --- tabs, the contents list and copy buttons ---
const TAB_KEY = "dbt-ls-docs-tab";
const tabLinks = [...document.querySelectorAll("[data-tab]")];
const panels = new Map([...document.querySelectorAll("[data-panel]")].map((p) => [p.dataset.panel, p]));
const tocLinks = document.getElementById("toc-links");
const toc = document.querySelector(".toc");
let spy = null;

function buildToc(panel) {
  spy?.disconnect();
  tocLinks.replaceChildren();
  const byId = new Map();
  panel.querySelectorAll("section[data-toc]").forEach((section) => {
    const a = document.createElement("a");
    a.href = "#" + section.id; a.textContent = section.dataset.toc;
    tocLinks.append(a); byId.set(section.id, a);
  });
  toc.style.visibility = byId.size ? "" : "hidden";
  spy = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      byId.forEach((a) => a.classList.remove("active"));
      byId.get(entry.target.id)?.classList.add("active");
    }
  }, { rootMargin: "-15% 0px -70% 0px" });
  byId.forEach((_, id) => spy.observe(document.getElementById(id)));
}

function show(key, target) {
  if (!panels.has(key)) key = "overview";
  panels.forEach((panel, name) => (panel.hidden = name !== key));
  tabLinks.forEach((a) => {
    const on = a.dataset.tab === key;
    a.setAttribute("aria-selected", String(on)); a.tabIndex = on ? 0 : -1;
    if (on) a.scrollIntoView({ block: "nearest", inline: "nearest" });
  });
  buildToc(panels.get(key));
  try { localStorage.setItem(TAB_KEY, key); } catch (e) {}
  if (target) target.scrollIntoView(); else window.scrollTo(0, 0);
}

// "#tasks" names a tab; "#platforms" names a section, which may be in another tab.
function go(id, remember) {
  let key = id, target = null;
  if (!panels.has(id)) {
    target = id ? document.getElementById(id) : null;
    const panel = target?.closest("[data-panel]");
    if (!panel) return false;
    key = panel.dataset.panel;
  }
  show(key, target);
  if (remember) { try { history.replaceState(null, "", "#" + id); } catch (e) {} }
  return true;
}

document.addEventListener("click", (event) => {
  const link = event.target.closest("a[href^='#']");
  if (link && go(link.getAttribute("href").slice(1), true)) event.preventDefault();
});
window.addEventListener("hashchange", () => go(location.hash.slice(1), false));
document.querySelector(".tabs").addEventListener("keydown", (event) => {
  const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
  if (!step) return;
  const i = tabLinks.indexOf(document.activeElement);
  const next = tabLinks[(i + step + tabLinks.length) % tabLinks.length];
  next.focus(); go(next.dataset.tab, true);
});

document.querySelectorAll("[data-copy]").forEach((button) => {
  button.addEventListener("click", () => {
    const code = button.closest(".window").querySelector("pre").innerText;
    const done = (label) => { button.textContent = label; setTimeout(() => (button.textContent = "Copy"), 1200); };
    navigator.clipboard.writeText(code).then(() => done("Copied"), () => done("Copy failed"));
  });
});

let last = "overview";
try { last = localStorage.getItem(TAB_KEY) || last; } catch (e) {}
if (!go(location.hash.slice(1), false)) show(last);
