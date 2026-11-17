"""Mascots for the limits card: inline SVG, 5 kinds x 4 states (calm, work, tight, limit). Colors are light-dark()
variables and the motion is keyframes, both in static/style.css under .pet; prefers-reduced-motion stops it all."""
import math
import re

KINDS = ("terminal", "duck", "rack", "coffee", "bot")
STATES = ("calm", "work", "tight", "limit")
INK, ACC, ORL, ORD, BODY, GREY = "var(--pet-ink)", "var(--acc)", "var(--pet-orl)", "var(--pet-ord)", "var(--pet-body)", "var(--pet-grey)"
PAPER, SCR, GLOW, SOFT, TINT, CARD = "var(--pet-paper)", "var(--pet-scr)", "var(--pet-glow)", "var(--soft)", "var(--tint)", "var(--card)"
BREW, FOAM, WATER = "var(--pet-brew)", "var(--pet-foam)", "var(--track)"
SW = f'stroke="{INK}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"'
MONO = 'font-family="ui-monospace,Menlo,Consolas,monospace" font-weight="700"'


def cl(c):
    return f' class="{c}"' if c else ""


def eyes(x1, x2, y, st):
    if st == "calm":
        return f'<path d="M{x1-4} {y+1} q4 -5 8 0 M{x2-4} {y+1} q4 -5 8 0" {SW} fill="none"/>'
    if st == "work":
        return (f'<g class="pet-blink"><circle cx="{x1}" cy="{y}" r="3.6" fill="{INK}"/><circle cx="{x2}" cy="{y}" r="3.6" fill="{INK}"/>'
                f'<circle cx="{x1+1.2:g}" cy="{y-1.3:g}" r="1.2" fill="{PAPER}"/><circle cx="{x2+1.2:g}" cy="{y-1.3:g}" r="1.2" fill="{PAPER}"/></g>')
    if st == "tight":
        return (f'<circle cx="{x1}" cy="{y}" r="5.5" fill="{PAPER}" stroke="{INK}" stroke-width="2"/><circle cx="{x2}" cy="{y}" r="5.5" fill="{PAPER}" stroke="{INK}" stroke-width="2"/>'
                f'<g class="pet-dart"><circle cx="{x1}" cy="{y+.5:g}" r="2.2" fill="{INK}"/><circle cx="{x2}" cy="{y+.5:g}" r="2.2" fill="{INK}"/></g>')
    return f'<path d="M{x1-4} {y} q4 3 8 0 M{x2-4} {y} q4 3 8 0" {SW} fill="none"/>'   # asleep


def mouth(x, y, st):
    return {"calm": f'<path d="M{x-4} {y} q4 4 8 0" {SW} fill="none"/>',
            "work": f'<ellipse class="pet-talk" cx="{x}" cy="{y+1}" rx="2.8" ry="2.4" fill="{INK}"/>',
            "tight": f'<path d="M{x-7} {y+1} q3.5 -3 7 0 t7 0" {SW} fill="none"/>',
            "limit": f'<path d="M{x-3} {y+1} h6" {SW}/>'}[st]


def screen(face, off=False):
    """A face drawn on a dark display: the strokes glow, eye highlights take the screen color."""
    return face.replace(INK, GREY if off else GLOW).replace(f'fill="{PAPER}"', f'fill="{SCR}"')


def sweat(x, y):
    return f'<path class="pet-drip" d="M{x} {y} q-5 8 0 11 q5 -3 0 -11z" fill="{PAPER}" stroke="{GREY}" stroke-width="2"/>'


def zz(x, y):
    return (f'<text class="pet-zz" x="{x}" y="{y}" font-size="15" font-weight="700" fill="{GREY}">z</text>'
            f'<text class="pet-zz pet-d2" x="{x+8}" y="{y-6}" font-size="11" font-weight="700" fill="{GREY}">z</text>')


