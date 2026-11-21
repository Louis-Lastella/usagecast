// Overview: the "Right now" card refreshes itself every few seconds while the page is in view.
// Opened as a Telegram Mini App: Telegram's script, its haptics and back button, header colours that match the page, no
// swipe-to-close while scrolling, full screen on phones, and an "add to home screen" button in the settings.
let tg = false
try { tg = location.hash.includes("tgWebAppData") || !!sessionStorage.getItem("__telegram__initParams") } catch (e) {}

// Limits card with several accounts: the device remembers whether the per-account view was open
addEventListener("toggle", e => { if (e.target.id === "accts") try { localStorage.accts = e.target.open ? "1" : "" } catch (x) {} }, true)

setInterval(async () => {
  const card = document.getElementById("live")
  if (!card || document.hidden) return
  try {
    const r = await fetch("/live?p=" + encodeURIComponent(card.dataset.p), { cache: "no-store" })
    if (r.ok) { card.outerHTML = await r.text(); dispatchEvent(new Event("uc:swap")) }
  } catch (e) {}
}, 5000)

if (tg) {
  const s = document.createElement("script")
  s.src = "https://telegram.org/js/telegram-web-app.js"
  s.onload = () => window.Telegram && mini(window.Telegram.WebApp)
  document.head.append(s)
}

let tgBack   // Telegram's back button: shown on every page but the overview, also after a page swap
function backButton() { if (tgBack) location.pathname === "/" ? tgBack.hide() : tgBack.show() }

function hex(c) {
  const m = c.match(/\d+/g) || [0, 0, 0]
  return "#" + m.slice(0, 3).map(x => (+x).toString(16).padStart(2, "0")).join("")
}

function mini(W) {
  const at = v => W.isVersionAtLeast(v)
  document.documentElement.classList.add("tg")
  W.ready()
  W.expand()
  if (at("7.7")) W.disableVerticalSwipes()
  const c = getComputedStyle(document.body).backgroundColor
  const bg = hex(/, 0\)$/.test(c) ? getComputedStyle(document.documentElement).backgroundColor : c)
  if (at("6.9")) { W.setHeaderColor(bg); W.setBackgroundColor(bg) }
  if (at("7.10")) W.setBottomBarColor(bg)
  if (at("8.0") && /^(ios|android)$/.test(W.platform) && !W.isFullscreen) W.requestFullscreen()
  if (!at("6.1")) return
  tgBack = W.BackButton
  tgBack.onClick(() => {
    const up = document.querySelector("a.back")   // a settings sub-page: back means the settings list, not the last POST
    up ? location.assign(up.href) : history.length > 1 ? history.back() : location.assign("/")
  })
  backButton()
  addEventListener("click", e => {
    if (e.isTrusted && e.target.closest("nav a, a.gear, a.btn, a.job, button, summary, .set input[type=checkbox], .set input[type=radio]"))
      W.HapticFeedback.selectionChanged()
  })
  const home = document.getElementById("tg-home")
  if (home && at("8.0")) W.checkHomeScreenStatus(st => {
    if (st === "unsupported" || st === "added") return
    home.hidden = false
    home.onclick = () => W.addToHomeScreen()
  })
}

// Settings sub-pages save when a field changes, like iOS: the whole form goes out in one fetch (inside its section a
// missing checkbox means off), the server answers {ok, bad}. The Save button stays for browsers without JS; the action
// buttons (set up, test, new topic, ...) still submit normally.
function autoSave(form) {
  document.documentElement.classList.add("auto")
  const toast = document.createElement("div")
  toast.id = "saved"
  toast.setAttribute("role", "status")
  document.body.append(toast)
  let timer
  const say = (text, err) => {
    clearTimeout(timer)
    toast.textContent = text
    toast.classList.toggle("bad", err)
    toast.classList.add("on")
    timer = setTimeout(() => { toast.classList.remove("on"); timer = setTimeout(() => { toast.textContent = "" }, 250) }, 1500)
  }
  const note = (f, text) => {
    const box = f.closest(".field")?.firstElementChild || f.parentNode
    box.querySelector(".fnote")?.remove()
    f.toggleAttribute("aria-invalid", !!text)
    if (!text) return
    const n = document.createElement("small")
    n.className = "fnote"
    n.setAttribute("role", "alert")
    n.textContent = text
    box.append(n)
  }
  const post = async el => {
    try {
      const r = await fetch(form.getAttribute("action"), { method: "POST", headers: { "X-Usagecast-Auto": "1" }, body: new URLSearchParams(new FormData(form)) })
      const j = await r.json()
      if (!j.ok) throw new Error("not saved")
      for (const f of form.elements) {
        if (!f.name || !f.matches("input:not([type=checkbox],[type=radio],[type=hidden],[type=password]),select")) continue
        if (j.bad.includes(f.name)) { f.value = f.defaultValue; note(f, form.dataset.bad); continue }
        if (f.name in j.num) f.value = j.num[f.name]   // clamped by the server
        f.defaultValue = f.value
        note(f, "")
      }
      if (j.bad.includes(el.name)) return
      say(form.dataset.saved, false)
      if (el.name === "lang") location.replace(location.pathname)
      if (el.name === "theme") {
        if (el.value === "system") delete document.documentElement.dataset.theme
        else document.documentElement.dataset.theme = el.value
      }
    } catch (e) { say(form.dataset.fail, true) }
  }
  let queue = Promise.resolve()   // one request at a time: two quick changes must not reach the server out of order
  form.addEventListener("change", e => { if (e.target.matches("input:not([type=hidden]),select")) queue = queue.then(() => post(e.target)) })
  // Enter in a field: the change event has already saved it, so the hidden default button must not submit the page too
  form.addEventListener("submit", e => { if (e.submitter?.classList.contains("sr")) e.preventDefault() })
}
const setForm = document.querySelector("form.set")
if (setForm) autoSave(setForm)

