#!/usr/bin/env python3
"""Report progress of a long background job (e.g. a Blender render) to the Usagecast dashboard.

  progress.py set NAME [--title T] [--done N] [--total N] [--frac F] [--step S] [--detail D] [--pid P]
                       [--image PATH] [--state running|done|failed]
  blender --background ... | progress.py blender NAME [--frames START END]   # Blender log in, readable log out
  progress.py clear NAME
  progress.py --test

One small JSON file per job: $HERMES_HOME/progress/NAME.json (HERMES_HOME defaults to ~/.hermes). `set` changes only
the given fields (`--state running` also restarts the clock for the time-left estimate), so a batch script (done/total/step) and a filter (frac/detail) can write the same file.
`blender` parses Cycles 'Sample a/b' and Eevee 'Rendering a / b samples' lines, writes frac + detail at most once a
second and passes every other line through unchanged. --image stores a 1280 px jpeg thumbnail if Pillow is installed.
Standard library only (Pillow optional), Python 3.9 or later.
"""
import argparse, codecs, json, os, re, sys, tempfile, time
from pathlib import Path

NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")
SAMPLE = re.compile(r"Sample (\d+)/(\d+)|Rendering (\d+) / (\d+) samples")
FRAME = re.compile(r"Fra:(\d+)")


def pdir():
    d = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes") / "progress"
    d.mkdir(parents=True, exist_ok=True)
    return d


def atomic(path, write):
    """write(fileobj) into a temp file next to `path`, then rename over it."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            write(f)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def thumb(src, dst):
    """jpeg thumbnail of src at dst; returns the path to store (the original if Pillow or the image is unusable)."""
    try:
        from PIL import Image
        with Image.open(src) as im:
            im = im.convert("RGB")
            im.thumbnail((1280, 1280))
            atomic(dst, lambda f: im.save(f, "JPEG", quality=82))
        return str(dst)
    except Exception:
        return os.path.abspath(src)


def update(name, now=None, **fields):
    """Merge the given (not None) fields into NAME's file; keeps the rest, sets started once and at every time."""
    d = pdir()
    p = d / (name + ".json")
    try:
        cur = json.loads(p.read_text("utf-8"))
        if not isinstance(cur, dict):
            cur = {}
    except (OSError, ValueError):
        cur = {}
    cur.update({k: v for k, v in fields.items() if v is not None})
    if cur.get("image") and cur["image"] == fields.get("image"):
        cur["image"] = thumb(cur["image"], d / (name + ".thumb.jpg"))
    now = time.time() if now is None else now
    if fields.get("state") == "running":   # an explicit (re)start: the time-left estimate counts from here
        cur["started"] = now
    cur.setdefault("started", now)
    cur.setdefault("title", name)
    cur["at"] = now
    atomic(p, lambda f: f.write(json.dumps(cur).encode()))


def parse(line):
    """(sample_frac, 'Sample a/b', frame number or None) of a progress line, else None."""
    m = SAMPLE.search(line)
    if not m:
        return None
    a, b = (int(x) for x in (m.group(1) or m.group(3), m.group(2) or m.group(4)))
    f = FRAME.search(line)
    return (min(a / b, 1.0) if b else 0.0), "Sample %d/%d" % (a, b), int(f.group(1)) if f else None


def blender(name, lines, frames=None, clock=time.time, out=sys.stdout):
    """Filter: log lines pass through, progress lines become frac/detail in the file (max once a second)."""
    last, pending = None, None
    for line in lines:
        if not line.strip():
            continue
        r = parse(line)
        if not r:
            print(line, file=out, flush=True)
            continue
        sf, detail, n = r
        frac = sf
        if frames and n is not None:
            total = frames[1] - frames[0] + 1
            frac = min(max((n - frames[0] + sf) / total, 0.0), 1.0)
            detail += " \u00b7 frame %d/%d" % (n - frames[0] + 1, total)
        pending = dict(frac=round(frac, 4), detail=detail)
        now = clock()
        if last is None or now - last >= 1:
            update(name, now, **pending)
            last, pending = now, None
    if pending:
        update(name, clock(), **pending)


def read_lines(f):
    """Yield lines of a binary stream as they arrive, split on \\n and \\r (Blender redraws progress with \\r)."""
    dec, buf = codecs.getincrementaldecoder("utf-8")("replace"), ""
    while True:
        chunk = f.read1(65536)
        buf += dec.decode(chunk, final=not chunk)
        *done, buf = re.split(r"[\r\n]", buf)
        yield from done
        if not chunk:
            yield buf
            return


def check_name(p, name):
    if not NAME.fullmatch(name) or name.startswith("."):
        p.error("bad name %r: use 1-64 characters of A-Z a-z 0-9 . _ - and no leading dot" % name)
    return name