def terminal(st, w):
    frame = (f'<path d="M50 86 h20 l4 12 h-28z" fill="{BODY}" {SW}/><rect x="40" y="97" width="40" height="6" rx="3" fill="{BODY}" {SW}/>'
             f'<rect x="16" y="22" width="88" height="66" rx="11" fill="{BODY}" {SW}/><rect x="24" y="30" width="72" height="50" rx="6" fill="{SCR}"/>')
    t = lambda x, y, s, col=ACC, c="", fs=14: f'<text{cl(c)} x="{x}" y="{y}" font-size="{fs}" {MONO} fill="{col}">{s}</text>'
    if st == "calm":
        return f'<g class="pet-breathe">{frame}{t(41, 54, "^ ^")}{t(30, 72, "&gt;", fs=12)}<rect class="pet-cursor" x="40" y="63" width="7" height="10" fill="{ACC}"/></g>'
    if st == "work":   # code scrolls behind the face
        widths = (30, 22, 40, 18, 34, 26, 38, 20, 30, 22, 40, 18)
        bars = "".join(f'<rect x="{30+(i%3)*4}" y="{34+i*8}" width="{x}" height="3.5" rx="1.75" fill="{ACC if i % 4 == 1 else GREY}" opacity=".7"/>'
                       for i, x in enumerate(widths))
        return (f'{frame}<defs><clipPath id="pet-clip-terminal"><rect x="24" y="30" width="72" height="50" rx="6"/></clipPath></defs>'
                f'<g clip-path="url(#pet-clip-terminal)"><g class="pet-code">{bars}</g></g>'
                f'<rect x="28" y="40" width="64" height="22" rx="4" fill="{SCR}" opacity=".85"/>{t(41, 56, "o o", c="pet-blink")}')
    if st == "tight":
        return f'<g class="pet-shake">{frame}{t(41, 54, "O O")}{t(31, 72, f"! {round(w)}%", c="pet-blinkfast", fs=11)}</g>{sweat(104, 14)}'
    return f'{frame}{t(43, 58, "- -", col=GREY)}<circle class="pet-standby" cx="96" cy="84" r="2.2" fill="{ACC}"/>{zz(100, 24)}'


def duck(st, w):   # rubber-duck debugging
    water = (f'<defs><clipPath id="pet-clip-duck-{st}"><rect x="2" y="80" width="116" height="32"/></clipPath></defs>'
             f'<g clip-path="url(#pet-clip-duck-{st})"><g class="pet-wave"><path d="M-6 100 q8 -5 16 0 t16 0 t16 0 t16 0 t16 0 t16 0 t16 0 t16 0 t16 0 t16 0 v14 h-160z" fill="{WATER}"/></g></g>')
    eye = {"calm": f'<path d="M77 47 q4 -4 8 0" {SW} fill="none"/>',
           "work": f'<g class="pet-blink"><circle cx="81" cy="46" r="3.6" fill="{INK}"/><circle cx="82.2" cy="44.7" r="1.2" fill="{PAPER}"/></g>',
           "tight": f'<circle cx="81" cy="46" r="5.5" fill="{PAPER}" stroke="{INK}" stroke-width="2"/><circle cx="82" cy="46.5" r="2.2" fill="{INK}"/>',
           "limit": f'<path d="M77 47 q4 3 8 0" {SW} fill="none"/>'}[st]
    body = (f'<path d="M24 74 l-8 -12 l14 6z" fill="{ORL}" {SW}/><ellipse cx="54" cy="82" rx="32" ry="17" fill="{ORL}" {SW}/>'
            f'<path d="M40 78 q14 12 28 0" fill="none" {SW}/><circle cx="76" cy="52" r="17" fill="{ORL}" {SW}/>'
            f'<path d="M90 51 q12 -3 15 3 q-6 7 -15 3z" fill="{ORD}" {SW}/>{eye}')
    if st == "limit":   # asleep with a night cap
        cap = f'<path d="M62 44 q8 -16 26 -6 q-2 -10 -18 -10 q-10 0 -8 16z" fill="{BODY}" {SW}/><circle cx="58" cy="34" r="4" fill="{ACC}"/>'
        return f'<g class="pet-bob2">{body}{cap}</g>{water}{zz(100, 30)}'
    bubble = ""
    if st in ("work", "tight"):
        txt, x = ("&lt;/&gt;", 9) if st == "work" else ("?!", 14)
        bubble = (f'<g class="{"pet-pop" if st == "work" else "pet-blinkfast"}"><rect x="2" y="12" width="40" height="24" rx="8" fill="{PAPER}" {SW}/>'
                  f'<path d="M30 36 l6 7 l1 -7" fill="{PAPER}" {SW}/><text x="{x}" y="29" font-size="12" {MONO} fill="{ACC}">{txt}</text></g>')
    c = {"calm": "pet-bob", "work": "pet-nod", "tight": "pet-shake"}[st]
    return f'{bubble}<g class="{c}">{body}</g>{water}' + (sweat(98, 22) if st == "tight" else "")