// Instant page switches: a click on a page link swaps the page in place instead of loading it. Every page fetched
// lands in a device cache (localStorage, the last 30, dropped when the static files or language or theme change);
// a cached page shows at once and is swapped silently once the fresh copy arrives. An uncached one shows "bones" in
// main after 150 ms. Anything odd (network error, not 200, not HTML, a redirect) falls back to a normal page load.
// Not taken over: Wrapped, settings (own auto-save form), language links, the API, forms, new tabs.
const R = document.documentElement
const V = [new URL(document.currentScript.src).searchParams.get("v"), R.lang, R.dataset.theme].join()
const SKIP = /^\/(wrapped|settings|api\/|cc-report\.py)/
const BONES = '<div class="bones" aria-busy="true"><i class="b1"></i><i class="b2"></i>' +
  '<div class="card"><i></i><i></i><i></i></div>'.repeat(3) + '</div>'
const skip = u => u.origin !== location.origin || SKIP.test(u.pathname) || u.searchParams.has("lang")
let idx, cur = location.pathname + location.search, seq = 0, busy = 0
try { idx = JSON.parse(localStorage.uc) } catch (e) {}
if (!idx || idx.v !== V) {
  try { for (const k of Object.keys(localStorage)) if (k.startsWith("uc:")) localStorage.removeItem(k) } catch (e) {}
  idx = { v: V, l: [] }   // l = [[path, fetched at]], oldest first
}

function get(u) {
  const i = idx.l.find(x => x[0] === u)
  try { return i && { t: i[1], h: localStorage["uc:" + u] } } catch (e) {}
}

function put(u, h) {
  const l = idx.l = idx.l.filter(x => x[0] !== u)
  const drop = x => { try { localStorage.removeItem("uc:" + x[0]) } catch (e) {} }
  l.push([u, Date.now()])
  while (l.length > 30) drop(l.shift())
  for (;;) {
    try { localStorage["uc:" + u] = h; localStorage.uc = JSON.stringify(idx); return } catch (e) {}
    drop(l.shift())   // storage full: the oldest page goes, at last this one
    if (!l.length) return
  }
}

async function load(u) {
  const r = await fetch(u)
  const h = r.ok && !r.redirected && /^text\/html/.test(r.headers.get("content-type")) && await r.text()
  if (!h || !h.includes('<div class="app">')) throw new Error("no page")
  put(u, h)
  return h
}

function show(h, y, keep) {
  const d = new DOMParser().parseFromString(h, "text/html"), N = d.documentElement
  const open = keep ? [...document.querySelectorAll("main details")].map(x => x.open) : []
  document.title = d.title
  R.lang = N.lang
  N.dataset.theme ? R.dataset.theme = N.dataset.theme : delete R.dataset.theme
  R.classList.remove("auto")
  if (!y) scrollTo(0, 0)   // before the swap: afterwards it would force a layout right away
  document.querySelector(".app").replaceWith(document.adoptNode(d.querySelector(".app")))
  document.querySelectorAll("main details").forEach((x, i) => { if (i < open.length) x.open = open[i] })
  const a = document.getElementById("accts")   // its inline script does not run in a swapped page
  try { if (a && localStorage.accts) a.open = true } catch (e) {}
  if (y) scrollTo(0, y)
  backButton()
  dispatchEvent(new Event("uc:swap"))
}

async function go(u, push, y = 0) {
  const n = ++seq, c = get(u)
  if (push) { history.replaceState({ y: scrollY }, ""); history.pushState({ y: 0 }, "", u) }
  cur = u
  let t
  if (c?.h) show(c.h, y)
  else t = setTimeout(() => { if (n === seq) { document.querySelector("main").innerHTML = BONES; scrollTo(0, 0) } }, 150)
  try {
    const h = await load(u)
    clearTimeout(t)
    if (n !== seq) return
    if (!c?.h) show(h, y)
    else if (h !== c.h && !document.activeElement?.matches("input, select, textarea")) show(h, scrollY, true)
  } catch (e) {
    if (n === seq && !c?.h) return location.assign(u)   // a cached page stays when the refresh fails (offline)
  }
  prefetch()
}

// The main tabs (with the current period) that are missing or older than 2 minutes, one at a time, while in view
async function prefetch() {
  if (document.hidden) return addEventListener("visibilitychange", prefetch, { once: true })
  if (busy) return
  busy = 1
  for (const a of document.querySelectorAll("aside nav a")) {
    const u = new URL(a.href), k = u.pathname + u.search, c = get(k)
    if (skip(u) || document.hidden || c?.h && Date.now() - c.t < 120000) continue
    try { await load(k) } catch (e) {}
  }
  busy = 0
}

if (document.querySelector(".app")) {
  history.scrollRestoration = "manual"
  if (window.ucPage && !skip(new URL(location.href))) put(cur, window.ucPage)
  addEventListener("click", e => {
    const a = e.target.closest?.("a[href]")
    if (!a || e.defaultPrevented || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || a.target ||
        a.hasAttribute("download") || a.getAttribute("href").startsWith("#")) return
    const u = new URL(a.href)
    if (skip(u)) return
    e.preventDefault()
    const k = u.pathname + u.search
    go(k, k !== cur)
  })
  addEventListener("popstate", e => {
    const k = location.pathname + location.search
    if (k !== cur) go(k, false, e.state?.y)
  })
  prefetch()
}
