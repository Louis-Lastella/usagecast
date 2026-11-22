// The page exactly as the server sent it, before the labels below change it: static/app.js keeps it in its page cache.
window.ucPage = document.documentElement.outerHTML
// A short vibration on every tap of a control: links, tabs, chips, rows, buttons, folds, switches, radios. Not on text
// fields, selects and date inputs (their native picker must open; iOS gives its own feedback there).
// iOS Safari has no Vibration API, but its <input type=checkbox switch> vibrates when it is toggled through a label
// the finger really touched (a script .click() stopped counting with iOS 26.5). So on iOS every control gets a
// transparent label on top, tied to a hidden switch: the tap hits the label, Safari vibrates, and because the label
// swallows the tap's normal action, the script clicks the control itself. Same trick as github.com/tijnjh/ios-haptics.
// In the Telegram Mini App Telegram's own HapticFeedback ticks instead, on Android navigator.vibrate. The system
// setting for haptics still applies. window.ucTick(kind) is for events without a tap (Wrapped swipe, settings saved):
// it works in Telegram and on Android; plain iOS Safari cannot vibrate without a touch.
const SEL = "a[href], button:not(.sr), summary, input[type=checkbox]:not(#haptic), input[type=radio]"
const ios = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1)
let tg = false
try { tg = location.hash.includes("tgWebAppData") || !!sessionStorage.getItem("__telegram__initParams") } catch (e) {}

window.ucTick = kind => {
  if (tg) {
    const W = window.Telegram?.WebApp, h = W?.isVersionAtLeast?.("6.1") && W.HapticFeedback
    if (!h) return
    kind === "ok" ? h.notificationOccurred("success") : kind === "bad" ? h.notificationOccurred("error") : h.selectionChanged()
  } else if (!ios && navigator.vibrate) navigator.vibrate(kind === "bad" ? [20, 60, 20] : 8)
}

// iOS shows :active (the press state in style.css) only when some touchstart listener exists
addEventListener("touchstart", () => {}, { passive: true })

if (tg || !ios) {
  // Wrapped's tap zones tick through wrapped.js when the slide changes, so a tap there does not tick twice
  addEventListener("click", e => { if (e.isTrusted && e.target.closest(SEL) && !e.target.closest(".tz")) ucTick() })
} else {
  // One hidden switch outside every control: its copy of the tap must not bubble through a button (that submitted twice).
  // Hidden and out of the way, since WebKit treats a touchstart on it as handled and would cancel the scroll.
  const sw = document.createElement("input")
  sw.type = "checkbox"
  sw.setAttribute("switch", "")
  sw.id = "haptic"
  sw.tabIndex = -1
  sw.setAttribute("aria-hidden", "true")
  sw.style.cssText = "position:fixed;top:0;left:0;width:1px;height:1px;margin:0;visibility:hidden"
  sw.addEventListener("click", e => e.stopPropagation())
  document.body.append(sw)
  label()
  addEventListener("uc:swap", label)   // static/app.js swapped in a new page or card
}

function label() {
  for (const el of document.querySelectorAll(SEL)) {
    if (el.disabled || el.hap) continue
    el.hap = 1
    // A radio is covered with its label (the visible segment or row). Buttons and bare switches get a wrapper and the
    // label goes next to them, not inside: a tap on a label inside a button still submits in Chromium.
    let host = el.matches("a, summary") ? el : el.type === "radio" ? el.closest("label") : null
    if (!host) {
      host = document.createElement("span")
      host.className = "hw"
      host.style.display = "inline-flex"
      el.before(host)
      host.append(el)
    }
    if (getComputedStyle(host).position === "static") host.style.position = "relative"
    const tap = document.createElement("label")
    tap.htmlFor = "haptic"
    tap.setAttribute("aria-hidden", "true")
    tap.style.cssText = "position:absolute;inset:0"
    // el.click() only after the label's own click is done: a page link swaps the page at once (static/app.js), and a
    // label that has left the page no longer toggles the switch (no vibration) and lets the link load the page.
    tap.addEventListener("click", e => { e.stopPropagation(); setTimeout(() => el.click()) })
    host.append(tap)
  }
}
