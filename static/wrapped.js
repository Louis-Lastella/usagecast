// Wrapped (/wrapped): swipe (CSS scroll-snap) or tap the left/right half (the .tz links) to move between slides; the
// progress bars follow. A slide replays its animation each time it comes into view and its big number counts up,
// unless the system asks for reduced motion. Haptics and the Telegram back button come from haptics.js and app.js.
const story = document.querySelector(".story")
const still = matchMedia("(prefers-reduced-motion: reduce)")
const slides = [...story.children], bars = document.querySelectorAll(".prog i"), [th, dec] = story.dataset.sep
let cur = 0

const go = i => story.scrollTo({ left: Math.max(0, Math.min(i, slides.length - 1)) * story.clientWidth, behavior: still.matches ? "auto" : "smooth" })

// "1,234.5M" -> counts from 0 to 1234.5 in the same format (the page's thousands and decimal separators)
const up = el => {
  const fin = el.dataset.fin ||= el.textContent, m = fin.match(/\d[\d.,]*\d|\d/)
  if (!m || still.matches) return
  const [a, f = ""] = m[0].split(dec), n = parseFloat(a.split(th).join("") + "." + f) || 0, t0 = performance.now()
  const step = now => {
    const p = Math.min(1, (now - t0) / 800), [i, d] = (n * (1 - (1 - p) ** 3)).toFixed(f.length).split(".")
    el.textContent = fin.replace(m[0], i.replace(/\B(?=(\d{3})+$)/g, th) + (d ? dec + d : ""))
    if (p < 1) requestAnimationFrame(step)
  }
  requestAnimationFrame(step)
}

const seen = new IntersectionObserver(es => es.forEach(x => {
  const on = x.intersectionRatio >= 0.02
  if (on && !x.target.classList.contains("on")) x.target.querySelectorAll(".up").forEach(up)
  x.target.classList.toggle("on", on)   // CSS animations restart when "on" comes back
}), { root: story, threshold: 0.02 })
slides.forEach(s => seen.observe(s))

story.addEventListener("scroll", () => {
  const i = Math.round(story.scrollLeft / story.clientWidth)
  if (i !== cur) window.ucTick?.()   // Telegram and Android: a tick per slide (iOS Safari ticks on the tap zones only)
  cur = i
  bars.forEach((b, j) => b.classList.toggle("on", j <= cur))
}, { passive: true })
addEventListener("click", e => {
  const a = e.target.closest(".tz a")
  if (!a) return
  e.preventDefault()   // no #s3 history entries: Telegram's back button leaves the page instead of stepping back
  go(cur + (a.classList.contains("next") ? 1 : -1))
})
addEventListener("keydown", e => { if (e.key === "ArrowRight" || e.key === "ArrowLeft") go(cur + (e.key === "ArrowRight" ? 1 : -1)) })
