// Overview: the "Right now" card refreshes itself every few seconds while the page is in view.
// Opened as a Telegram Mini App: Telegram's script, its haptics and back button, header colours that match the page, no
// swipe-to-close while scrolling, full screen on phones, and an "add to home screen" button in the settings.
let tg = false
try { tg = location.hash.includes("tgWebAppData") || !!sessionStorage.getItem("__telegram__initParams") } catch (e) {}

// Limits card with several accounts: the device remembers whether the per-account view was open
const accts = document.getElementById("accts")
if (accts) accts.addEventListener("toggle", () => { try { localStorage.accts = accts.open ? "1" : "" } catch (e) {} })

setInterval(async () => {
  const card = document.getElementById("live")
  if (!card || document.hidden) return
  try {
    const r = await fetch("/live?p=" + encodeURIComponent(card.dataset.p), { cache: "no-store" })
    if (r.ok) card.outerHTML = await r.text()
  } catch (e) {}
}, 5000)

if (tg) {
  const s = document.createElement("script")
  s.src = "https://telegram.org/js/telegram-web-app.js"
  s.onload = () => window.Telegram && mini(window.Telegram.WebApp)
  document.head.append(s)
}

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
  if (location.pathname !== "/") {
    W.BackButton.show()
    const up = document.querySelector("a.back")   // a settings sub-page: back means the settings list, not the last POST
    W.BackButton.onClick(() => up ? location.assign(up.href) : history.length > 1 ? history.back() : location.assign("/"))
  }
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