def rack(st, w):   # homelab: LCD face + disk LEDs, vent, two fans, network cable with packets
    fan = {"calm": "pet-fan3", "work": "pet-fan2", "tight": "pet-fan1", "limit": ""}[st]
    led = {"calm": "pet-pulse", "work": "pet-blinkfast", "tight": "pet-blinkfast", "limit": "pet-off"}[st]
    lc = ORD if st == "tight" else ACC
    rail = "".join(f'<rect x="{x}" y="18" width="7" height="88" rx="2" fill="{BODY}" {SW}/>'
                   + "".join(f'<circle cx="{x+3.5}" cy="{y}" r="1.3" fill="{GREY}"/>' for y in (30, 46, 62, 78, 94)) for x in (13, 100))
    face = screen(eyes(35, 47, 38 if st in ("calm", "limit") else 37, st), st == "limit")
    bays = "".join(f'<rect x="{60+i*9}" y="28" width="6" height="18" rx="1.5" fill="{TINT}" stroke="{INK}" stroke-width="1.6"/>'
                   f'<circle class="{led}" style="animation-delay:-{i*.13:.2f}s" cx="{63+i*9}" cy="42" r="1.5" fill="{lc}"/>' for i in range(4))
    u1 = f'<rect x="20" y="22" width="80" height="30" rx="3" fill="{BODY}" {SW}/><rect x="25" y="27" width="31" height="20" rx="3" fill="{SCR}"/>{face}{bays}'
    u2 = (f'<rect x="20" y="56" width="80" height="18" rx="3" fill="{BODY}" {SW}/>'
          + "".join(f'<path d="M{27+i*6} 61 v8" stroke="{GREY}" stroke-width="2" stroke-linecap="round"/>' for i in range(9))
          + "".join(f'<rect x="{80+i*6}" y="61" width="4" height="8" rx="1" fill="{SCR}"/>' for i in range(3)))
    blades = "".join(f'<path d="M0 0 q-3 -8 3 -10 q4 5 -3 10z" fill="{GREY}" transform="rotate({i*120})"/>' for i in range(3))
    fans = "".join(f'<circle cx="{x}" cy="89" r="10" fill="{TINT}" stroke="{INK}" stroke-width="2"/>'
                   f'<g transform="translate({x} 89)"><g{cl(fan)}>{blades}</g></g>' for x in (38, 62))
    u3 = (f'<rect x="20" y="78" width="80" height="22" rx="3" fill="{BODY}" {SW}/>{fans}'
          f'<circle class="{"pet-standby" if st == "limit" else led}" cx="88" cy="89" r="2.6" fill="{lc}"/>')
    pkt = "" if st == "limit" else f' class="{"pet-pkt2" if st == "calm" else "pet-pkt1"}" stroke-dasharray="3 4"'
    cable = (f'<path d="M100 66 q14 2 12 20 t6 22" fill="none" stroke="{GREY}" stroke-width="3" stroke-linecap="round"/>'
             f'<path d="M100 66 q14 2 12 20 t6 22" fill="none" stroke="{SOFT if st == "limit" else ACC}" stroke-width="1.6"{pkt}/>')
    body = f'{cable}{rail}<path d="M15 106 v5 M104 106 v5" {SW}/>{u1}{u2}{u3}'
    if st == "limit":
        return body + zz(96, 14)
    if st == "tight":   # heat rising over the rack
        heat = "".join(f'<path class="pet-heat" style="animation-delay:-{d}s" d="M{x} 16 q-4 -5 0 -10 q4 -5 0 -10" fill="none" stroke="{ACC}" '
                       f'stroke-width="2.2" stroke-linecap="round"/>' for x, d in ((40, 0), (60, .33), (80, .66)))
        return f'{heat}<g class="pet-shake">{body}</g>{sweat(8, 6)}'
    return body


def coffee(st, w):   # the fill level is what is left of the week
    glass = "M30 40 h52 v42 a16 16 0 0 1 -16 16 h-20 a16 16 0 0 1 -16 -16z"
    level = round(98 - 54 * min(max(100 - w, 0), 100) / 100, 1)   # full = 44, empty = 98 (the bottom of the mug)
    y = {"calm": 70, "work": 72, "tight": 64, "limit": 68}[st]
    face = eyes(49, 63, y, st) + mouth(56, {"calm": 78, "work": 80, "tight": 76, "limit": 76}[st], st)
    spoon = f'<g class="pet-stir"><path d="M64 60 L84 14" stroke="{GREY}" stroke-width="3.5" stroke-linecap="round"/></g>' if st == "work" else ""
    mug = (f'<defs><clipPath id="pet-clip-coffee-{st}"><path d="{glass}"/></clipPath></defs>'
           f'<path d="M82 50 h4 a11 11 0 0 1 0 22 h-4" fill="none" stroke="{INK}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>'
           f'<path d="{glass}" fill="{PAPER}"/><g clip-path="url(#pet-clip-coffee-{st})"><rect x="26" y="{level:g}" width="60" height="70" fill="{BREW}"/>'
           f'<rect x="26" y="{level:g}" width="60" height="5" fill="{FOAM}"/></g>{spoon}<path d="{glass}" fill="none" {SW}/>{face}')
    if st == "limit":
        return f'<g class="pet-breathe">{mug}</g>{zz(92, 30)}'
    steam = ""
    if st in ("calm", "work"):
        steam = "".join(f'<path class="{"pet-steam2" if st == "calm" else "pet-steam1"}" style="animation-delay:-{d}s" d="M{x} 34 q-4 -5 0 -10 q4 -5 0 -10" '
                        f'fill="none" stroke="{SOFT}" stroke-width="2.5" stroke-linecap="round"/>' for x, d in ((46, 0), (60, .5), (72, .25)))
    c = {"calm": "pet-breathe", "work": "", "tight": "pet-shake"}[st]
    return f'{steam}<g{cl(c)}>{mug}</g>' + (sweat(98, 30) if st == "tight" else "")