def test():
    import io
    cy = "Fra:1 Mem:120.5M (Peak 130M) | Time:00:12.34 | Remaining:00:40.12 | Mem:80M, Peak:90M | Scene, ViewLayer | Sample 64/512"
    ee = "Fra:1 Mem:50.2M (Peak 60.1M) | Time:00:01.20 | Rendering 12 / 64 samples"
    assert parse(cy) == (0.125, "Sample 64/512", 1) and parse(ee) == (12 / 64, "Sample 12/64", 1)
    assert parse("Saved: x.png") is None and parse("Fra:1 | Sample 3/0")[0] == 0
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["HERMES_HOME"] = tmp
        rd = lambda n: json.loads((Path(tmp) / "progress" / (n + ".json")).read_text())
        # merge: two writers, started kept, at updated
        update("j", 10.0, step="shot 2", done=1, total=4)
        update("j", 20.0, frac=0.5, detail="Sample 1/2")
        j = rd("j")
        assert (j["step"], j["done"], j["frac"], j["detail"], j["title"]) == ("shot 2", 1, 0.5, "Sample 1/2", "j")
        assert (j["started"], j["at"]) == (10.0, 20.0)
        (Path(tmp) / "progress" / "j.json").write_text("{broken")
        update("j", 30.0, step="x")
        assert rd("j")["step"] == "x" and rd("j")["started"] == 30.0
        update("j", 40.0, state="running")
        update("j", 50.0, done=2)
        assert rd("j")["started"] == 40.0
        # filter: throttle (clock +0.1 per progress line), pass-through, \r input, frames
        out, n, calls, real = io.StringIO(), [0], [], update
        def clock():
            n[0] += 1
            return 1000 + n[0] / 10
        globals()["update"] = lambda name, now, **f: (calls.append(f), real(name, now, **f))
        try:
            raw = b"start\r" + b"".join(b"Fra:3 | Sample %d/20\r" % i for i in range(1, 21)) + b"\nwritten x.png\n"
            blender("b", read_lines(io.BufferedReader(io.BytesIO(raw))), (2, 5), clock, out)
        finally:
            globals()["update"] = real
        assert out.getvalue() == "start\nwritten x.png\n", out.getvalue()
        assert len(calls) == 3 and calls[0]["frac"] == 0.2625, calls  # first, one after 1 s, EOF
        b = rd("b")
        assert b["frac"] == 0.5 and b["detail"] == "Sample 20/20 \u00b7 frame 2/4", b
        # names
        for bad in ("../x", ".hidden", "", "a" * 65):
            try:
                with open(os.devnull, "w") as sys.stderr:
                    check_name(argparse.ArgumentParser(), bad)
                raise AssertionError(bad)
            except SystemExit as e:
                assert e.code == 2
            finally:
                sys.stderr = sys.__stderr__
        assert check_name(None, "a" * 64) and check_name(None, "r_1.v-2")
        # thumbnail (only with Pillow)
        try:
            from PIL import Image
        except ImportError:
            print("skip thumbnail: no Pillow", file=sys.stderr)
        else:
            png = Path(tmp) / "big.png"
            Image.new("RGB", (2000, 1000), "red").save(png)
            update("t", 1.0, image=str(png))
            t = rd("t")["image"]
            assert t.endswith("t.thumb.jpg") and Image.open(t).size == (1280, 640), t
            Path(png).write_bytes(b"nope")  # unreadable image: stored as is
            update("t", 2.0, image=str(png))
            assert rd("t")["image"] == str(png)
    print("ok")


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--test", action="store_true")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("set")
    s.add_argument("name")
    num = lambda v: int(float(v)) if float(v).is_integer() else float(v)
    clamp = lambda v: min(1.0, max(0.0, float(v)))
    for k, t in dict(title=str, done=num, total=num, frac=clamp, step=str, detail=str, pid=int, image=str).items():
        s.add_argument("--" + k, type=t)
    s.add_argument("--state", choices=["running", "done", "failed"])
    b = sub.add_parser("blender")
    b.add_argument("name")
    b.add_argument("--frames", nargs=2, type=int, metavar=("START", "END"))
    sub.add_parser("clear").add_argument("name")
    a = p.parse_args()
    if a.test:
        return test()
    if not a.cmd:
        p.error("a command or --test is required")
    check_name(p, a.name)
    if a.cmd == "set":
        update(a.name, **{k: v for k, v in vars(a).items() if k not in ("test", "cmd", "name")})
    elif a.cmd == "blender":
        blender(a.name, read_lines(sys.stdin.buffer), a.frames)
    else:
        for ext in (".json", ".thumb.jpg"):
            (pdir() / (a.name + ext)).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
