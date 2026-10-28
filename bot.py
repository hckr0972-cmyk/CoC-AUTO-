#!/usr/bin/env python3
"""
CoC auto-attack + wall upgrade bot for Android (v3) - engine.
Runs inside Termux and controls the SAME phone over ADB wireless debugging.
Port of the PC version (attack.py + wall_upgrade.py).

Easiest way to use it: the web panel  ->  python panel.py   (see panel.py)

Command line (optional)
  python bot.py init                       create config.json
  python bot.py setpos [key]               tap-to-set positions
  python bot.py set KEY X Y                set one position by hand
  python bot.py click KEY                  tap a saved position
  python bot.py shot                       save screen.png
  python bot.py capture NAME x1 y1 x2 y2   crop img/NAME.png from the screen
  python bot.py test                       check templates on the current screen
  python bot.py run                        start the bot (Ctrl+C to stop)
"""
import json
import math
import os
import random
import re
import subprocess
import sys
import threading
import time
from collections import deque
from io import BytesIO

import numpy as np
from PIL import Image

CFG_FILE = "config.json"
IMG_DIR = "img"


class BotError(Exception):
    pass


class Stopped(Exception):
    pass


# (group, [keys]) - same layout as the PC setup panel
GROUPS = [
    ("Attack UI", ["attack_menu", "find_match", "confirm_attack", "surrender", "confirm_ok"]),
    ("Army Bar", ["troop", "spell", "siege", "hero_1", "hero_2", "hero_3", "hero_4"]),
    ("Deploy Edges", ["deploy_left_up", "deploy_left_left", "deploy_left_bottom",
                      "deploy_right_up", "deploy_right_right", "deploy_right_bottom"]),
    ("Spell Targets", ["spell_target_left", "spell_target_center", "spell_target_right"]),
    ("Hero & Siege Deploy", ["siege_deploy", "hero_1_deploy", "hero_2_deploy",
                             "hero_3_deploy", "hero_4_deploy"]),
    ("Wall Upgrade", ["wall_upgradable", "wall_scroll_from", "wall_scroll_to",
                      "wall_select_multi", "wall_gold", "wall_elixir", "wall_ok"]),
]

LABELS = {
    "attack_menu": "Attack menu button", "find_match": "Find a Match button",
    "confirm_attack": "Confirm / Search button", "surrender": "Surrender button",
    "confirm_ok": "Surrender OK button",
    "troop": "Troop slot", "spell": "Spell slot", "siege": "Siege machine slot",
    "hero_1": "Hero 1 slot", "hero_2": "Hero 2 slot", "hero_3": "Hero 3 slot", "hero_4": "Hero 4 slot",
    "deploy_left_up": "Left side - top corner", "deploy_left_left": "Left corner",
    "deploy_left_bottom": "Left side - bottom corner", "deploy_right_up": "Right side - top corner",
    "deploy_right_right": "Right corner", "deploy_right_bottom": "Right side - bottom corner",
    "spell_target_left": "Spell drop - left", "spell_target_center": "Spell drop - center",
    "spell_target_right": "Spell drop - right",
    "siege_deploy": "Siege drop spot", "hero_1_deploy": "Hero 1 drop spot",
    "hero_2_deploy": "Hero 2 drop spot", "hero_3_deploy": "Hero 3 drop spot",
    "hero_4_deploy": "Hero 4 drop spot",
    "wall_upgradable": "All Upgradable button", "wall_scroll_from": "Upgrade list - swipe START (lower)",
    "wall_scroll_to": "Upgrade list - swipe END (upper)", "wall_select_multi": "Select Multiple Walls",
    "wall_gold": "Upgrade with Gold", "wall_elixir": "Upgrade with Elixir", "wall_ok": "Wall upgrade OK",
}

POSITIONS = [(k, LABELS[k]) for _, keys in GROUPS for k in keys]

# template -> (label, match confidence)
TEMPLATES = {
    "fifty_percent": ("50% Destruction", 0.93),
    "return_home": ("Return Home", 0.85),
    "wall_text": ("Wall Text", 0.90),
}

DEFAULT = {
    "adb_serial": "localhost:5555",
    "settings": {
        "attacks": 10,
        "troop_count": 40,
        "spell_count": 11,
        "wall_enabled": False,
        "wall_every": 5,
        "fifty_timeout": 90,
        "max_scrolls": 10,
    },
    "positions": {},
}

cfg = {}
STOP = threading.Event()
PAUSE = threading.Event()
LOGS = deque(maxlen=400)
STATS = {"done": 0, "total": 0, "ok": 0, "running": False}