def bot(st, w):   # the chest battery is what is left of the week
    bars = 0 if st == "limit" else min(max(math.ceil((100 - w) / 25), 0), 4)
    cells = "".join(f'<rect{cl("pet-charge" if st == "limit" else "")} style="animation-delay:-{(3-i)*.5:g}s" x="{49+i*6}" y="88" width="4" height="9" rx="1" '
                    f'fill="{ORD if bars == 1 else ACC}" opacity="{1 if i < bars else .15}"/>' for i in range(4))
    if bars == 1:
        cells = f'<g class="pet-blinkfast">{cells}</g>'
    batt = (f'<rect x="46" y="85" width="27" height="15" rx="3" fill="{PAPER}" stroke="{INK}" stroke-width="2"/>'
            f'<rect x="73" y="89" width="3" height="7" rx="1" fill="{INK}"/>{cells}')
    ant = {"calm": "pet-pulse", "work": "pet-blinkfast", "tight": "pet-blinkfast", "limit": "pet-off"}[st]
    head = (f'<path d="M60 26 V16" {SW}/><circle class="{ant}" cx="60" cy="13" r="4.5" fill="{ACC}"/>'
            f'<rect x="34" y="26" width="52" height="40" rx="12" fill="{BODY}" {SW}/><rect x="40" y="36" width="40" height="22" rx="8" fill="{SCR}"/>'
            + screen(eyes(50, 70, {"calm": 50, "work": 49, "tight": 48, "limit": 50}[st], st)))
    body = f'<rect x="40" y="70" width="40" height="36" rx="10" fill="{BODY}" {SW}/>{batt}<path d="M40 82 l-8 8 M80 82 l8 8" {SW}/>'
    if st == "limit":   # on the charging cable
        return f'<path d="M80 96 q16 0 18 10 t14 4" fill="none" stroke="{INK}" stroke-width="2.5"/><g class="pet-breathe">{body}{head}</g>{zz(92, 22)}'
    gear = ""
    if st == "work":
        teeth = "".join(f'<rect x="-2" y="-11" width="4" height="5" fill="{GREY}" transform="rotate({i*45})"/>' for i in range(8))
        gear = f'<g transform="translate(100 36)"><g class="pet-gear">{teeth}<circle r="7" fill="{GREY}"/><circle r="2.5" fill="{CARD}"/></g></g>'
    c = {"calm": "pet-bob", "work": "", "tight": "pet-shake"}[st]
    return f'{gear}<g{cl(c)}>{body}{head}</g>' + (sweat(98, 30) if st == "tight" else "")


def paint(svg):
    """fill/stroke="var(--x)" -> style="fill:var(--x)": var() in SVG presentation attributes is not safe in every engine."""
    rx = r' (fill|stroke)="(var\(--[\w-]+\))"'

    def one(m):
        tag = m.group(0)
        css = ";".join(f"{k}:{v}" for k, v in re.findall(rx, tag))
        if not css:
            return tag
        tag = re.sub(rx, "", tag)
        if ' style="' in tag:
            return tag.replace(' style="', f' style="{css};', 1)
        return re.sub(r"^<\w+", lambda t: f'{t.group(0)} style="{css}"', tag)
    return re.sub(r"<\w+[^>]*>", one, svg)


def pet(kind, state, week_used):
    """One mascot as inline SVG. week_used (0-100) sets the coffee level, the bot's battery and the terminal's warning."""
    draw = {"terminal": terminal, "duck": duck, "rack": rack, "coffee": coffee, "bot": bot}[kind]
    return (f'<svg class="pet-{state}" viewBox="0 0 120 120" width="64" height="64" aria-hidden="true" focusable="false">'
            f'{paint(draw(state, week_used or 0))}</svg>')
