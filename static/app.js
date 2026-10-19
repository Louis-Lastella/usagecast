// Overview: the "Right now" card refreshes itself every few seconds while the page is in view.
// Opened as a Telegram Mini App: Telegram's script, its haptics and back button, header colours that match the page, no
// swipe-to-close while scrolling, full screen on phones, and an "add to home screen" button in the settings.
let tg = false
try { tg = location.hash.includes("tgWebAppData") || !!sessionStorage.getItem("__telegram__initParams") } catch (e) {}

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
    W.BackButton.onClick(() => history.length > 1 ? history.back() : location.assign("/"))
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