# --------------------------------------------------------------------------
#  Config / logging / control
# --------------------------------------------------------------------------

def load():
    global cfg
    if not os.path.isfile(CFG_FILE):
        raise BotError("config.json not found - run: python bot.py init")
    with open(CFG_FILE) as f:
        cfg = json.load(f)
    cfg.setdefault("adb_serial", DEFAULT["adb_serial"])
    cfg.setdefault("positions", {})
    for k, v in DEFAULT["settings"].items():
        cfg.setdefault("settings", {}).setdefault(k, v)


def save():
    with open(CFG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    LOGS.append(line)
    print(line, flush=True)


def checkpoint():
    """Stop / pause point (like the F6 pause on the PC version)."""
    if STOP.is_set():
        raise Stopped()
    while PAUSE.is_set():
        if STOP.is_set():
            raise Stopped()
        time.sleep(0.2)


def sleep(a, b=None):
    end = time.time() + (random.uniform(a, b) if b else a)
    while True:
        checkpoint()
        left = end - time.time()
        if left <= 0:
            return
        time.sleep(min(0.2, left))


def adb(*args):
    try:
        r = subprocess.run(["adb", "-s", cfg["adb_serial"], *args], capture_output=True)
    except FileNotFoundError:
        raise BotError("adb not found - run: pkg install android-tools")
    return r.stdout


def pos(key):
    v = cfg["positions"].get(key)
    if v and len(v) == 2 and (v[0] or v[1]):
        return int(v[0]), int(v[1])
    return None


# --------------------------------------------------------------------------
#  Input
# --------------------------------------------------------------------------

def tap(p, jitter=3):
    checkpoint()
    x, y = p
    x += random.randint(-jitter, jitter) if jitter else 0
    y += random.randint(-jitter, jitter) if jitter else 0
    adb("shell", "input", "tap", str(x), str(y))
    sleep(0.12, 0.25)


def tap_many(points, jitter=2):
    """Several taps in ONE adb call (much faster for troop/spell drops)."""
    checkpoint()
    cmds = [f"input tap {x + random.randint(-jitter, jitter)} {y + random.randint(-jitter, jitter)}"
            for x, y in points]
    adb("shell", " && ".join(cmds))


def swipe(a, b, ms=700):
    checkpoint()
    adb("shell", "input", "swipe", str(a[0]), str(a[1]), str(b[0]), str(b[1]), str(ms))


# --------------------------------------------------------------------------
#  Screen + template matching
# --------------------------------------------------------------------------

def shot():
    """Current screen as a PIL RGB image."""
    data = adb("exec-out", "screencap", "-p")
    try:
        return Image.open(BytesIO(data)).convert("RGB")
    except Exception:
        raise BotError("Cannot read the screen. Is adb connected? (adb connect localhost:PORT)")


def has_template(name):
    return os.path.isfile(f"{IMG_DIR}/{name}.png")


DOWNSCALE = 2  # match on a half-size picture: ~4x faster, accurate enough for taps


def _prep(img):
    g = np.asarray(img.convert("L"), dtype=np.float32)
    f = DOWNSCALE
    if f > 1:
        h, w = (g.shape[0] // f) * f, (g.shape[1] // f) * f
        g = g[:h, :w].reshape(h // f, f, w // f, f).mean(axis=(1, 3))
    return g


def ncc(scr, tpl):
    """Normalized cross-correlation (same score as OpenCV TM_CCOEFF_NORMED).
    Returns (best_score, x, y) of the best top-left position."""
    H, W = scr.shape
    h, w = tpl.shape
    if h > H or w > W or h < 2 or w < 2:
        return 0.0, 0, 0
    t0 = tpl - tpl.mean()
    tden = float(np.sqrt((t0 * t0).sum()))
    if tden < 1e-6:  # flat template, nothing to match
        return 0.0, 0, 0
    n = h * w
    F = np.fft.rfft2(scr)
    G = np.fft.rfft2(t0, s=scr.shape)
    num = np.fft.irfft2(F * np.conj(G), s=scr.shape)[:H - h + 1, :W - w + 1]

    def box(a):  # sum of every h x w window (integral image)
        c = np.cumsum(np.cumsum(a, axis=0, dtype=np.float64), axis=1, dtype=np.float64)
        c = np.pad(c, ((1, 0), (1, 0)))
        return c[h:, w:] - c[:-h, w:] - c[h:, :-w] + c[:-h, :-w]

    s1, s2 = box(scr), box(scr * scr)
    den = np.sqrt(np.maximum(s2 - s1 * s1 / n, 0)) * tden
    r = np.where(den > 1e-3, num / np.maximum(den, 1e-9), 0.0)
    iy, ix = np.unravel_index(np.argmax(r), r.shape)
    return float(r[iy, ix]), int(ix), int(iy)


def best_match(name, scr=None):
    """Return (center or None, confidence, threshold)."""
    thr = TEMPLATES[name][1]
    path = f"{IMG_DIR}/{name}.png"
    if not os.path.isfile(path):
        raise BotError(f"template '{name}' not captured yet")
    if scr is None:
        scr = shot()
    tpl = _prep(Image.open(path))
    score, x, y = ncc(_prep(scr), tpl)
    f = DOWNSCALE
    center = ((x + tpl.shape[1] // 2) * f, (y + tpl.shape[0] // 2) * f) if score >= thr else None
    return center, score, thr


def find(name):
    checkpoint()
    return best_match(name)[0]


def wait_for(name, timeout, interval=1.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        p = find(name)
        if p:
            return p
        sleep(interval)
    return None


# --------------------------------------------------------------------------
#  Attack
# --------------------------------------------------------------------------

def interpolate(p1, p2, n):
    if n <= 1:
        return [((p1[0] + p2[0]) // 2, (p1[1] + p2[1]) // 2)]
    return [(int(p1[0] + i / (n - 1) * (p2[0] - p1[0])),
             int(p1[1] + i / (n - 1) * (p2[1] - p1[1]))) for i in range(n)]


def attack():
    s = cfg["settings"]
    for k in ("attack_menu", "find_match", "confirm_attack"):
        if not pos(k):
            log(f"X position missing: {k}")
            return False

    log("Clicking Attack menu...")
    tap(pos("attack_menu")); sleep(1.0)
    log("Clicking Find Match...")
    tap(pos("find_match")); sleep(1.0)
    log("Confirming attack...")
    tap(pos("confirm_attack")); sleep(3.0, 4.0)

    # spells - 3 drop spots, the one closest to the siege gets the extras
    siege_deploy = pos("siege_deploy")
    targets = [pos(k) for k in ("spell_target_left", "spell_target_center", "spell_target_right")]
    targets = [t for t in targets if t]
    n_sp = s["spell_count"]
    if pos("spell") and targets and n_sp > 0:
        if siege_deploy:
            targets.sort(key=lambda t: math.hypot(t[0] - siege_deploy[0], t[1] - siege_deploy[1]))
        base, rem = divmod(n_sp, len(targets))
        plan = [(t, base + (1 if i < rem else 0)) for i, t in enumerate(targets)]
        log(f"Deploying {n_sp} spells: {'+'.join(str(c) for _, c in plan)}")
        tap(pos("spell")); sleep(0.3)
        for t, c in plan:
            if c:
                tap_many([t] * c)
                sleep(0.15)
        sleep(0.3)

    # siege
    if pos("siege") and siege_deploy:
        log("Deploying siege machine...")
        tap(pos("siege")); sleep(0.3)
        tap(siege_deploy); sleep(0.3)

    # troops - spread along 4 edges (each edge = 2 corner points)
    pairs = []
    for k1, k2 in (("deploy_left_up", "deploy_left_left"),
                   ("deploy_left_left", "deploy_left_bottom"),
                   ("deploy_right_up", "deploy_right_right"),
                   ("deploy_right_right", "deploy_right_bottom")):
        if pos(k1) and pos(k2):
            pairs.append((pos(k1), pos(k2)))
    n_tr = s["troop_count"]
    if pos("troop") and pairs:
        log(f"Deploying {n_tr} troops across {len(pairs)} sides...")
        tap(pos("troop")); sleep(0.3)
        base, rem = divmod(n_tr, len(pairs))
        for i, (p1, p2) in enumerate(pairs):
            c = base + (1 if i < rem else 0)
            if c > 0:
                tap_many(interpolate(p1, p2, c))
        sleep(0.3)
        log("Troops deployed")

    # heroes
    heroes = []
    for n in range(1, 5):
        sl, dp = pos(f"hero_{n}"), pos(f"hero_{n}_deploy")
        if sl and dp:
            heroes.append((sl, dp))
    if heroes:
        log(f"Deploying {len(heroes)} heroes...")
        for sl, dp in heroes:
            tap(sl); sleep(0.3)
            tap(dp); sleep(0.3)
        sleep(1.5)
        log("Activating hero abilities...")
        for sl, _ in heroes:
            tap(sl); sleep(0.3)

    # wait for 50%
    got_fifty = False
    if has_template("fifty_percent"):
        log(f"Waiting for 50% destruction ({s['fifty_timeout']} s timeout)...")
        t0 = time.time()
        while time.time() - t0 < s["fifty_timeout"]:
            if find("fifty_percent"):
                got_fifty = True
                w = random.uniform(5.0, 10.0)
                log(f"OK 50% detected after {time.time() - t0:.0f}s - waiting {w:.1f}s...")
                sleep(w)
                break
            sleep(1.0)
        if not got_fifty:
            log(f"50% not reached after {time.time() - t0:.0f}s - surrendering")
    else:
        log("No 50% template - waiting 30 s then surrendering")
        sleep(30)

    # surrender -> OK -> return home
    if pos("surrender"):
        log("Surrendering...")
        tap(pos("surrender")); sleep(1.0)
    if pos("confirm_ok"):
        log("Clicking OK...")
        tap(pos("confirm_ok")); sleep(2.0)
    if has_template("return_home"):
        log("Waiting for Return Home button...")
        p = wait_for("return_home", 30)
        if p:
            tap(p)
            log("Returning home...")
            sleep(3.0)
        else:
            log("Return Home not found - continuing")
    else:
        log("No return_home template - waiting 5 s")
        sleep(5)
    return got_fifty


# --------------------------------------------------------------------------
#  Wall upgrade
# --------------------------------------------------------------------------

WALL_REQUIRED = ("wall_upgradable", "wall_scroll_from", "wall_scroll_to",
                 "wall_gold", "wall_elixir", "wall_ok")


def wall_pass(resource_key, name):
    log(f"Opening upgrade list ({name})...")
    tap(pos("wall_upgradable")); sleep(1.0)
    if not has_template("wall_text"):
        log("Wall template not found - capture 'Wall Text' first")
        return False
    log("Searching for 'Wall' in upgrade list...")
    wall = None
    for _ in range(cfg["settings"]["max_scrolls"] + 1):
        wall = find("wall_text")
        if wall:
            break
        swipe(pos("wall_scroll_from"), pos("wall_scroll_to"))
        sleep(0.8)
    if not wall:
        log("Wall not found in upgrade list.")
        return False
    log(f"Wall found at ({wall[0]}, {wall[1]})")
    tap(wall); sleep(0.8)
    if pos("wall_select_multi"):
        log("Clicking Select Multiple (x3)...")
        for _ in range(3):
            tap(pos("wall_select_multi")); sleep(0.3)
    log(f"Upgrading with {name}...")
    tap(pos(resource_key)); sleep(0.5)
    log("Confirming upgrade...")
    tap(pos("wall_ok")); sleep(0.8)
    log(f"Wall upgrade with {name} complete.")
    return True


def wall_cycle():
    missing = [k for k in WALL_REQUIRED if not pos(k)]
    if missing:
        log(f"Missing positions: {', '.join(missing)}")
        return False
    log("=== Pass 1: Upgrade with Gold ===")
    a = wall_pass("wall_gold", "Gold")
    sleep(1.0)
    log("=== Pass 2: Upgrade with Elixir ===")
    b = wall_pass("wall_elixir", "Elixir")
    log("Full wall upgrade cycle finished." if (a or b) else "No walls were upgraded this cycle.")
    return a or b


# --------------------------------------------------------------------------
#  Run
# --------------------------------------------------------------------------

def missing_required():
    return [k for k in ("attack_menu", "find_match", "confirm_attack") if not pos(k)]


def run_attacks():
    """Run the configured number of attacks. STOP/PAUSE are honoured everywhere."""
    s = cfg["settings"]
    total = s["attacks"]
    STATS.update(done=0, total=total, ok=0, running=True)
    try:
        for i in range(1, total + 1):
            checkpoint()
            log(f"=== Attack {i}/{total} ===")
            got = attack()
            STATS["done"] = i
            STATS["ok"] += 1 if got else 0
            log(f"Attacks: {i}/{total} | 50%: {STATS['ok']} | Left: {total - i}")
            if s["wall_enabled"] and i % s["wall_every"] == 0:
                log("Running wall upgrade cycle...")
                try:
                    wall_cycle()
                except BotError as e:
                    log(f"Wall upgrade error: {e}")
            sleep(2.0)
        log("All attacks completed.")
    except Stopped:
        log("Bot stopped.")
    except BotError as e:
        log(f"Bot error: {e}")
    finally:
        STATS["running"] = False


def start_flags():
    STOP.clear()
    PAUSE.clear()
    STATS.update(done=0, total=cfg["settings"]["attacks"], ok=0, running=True)


def run_cli():
    m = missing_required()
    if m:
        raise BotError(f"Set these positions first: {', '.join(m)}")
    start_flags()
    log("Starting in 5s - switch to the game (Ctrl+C to stop)")
    try:
        sleep(5)
        run_attacks()
    except (KeyboardInterrupt, Stopped):
        STOP.set()
        STATS["running"] = False
        log("Stopped.")


# --------------------------------------------------------------------------
#  Tap-to-set positions (reads real touches with getevent)
# --------------------------------------------------------------------------

def touch_info():
    t = adb("shell", "getevent", "-lp").decode(errors="ignore")
    mx = re.search(r"ABS_MT_POSITION_X\s*:.*?max (\d+)", t)
    my = re.search(r"ABS_MT_POSITION_Y\s*:.*?max (\d+)", t)
    if not (mx and my):
        raise BotError("Could not read the touchscreen. Enter the position by hand instead.")
    d = adb("shell", "dumpsys", "input").decode(errors="ignore")
    m = re.search(r"SurfaceOrientation:\s*(\d)", d)
    rot = int(m.group(1)) if m else 1
    w, h = shot().size
    return int(mx.group(1)), int(my.group(1)), rot, min(w, h), max(w, h)


def convert(rx, ry, info):
    mx, my, rot, wp, hp = info
    px, py = rx * wp / mx, ry * hp / my
    if rot == 0:
        x, y = px, py
    elif rot == 1:
        x, y = py, wp - px
    elif rot == 2:
        x, y = wp - px, hp - py
    else:
        x, y = hp - py, px
    return int(round(x)), int(round(y))


def get_tap(info):
    proc = subprocess.Popen(["adb", "-s", cfg["adb_serial"], "shell", "getevent", "-l"],
                            stdout=subprocess.PIPE, text=True)
    x = y = None
    try:
        for line in proc.stdout:
            if "ABS_MT_POSITION_X" in line:
                x = int(line.split()[-1], 16)
            elif "ABS_MT_POSITION_Y" in line:
                y = int(line.split()[-1], 16)
            if x is not None and y is not None:
                break
    finally:
        proc.kill()
    if x is None or y is None:
        raise BotError("getevent stopped - is adb still connected?")
    time.sleep(0.8)
    return convert(x, y, info)


def setpos_cli(only=None):
    if only and only not in LABELS:
        raise BotError(f"unknown key: {only}")
    keys = [only] if only else [k for k, _ in POSITIONS if not pos(k)]
    if not keys:
        print("All positions are set. Use: python bot.py setpos KEY  to redo one.")
        return
    info = touch_info()
    print("Open the game. For each prompt, tap the button in the game (Ctrl+C to stop).\n")
    try:
        for k in keys:
            print(f"-> {LABELS[k]}  [{k}]")
            x, y = get_tap(info)
            cfg["positions"][k] = [x, y]
            save()
            print(f"   saved ({x}, {y})")
    except KeyboardInterrupt:
        print("\nstopped - progress saved")


# --------------------------------------------------------------------------
#  CLI
# --------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd, args = sys.argv[1], sys.argv[2:]
    os.makedirs(IMG_DIR, exist_ok=True)
    if cmd == "init":
        with open(CFG_FILE, "w") as f:
            json.dump(DEFAULT, f, indent=2)
        print("config.json created. Set adb_serial, then run: python panel.py")
        return
    load()
    if cmd == "setpos":
        setpos_cli(args[0] if args else None)
    elif cmd == "set" and len(args) == 3:
        cfg["positions"][args[0]] = [int(args[1]), int(args[2])]
        save()
        print("saved")
    elif cmd == "click" and len(args) == 1:
        p = pos(args[0])
        print("not set" if not p else f"tapping {p}")
        if p:
            tap(p, jitter=0)
    elif cmd == "shot":
        shot().save("screen.png")
        print("saved screen.png")
    elif cmd == "capture" and len(args) == 5 and args[0] in TEMPLATES:
        x1, y1, x2, y2 = map(int, args[1:5])
        shot().crop((x1, y1, x2, y2)).save(f"{IMG_DIR}/{args[0]}.png")
        print(f"saved {IMG_DIR}/{args[0]}.png")
    elif cmd == "test":
        for n in TEMPLATES:
            if not has_template(n):
                print(f"{n}: template not captured")
                continue
            p, conf, thr = best_match(n)
            print(f"{n}: {'FOUND at ' + str(p) if p else 'not found'} "
                  f"(best {conf * 100:.1f}%, needs {thr * 100:.0f}%)")
    elif cmd == "run":
        run_cli()
    else:
        print(__doc__)


if __name__ == "__main__":
    try:
        main()
    except BotError as e:
        sys.exit(str(e))
