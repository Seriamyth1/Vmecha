"""Survival-first Mech Royale bot.

Placement is all that scores, so the bot plays for the last circle:
  * builds a walk grid from the map and paths around buildings (A*),
  * picks a covered hide spot inside the next zone circle and walks there quietly,
  * avoids NPC drones, supply-drop scrums and far-away fights,
  * fights back hard (faces attackers, strafes, leads shots) when engaged,
  * grabs weapons and cubes when it is safe, takes defensive upgrades.
"""
import gc
import heapq
import json
import math
import os
import random
import sys
import time
from array import array

import mechfight as mf

TAU = 2.0 * math.pi
SQ2 = math.sqrt(2.0)


def wrap(a):
    return (a + math.pi) % TAU - math.pi


def hdist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


# --------------------------------------------------------------------------- map
class Nav:
    G = 2.0  # grid cell size (m)

    @staticmethod
    def fingerprint(mapdir):
        import hashlib
        h = hashlib.sha1()
        for f in ("world.json", "terrain.i16"):
            with open(os.path.join(mapdir, "v2", f), "rb") as fh:
                h.update(fh.read())
        return h.hexdigest()

    def save(self, path, fp):
        meta = {"v": 2, "fp": fp, "nx": self.nx, "ny": self.ny, "minx": self.minx, "miny": self.miny,
                "tminx": self.tminx, "tminy": self.tminy, "tstep": self.tstep, "tnx": self.tnx, "tny": self.tny}
        with open(path, "wb") as f:
            mb = json.dumps(meta).encode()
            f.write(len(mb).to_bytes(4, "little") + mb)
            for a in (array("f", self.gh), array("f", self.top), array("f", self.base)):
                f.write(a.tobytes())
            f.write(bytes(self.raw))
            f.write(bytes(self.blk))
            f.write(bytes(self.canopy))
            f.write(self.th.tobytes())

    @classmethod
    def load(cls, path, fp=None):
        self = cls.__new__(cls)
        with open(path, "rb") as f:
            data = f.read()
        n0 = int.from_bytes(data[:4], "little")
        meta = json.loads(data[4:4 + n0])
        if fp is not None and meta["fp"] != fp:
            raise ValueError("map changed")
        if meta.get("v") != 2:
            raise ValueError("old cache format")
        for k in ("nx", "ny", "minx", "miny", "tminx", "tminy", "tstep", "tnx", "tny"):
            setattr(self, k, meta[k])
        n = self.nx * self.ny
        o = 4 + n0
        arrs = []
        for _ in range(3):
            a = array("f")
            a.frombytes(data[o:o + 4 * n])
            arrs.append(a)
            o += 4 * n
        self.gh, self.top, self.base = arrs
        self.raw = bytearray(data[o:o + n]); o += n
        self.blk = bytearray(data[o:o + n]); o += n
        self.canopy = bytearray(data[o:o + n]); o += n
        self.th = array("h")
        self.th.frombytes(data[o:o + 2 * self.tnx * self.tny])
        self.cover_cache = {}
        return self

    def __init__(self, mapdir, cfg_mech):
        w = json.load(open(os.path.join(mapdir, "v2", "world.json")))
        t = w["terrain"]
        self.tminx, self.tminy, self.tstep = t["minX"], t["minY"], t["step"]
        self.tnx, self.tny = t["nx"], t["ny"]
        h = array("h")
        with open(os.path.join(mapdir, "v2", t.get("file", "terrain.i16")), "rb") as f:
            h.frombytes(f.read())
        if sys.byteorder != "little":
            h.byteswap()
        self.th = h
        b = w["bounds"]
        G = self.G
        self.minx, self.miny = b["minX"], b["minY"]
        self.nx = int((b["maxX"] - b["minX"]) / G) + 1
        self.ny = int((b["maxY"] - b["minY"]) / G) + 1
        nx, ny = self.nx, self.ny
        n = nx * ny
        gh = [0.0] * n
        for j in range(ny):
            y = self.miny + j * G
            for i in range(nx):
                gh[j * nx + i] = self.ground(self.minx + i * G, y)
        self.gh = gh
        step_up = cfg_mech.get("step_up", 1.2)
        height = cfg_mech.get("height", 9.0)
        raw = bytearray(n)
        self.top = [-1e9] * n   # highest solid top in cell (for line of sight)
        self.base = [1e9] * n   # lowest solid base in cell
        for p in w["prisms"]:
            cells = self._raster(p["ring"])
            for hole in p.get("holes") or []:
                cells -= self._raster(hole)
            # thin features: make sure ring vertices count too
            r = p["ring"]
            for k in range(0, len(r), 2):
                c = self.cell(r[k], r[k + 1])
                if c is not None:
                    cells.add(c)
            pb, pt = p["base"], p["top"]
            for c in cells:
                g = gh[c]
                if pt > self.top[c]:
                    self.top[c] = pt
                if pb < self.base[c]:
                    self.base[c] = pb
                if pt > g + step_up and pb < g + height + 0.5:
                    raw[c] = 1
        # map edge
        for i in range(nx):
            for j in (0, 1, ny - 2, ny - 1):
                raw[j * nx + i] = 1
        for j in range(ny):
            for i in (0, 1, nx - 2, nx - 1):
                raw[j * nx + i] = 1
        self.raw = raw
        # tree canopy density per cell (blocks sight, not shots)
        canopy = bytearray(n)
        tr = w.get("trees") or {}
        flds = tr.get("fields") or []
        data = tr.get("data") or []
        if flds and data:
            k = len(flds)
            ix, iy = flds.index("x"), flds.index("y")
            icr = flds.index("crownR") if "crownR" in flds else None
            icx = flds.index("crownX") if "crownX" in flds else None
            icy = flds.index("crownY") if "crownY" in flds else None
            for q in range(0, len(data) - k + 1, k):
                cx = data[q + icx] if icx is not None and data[q + icx] is not None else data[q + ix]
                cy = data[q + icy] if icy is not None and data[q + icy] is not None else data[q + iy]
                r = (data[q + icr] if icr is not None and data[q + icr] else 3.0)
                rc = int(r / G) + 1
                c0 = self.cell(cx, cy)
                if c0 is None:
                    continue
                i0, j0 = c0 % nx, c0 // nx
                for dj in range(-rc, rc + 1):
                    for di in range(-rc, rc + 1):
                        ii, jj = i0 + di, j0 + dj
                        if 0 <= ii < nx and 0 <= jj < ny and (di * di + dj * dj) * G * G <= r * r:
                            cc = jj * nx + ii
                            if canopy[cc] < 250:
                                canopy[cc] += 1
        self.canopy = canopy
        blk = bytearray(raw)
        for c in range(n):
            if raw[c]:
                i, j = c % nx, c // nx
                for dj in (-1, 0, 1):
                    jj = j + dj
                    if 0 <= jj < ny:
                        for di in (-1, 0, 1):
                            ii = i + di
                            if 0 <= ii < nx:
                                blk[jj * nx + ii] = 1
        self.blk = blk
        self.cover_cache = {}

    def ground(self, x, y):
        fi = (x - self.tminx) / self.tstep
        fj = (y - self.tminy) / self.tstep
        i0 = max(0, min(self.tnx - 2, int(math.floor(fi))))
        j0 = max(0, min(self.tny - 2, int(math.floor(fj))))
        fx = min(1.0, max(0.0, fi - i0))
        fy = min(1.0, max(0.0, fj - j0))
        th, nx = self.th, self.tnx
        a = th[j0 * nx + i0]
        b = th[j0 * nx + i0 + 1]
        c = th[(j0 + 1) * nx + i0]
        d = th[(j0 + 1) * nx + i0 + 1]
        return ((a * (1 - fx) + b * fx) * (1 - fy) + (c * (1 - fx) + d * fx) * fy) * 0.01

    def _raster(self, ring):
        G, nx, ny = self.G, self.nx, self.ny
        xs = ring[0::2]
        ys = ring[1::2]
        m = len(xs)
        out = set()
        if m < 3:
            return out
        j0 = max(0, int(math.ceil((min(ys) - self.miny) / G)))
        j1 = min(ny - 1, int(math.floor((max(ys) - self.miny) / G)))
        for j in range(j0, j1 + 1):
            yc = self.miny + j * G
            xc = []
            for k in range(m):
                x1, y1 = xs[k], ys[k]
                x2, y2 = xs[(k + 1) % m], ys[(k + 1) % m]
                if (y1 <= yc < y2) or (y2 <= yc < y1):
                    xc.append(x1 + (yc - y1) * (x2 - x1) / (y2 - y1))
            xc.sort()
            for k in range(0, len(xc) - 1, 2):
                i0 = max(0, int(math.ceil((xc[k] - self.minx) / G)))
                i1 = min(nx - 1, int(math.floor((xc[k + 1] - self.minx) / G)))
                for i in range(i0, i1 + 1):
                    out.add(j * nx + i)
        return out

    def cell(self, x, y):
        i = int(round((x - self.minx) / self.G))
        j = int(round((y - self.miny) / self.G))
        if 0 <= i < self.nx and 0 <= j < self.ny:
            return j * self.nx + i
        return None

    def xy(self, c):
        return (self.minx + (c % self.nx) * self.G, self.miny + (c // self.nx) * self.G)

    def free(self, c):
        return c is not None and not self.blk[c]

    def nearest_free(self, c, maxr=12):
        if c is None:
            return None
        if not self.blk[c]:
            return c
        nx, ny = self.nx, self.ny
        i, j = c % nx, c // nx
        for r in range(1, maxr + 1):
            best = None
            bd = 1e9
            for dj in range(-r, r + 1):
                for di in range(-r, r + 1):
                    if max(abs(di), abs(dj)) != r:
                        continue
                    ii, jj = i + di, j + dj
                    if 0 <= ii < nx and 0 <= jj < ny:
                        cc = jj * nx + ii
                        if not self.blk[cc]:
                            d = di * di + dj * dj
                            if d < bd:
                                bd, best = d, cc
            if best is not None:
                return best
        return None

    def cover(self, c):
        v = self.cover_cache.get(c)
        if v is not None:
            return v
        nx, ny = self.nx, self.ny
        i, j = c % nx, c // nx
        s = 0
        for dj in range(-3, 4):
            jj = j + dj
            if 0 <= jj < ny:
                row = jj * nx
                for di in range(-3, 4):
                    ii = i + di
                    if 0 <= ii < nx:
                        if self.raw[row + ii]:
                            s += 1
                        elif self.canopy[row + ii] >= 2:
                            s += 0.5
        self.cover_cache[c] = s
        return s

    def walk_line(self, a, b):
        """Straight walk from a to b stays on free cells."""
        d = hdist(a, b)
        n = int(d / 1.0) + 1
        for k in range(1, n + 1):
            f = k / n
            c = self.cell(a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)
            if c is None or self.blk[c]:
                return False
        return True

    def los(self, a, b):
        """3D line of sight a->b (points [x,y,z]) against prisms and terrain."""
        d = math.dist(a, b)
        n = int(d / 1.5) + 1
        top, base, gh = self.top, self.base, self.gh
        for k in range(1, n):
            f = k / n
            x = a[0] + (b[0] - a[0]) * f
            y = a[1] + (b[1] - a[1]) * f
            z = a[2] + (b[2] - a[2]) * f
            c = self.cell(x, y)
            if c is None:
                return False
            if base[c] < z < top[c]:
                return False
            if z < gh[c] - 0.5:
                return False
        return True

    def descend(self, field, c, steps=14):
        """Greedy walk down a flow field from cell c; returns list of cells."""
        nx, ny, blk = self.nx, self.ny, self.blk
        out = [c]
        for _ in range(steps):
            dc = field[c]
            if dc <= 0:
                break
            ci, cj = c % nx, c // nx
            best, bd = None, dc
            for dj in (-1, 0, 1):
                jj = cj + dj
                if not 0 <= jj < ny:
                    continue
                for di in (-1, 0, 1):
                    ii = ci + di
                    if (di or dj) and 0 <= ii < nx:
                        nc = jj * nx + ii
                        v = field[nc]
                        if 0 <= v < bd:
                            if di and dj and (blk[cj * nx + ii] or blk[jj * nx + ci]):
                                continue
                            bd, best = v, nc
            if best is None:
                break
            c = best
            out.append(c)
        return out

    def astar(self, s, g, budget=0.012, maxexp=25000):
        if s is None or g is None:
            return None
        if s == g:
            return [g]
        nx, ny, blk, gh = self.nx, self.ny, self.blk, self.gh
        gx, gy = g % nx, g // nx
        t0 = time.perf_counter()

        def hfun(c):
            dx = abs(c % nx - gx)
            dy = abs(c // nx - gy)
            return (dx + dy) + (SQ2 - 2) * min(dx, dy)

        openh = [(hfun(s), 0.0, s)]
        gs = {s: 0.0}
        par = {s: -1}
        best, bh = s, hfun(s)
        exp = 0
        dirs = ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
                (1, 1, SQ2), (1, -1, SQ2), (-1, 1, SQ2), (-1, -1, SQ2))
        while openh:
            f, gc, c = heapq.heappop(openh)
            if gc > gs.get(c, 1e18):
                continue
            if c == g:
                best = g
                break
            exp += 1
            if exp > maxexp or (exp & 255) == 0 and time.perf_counter() - t0 > budget:
                break
            ci, cj = c % nx, c // nx
            hc = gh[c]
            for di, dj, cost in dirs:
                ii, jj = ci + di, cj + dj
                if not (0 <= ii < nx and 0 <= jj < ny):
                    continue
                nc = jj * nx + ii
                if blk[nc]:
                    continue
                if di and dj and (blk[cj * nx + ii] or blk[jj * nx + ci]):
                    continue
                if gh[nc] - hc > 2.0 * cost:
                    continue
                ng = gc + cost
                if ng < gs.get(nc, 1e18):
                    gs[nc] = ng
                    par[nc] = c
                    hh = hfun(nc)
                    if hh < bh:
                        bh, best = hh, nc
                    heapq.heappush(openh, (ng + hh, ng, nc))
        path = []
        c = best
        while c != -1:
            path.append(c)
            c = par[c]
        path.reverse()
        return path


class Field:
    """Breadth-first distance field to one goal cell, built a slice at a time."""

    def __init__(self, nav, goal_cell):
        from collections import deque
        self.nav = nav
        self.goal = goal_cell
        self.dist = array("i", [-1]) * (nav.nx * nav.ny)
        self.dist[goal_cell] = 0
        self.q = deque([goal_cell])
        self.done = False

    def step(self, budget):
        nav = self.nav
        nx, ny, blk, gh, dist, q = nav.nx, nav.ny, nav.blk, nav.gh, self.dist, self.q
        t0 = time.perf_counter()
        k = 0
        while q:
            c = q.popleft()
            dc = dist[c] + 1
            ci, cj = c % nx, c // nx
            hc = gh[c]
            for dj in (-1, 0, 1):
                jj = cj + dj
                if not 0 <= jj < ny:
                    continue
                row = jj * nx
                for di in (-1, 0, 1):
                    ii = ci + di
                    if not 0 <= ii < nx:
                        continue
                    nc = row + ii
                    if dist[nc] >= 0 or blk[nc]:
                        continue
                    if di and dj and (blk[cj * nx + ii] or blk[row + ci]):
                        continue
                    # walking nc -> c must not climb a cliff
                    if hc - gh[nc] > 2.6:
                        continue
                    dist[nc] = dc
                    q.append(nc)
            k += 1
            if (k & 511) == 0 and time.perf_counter() - t0 > budget:
                return
        self.done = True


# --------------------------------------------------------------------------- aiming helpers
def lead_point(M, T, V, s):
    D = (T[0] - M[0], T[1] - M[1], T[2] - M[2])
    a = V[0] * V[0] + V[1] * V[1] + V[2] * V[2] - s * s
    b = 2 * (D[0] * V[0] + D[1] * V[1] + D[2] * V[2])
    c = D[0] * D[0] + D[1] * D[1] + D[2] * D[2]
    t = None
    if abs(a) < 1e-6:
        if abs(b) > 1e-6:
            t = -c / b
    else:
        disc = b * b - 4 * a * c
        if disc >= 0:
            r = math.sqrt(disc)
            cands = [x for x in ((-b - r) / (2 * a), (-b + r) / (2 * a)) if x > 0]
            if cands:
                t = min(cands)
    if t is None or t <= 0:
        t = math.sqrt(c) / s
    t = min(t, 2.0)
    return (T[0] + V[0] * t, T[1] + V[1] * t, T[2] + V[2] * t), t


def aim_to(M, P):
    dx, dy, dz = P[0] - M[0], P[1] - M[1], P[2] - M[2]
    return [math.atan2(dy, dx), math.atan2(dz, math.hypot(dx, dy))]


def mortar_pitch(d, h, v, g, high):
    disc = v ** 4 - g * (g * d * d + 2 * h * v * v)
    if disc < 0 or d < 1:
        return None
    r = math.sqrt(disc)
    return math.atan((v * v + r) / (g * d)) if high else math.atan((v * v - r) / (g * d))


# Tunable tactics (override with env MECH_PARAMS='{"adv_ambush": 1.5}')
TP = {
    "facing_rad": 0.6,     # an enemy aims within this of us = it is facing us
    "adv_attacked": 0.85,  # fight back when attacked if our duel advantage is at least this
    "adv_close": 0.6,      # someone within close_d: fight unless badly outmatched
    "close_d": 45.0,
    "vulture_hp": 0.4,     # finish enemies below this HP fraction...
    "vulture_d": 140.0,    # ...within this range, when nobody else is around
    "adv_vulture": 1.0,
    "ambush_d": 110.0,     # shoot first at an unaware enemy within this range
    "adv_ambush": 1.4,
    "npc_fight_hp": 0.5,   # only fight drones that attack us, and only above this HP
    "retreat_hp": 0.35,
    "crowd_r": 150.0,      # headcount radius for "outnumbered"
    "crowd_pen": 0.6,
    "chase_s": 6.0,
    "reinf_prior": 12.0,   # seconds until teammates arrive after we open fire (learned per match)
    "hitrun_frac": 0.8,    # leave a fight at this fraction of the learned reinforcement time
    "ambush_others": 1,
    "adv_laser": 1.2,
    "loot_r_early": 160.0,  # go this far for a special weapon in the first two minutes
    "loot_r_late": 70.0,        # stop chasing a healthy enemy that keeps backing off after this long      # advantage divided by (1 + crowd_pen * other enemies nearby)
}
try:
    TP.update(json.loads(os.environ.get("MECH_PARAMS", "{}")))
except Exception:
    pass

UPGRADE_PREF = ["armor", "plating", "nanites", "gun_damage", "gun_rate", "laser_damage",
                "laser_charge", "lightning_range", "servos", "gun_mag", "sensors",
                "lightning_chain", "lightning_efficiency", "mortar_splash", "mortar_reload",
                "gun_velocity", "mortar_velocity", "reactor", "capacitor", "jets", "gyros",
                "laser_pierce"]
WEAPON_RANK = {"laser": 4, "lightning": 3, "mortar": 2, "gun": 0}


# --------------------------------------------------------------------------- contact tracker
class Tracker:
    """Best guess of where every enemy is: seen ones extrapolated, heard ones placed on their
    bearing at a typical distance for the band. Cheap: a few dozen entries at most."""
    BAND = {"near": 35.0, "mid": 105.0, "far": 230.0}
    LOUD = ("gun", "laser", "laser_charge", "lightning", "mortar", "step", "boost", "jet", "reload", "explosion")

    def __init__(self):
        self.c = {}      # key -> dict(pos, vel, t, src)
        self.n = 0

    def update(self, obs, me_pos, t):
        seen = set()
        for m in obs.get("mechs") or []:
            if m.get("kind") != "mech":
                continue
            k = ("m", m["id"])
            self.c[k] = {"pos": list(m["pos"][:2]), "vel": list(m.get("vel", [0, 0])[:2]), "t": t, "src": "seen"}
            seen.add(k)
        for s in obs.get("sounds") or []:
            if s["kind"] not in self.LOUD:
                continue
            dist = self.BAND.get(s["dist"], 120.0)
            if s["kind"] in ("step", "reload"):
                dist = min(dist, 30.0)
            b = s["bearing"]
            est = [me_pos[0] + dist * math.cos(b), me_pos[1] + dist * math.sin(b)]
            # refresh a matching contact instead of adding a duplicate
            best, bd = None, 1e9
            for k, c in self.c.items():
                if k in seen:
                    continue
                p = self.predict(c, t)
                cb = math.atan2(p[1] - me_pos[1], p[0] - me_pos[0])
                cd = math.hypot(p[0] - me_pos[0], p[1] - me_pos[1])
                if abs(wrap(cb - b)) < 0.35 and abs(cd - dist) < max(40.0, dist * 0.6):
                    score = abs(wrap(cb - b)) * 100 + abs(cd - dist) * 0.1
                    if score < bd:
                        bd, best = score, k
            if best is not None:
                c = self.c[best]
                if c["src"] != "seen" or t - c["t"] > 1.0:
                    p = self.predict(c, t)
                    c["pos"] = [(p[0] + est[0]) / 2, (p[1] + est[1]) / 2]
                    c["vel"] = [0.0, 0.0]
                c["t"] = t
            else:
                self.n += 1
                self.c[("s", self.n)] = {"pos": est, "vel": [0.0, 0.0], "t": t, "src": "heard"}
        for ev in obs.get("events") or []:
            if ev.get("type") == "damaged" and ev.get("by") is not None and ev["by"] < 100 and ev.get("bearing") is not None:
                k = ("m", ev["by"])
                if k in seen:
                    continue
                b = ev["bearing"]
                old = self.c.get(k)
                dist = math.hypot(old["pos"][0] - me_pos[0], old["pos"][1] - me_pos[1]) if old else 100.0
                self.c[k] = {"pos": [me_pos[0] + dist * math.cos(b), me_pos[1] + dist * math.sin(b)],
                             "vel": [0.0, 0.0], "t": t, "src": "shot"}
            elif ev.get("type") == "kill":
                self.c.pop(("m", ev.get("target")), None)
        # forget stale contacts
        if len(self.c) > 30 or int(t * 10) % 10 == 0:
            self.c = {k: v for k, v in self.c.items()
                      if t - v["t"] < (25.0 if k[0] == "m" else 10.0)}

    @staticmethod
    def predict(c, t):
        dt = min(3.0, t - c["t"])
        return (c["pos"][0] + c["vel"][0] * dt, c["pos"][1] + c["vel"][1] * dt)

    def nearby(self, me_pos, t, radius, max_age=10.0, exclude=None):
        """Distinct contacts probably within radius (heard ones merged if close together)."""
        pts = []
        for k, c in self.c.items():
            if k == exclude or t - c["t"] > max_age:
                continue
            p = self.predict(c, t)
            if math.hypot(p[0] - me_pos[0], p[1] - me_pos[1]) <= radius:
                if c["src"] != "seen" and any(math.hypot(p[0] - q[0], p[1] - q[1]) < 30 for q in pts):
                    continue
                pts.append(p)
        return pts


# --------------------------------------------------------------------------- bot
class Bot:
    name = "quiet-survivor"

    def __init__(self, hello):
        self.cfg = hello["config"]
        random.seed(hello.get("seed", 0))
        self.slot = hello.get("slot", 0)
        self.nav = None
        try:
            t0 = time.perf_counter()
            mapdir = hello["map"]["dir"]
            cache = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nav.bin")
            try:
                fp = Nav.fingerprint(mapdir)
            except Exception:
                fp = None
            try:
                self.nav = Nav.load(cache, fp)
            except Exception as e:
                mf.log(f"nav cache unusable ({e!r}); building from map")
                self.nav = Nav(mapdir, self.cfg.get("mech", {}))
            self.nav.gh = array("f", self.nav.gh)
            self.nav.top = array("f", self.nav.top)
            self.nav.base = array("f", self.nav.base)
            mf.log(f"nav ready {self.nav.nx}x{self.nav.ny} in {time.perf_counter() - t0:.2f}s")
        except Exception as e:  # play without a map rather than not at all
            mf.log(f"nav failed: {e!r}")
        self.goal = None
        self.goal_t = -99
        self.path = []
        self.path_goal = None
        self.path_t = -99
        self.pidx = 0
        self.attackers = {}      # mech id -> last time it hurt us
        self.npc_aggro = {}      # npc id -> last time it hurt us
        self.npcs = {}           # npc id -> (pos, t)
        self.dead_npcs = set()
        self.last_mech_seen = -99
        self.look = None         # (yaw, until)
        self.hurt_bearing = None  # (bearing, t)
        self.strafe = 1
        self.strafe_until = 0
        self.stuck_since = None
        self.unstick_until = -1
        self.last_take = {}
        self.loot_target = None
        self.blacklist = {}
        self.field = None
        self.field_next = None
        self.hide_cache = None
        self.watch = None
        self.stance = "idle"
        self.threats = {}
        W = self.cfg.get("weapons", {})
        try:
            g, l, li, mo = W["gun"], W["laser"], W["lightning"], W["mortar"]
            self.W_DPS = {"gun": g["damage"] / g["cooldown"] * (g["mag"] * g["cooldown"]) / (g["mag"] * g["cooldown"] + g["reload"]),
                          "laser": l["damage"] / (l["charge"] + l["cooldown"]),
                          "lightning": li["damage"] / li["cooldown"],
                          "mortar": (mo["damage"] + mo["splash"]) / 2 / mo["cooldown"]}
            self.W_RANGE = {"gun": g["range"], "laser": l["range"], "lightning": li["range"], "mortar": 230.0}
        except Exception:
            pass
        self.es = {}   # enemy weapon state: id -> {"shots", "reload_until", "laser_ready"}
        self.seen_proj = {}
        self.chase = None   # (target id, start t, start dist)
        self.reinf = []       # seconds from our first shot until another enemy showed up
        self.engage = None    # (t of first shot, headcount then)
        self.last_fire_t = -99
        self.spotted = False
        self.reload_obs = []  # observed enemy reload pauses
        self.used_spots = []  # (pos, t) hiding spots used recently
        self.relocate_until = -1
        self.loot = {}        # pickup id -> (kind, pos, t last seen)
        self.rushing = False
        self.tracker = Tracker()
        self.local = 0
        gc.collect()
        try:
            gc.freeze()
        except AttributeError:
            pass
        gc.set_threshold(50000, 50, 1000)

    # ------------------------------------------------------------ helpers
    W_DPS = {"gun": 180.0, "laser": 125.0, "lightning": 200.0, "mortar": 90.0}
    W_RANGE = {"gun": 230.0, "laser": 280.0, "lightning": 45.0, "mortar": 230.0}

    def dps_at(self, kinds, d):
        tot = 0.0
        for k in kinds:
            if not k or d > self.W_RANGE.get(k, 200):
                continue
            base = self.W_DPS.get(k, 100.0)
            if k == "gun":
                acc = max(0.25, min(1.0, 1.25 - d / 140.0))  # strafing targets dodge at range
            elif k == "laser":
                acc = 1.4   # 315 in one instant hit: front-loaded
            elif k == "mortar":
                acc = 0.35
            else:
                acc = 1.0
            tot += base * acc
        return max(tot, 1.0)

    def my_dps(self, wk, weapons):
        return [k for k in wk if k]

    def advantage(self, m, d, my_hp, my_kinds, facing_me):
        """> 1: we should win the duel. Ratio of the time they need to kill us to the time we need."""
        mine = self.dps_at(my_kinds, d)
        if not self.enemy_kinds_now(m, self._t):
            mine *= 2.0   # it cannot shoot back right now: press
        theirs = self.dps_at(self.enemy_kinds_now(m, self._t), d)
        if not facing_me:
            mine *= 1.3          # we hit its back and get the first volley
        cube_mul = 1 + 0.04 * min(m.get("cubes", 0), 10)
        t_us = my_hp / (theirs * cube_mul)
        t_them = m.get("hp", 1400) / mine
        return t_us / max(t_them, 1e-3)

    def infer_enemy_weapons(self, obs, mechs, t):
        """Count bullets each visible enemy fires (12 a magazine), listen for reloads, note laser
        shots: tells us when an enemy cannot shoot back."""
        g = self.cfg["weapons"]["gun"]
        lz = self.cfg["weapons"]["laser"]
        for pr in obs.get("projectiles") or []:
            if pr.get("kind") != "gun" or pr["id"] in self.seen_proj:
                continue
            self.seen_proj[pr["id"]] = t
            v = pr.get("vel") or [0, 0, 0]
            sp = math.hypot(v[0], v[1]) or 1
            best, bd = None, 6.0
            for m in mechs:
                # distance from the shooter to the bullet's path behind it (it left < 0.25 s ago)
                rx, ry = m["pos"][0] - pr["pos"][0], m["pos"][1] - pr["pos"][1]
                along = -(rx * v[0] + ry * v[1]) / sp
                back = -along
                if not (-2 <= back <= sp * 0.25):
                    continue
                perp = abs(rx * v[1] - ry * v[0]) / sp
                if perp < bd:
                    bd, best = perp, m
            if best is not None:
                e = self.es.setdefault(best["id"], {"shots": 0, "reload_until": 0.0, "laser_ready": 0.0})
                gap = t - e.get("last_shot", -99)
                if e.get("full") and 1.2 < gap < 5.0:
                    self.reload_obs.append(gap)           # measured pause after a full magazine
                    self.reload_obs = self.reload_obs[-12:]
                e["full"] = False
                e["last_shot"] = t
                e["shots"] += 1
                if e["shots"] >= g["mag"]:
                    e["reload_until"] = t + self.reload_est()
                    e["shots"] = 0
                    e["full"] = True
        for b in obs.get("beams") or []:
            if b.get("kind") == "laser" and b.get("by") is not None:
                e = self.es.setdefault(b["by"], {"shots": 0, "reload_until": 0.0, "laser_ready": 0.0})
                e["laser_ready"] = t + lz["cooldown"] + lz["min_charge"] - b.get("age", 0)
        for snd in obs.get("sounds") or []:
            if snd["kind"] != "reload":
                continue
            for m in mechs:
                bb = math.atan2(m["pos"][1] - obs["self"]["pos"][1], m["pos"][0] - obs["self"]["pos"][0])
                if abs(wrap(bb - snd["bearing"])) < 0.3:
                    e = self.es.setdefault(m["id"], {"shots": 0, "reload_until": 0.0, "laser_ready": 0.0})
                    e["reload_until"] = t + g["reload"] - snd.get("age", 0)
                    e["shots"] = 0
        if len(self.seen_proj) > 400:
            self.seen_proj = {k: v for k, v in self.seen_proj.items() if t - v < 2}

    def reload_est(self):
        g = self.cfg["weapons"]["gun"]["reload"]
        if len(self.reload_obs) >= 2:
            return 0.5 * g + 0.5 * (sum(self.reload_obs) / len(self.reload_obs))
        return g

    def reinf_est(self):
        if not self.reinf:
            return TP["reinf_prior"]
        return 0.4 * TP["reinf_prior"] + 0.6 * (sum(self.reinf) / len(self.reinf))

    def am_i_seen(self, mechs, pos):
        """Some visible enemy is facing us within its view cone (team shares this)."""
        half = math.radians(self.cfg["vision"].get("fov_deg", 120)) / 2
        near = self.cfg["vision"].get("near_range", 45)
        for m in mechs:
            if m["d"] < near:
                return True
            b = math.atan2(pos[1] - m["pos"][1], pos[0] - m["pos"][0])
            if abs(wrap(m["aim"][0] - b)) < half and m["d"] < self.cfg["vision"].get("range", 200):
                return True
        return False

    def find_cluster(self, mechs, pos):
        """Two or more enemies within one blast: returns (centroid, mean vel, count)."""
        r = self.cfg["weapons"]["mortar"].get("splash_radius", 7) * 1.4
        best = None
        for m in mechs:
            grp = [o for o in mechs if hdist(o["pos"], m["pos"]) < r]
            if len(grp) >= 2 and 40 < m["d"] < 215 and (best is None or len(grp) > best[2]):
                cx = sum(o["pos"][0] for o in grp) / len(grp)
                cy = sum(o["pos"][1] for o in grp) / len(grp)
                cz = min(o["pos"][2] for o in grp)
                vx = sum(o.get("vel", [0, 0])[0] for o in grp) / len(grp)
                vy = sum(o.get("vel", [0, 0])[1] for o in grp) / len(grp)
                best = ([cx, cy, cz], [vx, vy, 0.0], len(grp))
        return best

    def enemy_kinds_now(self, m, t):
        """Weapons this enemy can actually use right now."""
        kinds = list(m.get("weapons") or ["gun"])
        e = self.es.get(m["id"])
        if e:
            if t < e["reload_until"] and "gun" in kinds:
                kinds.remove("gun")
            if t < e["laser_ready"] and "laser" in kinds and not m.get("charging"):
                kinds.remove("laser")
        return kinds

    def trap_risk(self, m, pos, t, obs):
        """Is chasing m walking into something? Other contacts near it, a drone, or out of the zone."""
        mp = m["pos"]
        if len(self.tracker.nearby(mp, t, 70.0, exclude=("m", m["id"]))) > 0:
            return True
        for nid, (np_, nt) in self.npcs.items():
            if nid not in self.dead_npcs and t - nt < 60 and hdist(mp, np_) < 50:
                return True
        z = obs["zone"]
        if hdist(mp, z["next_center"]) > z["next_radius"] and obs["t"] > z["shrink_start"] - 20:
            return True
        # a healthy enemy that keeps backing away from us while we chase is luring us
        if self.chase and self.chase[0] == m["id"]:
            cid, t0, d0 = self.chase
            if t - t0 > TP["chase_s"] and m.get("hp", 0) > 0.5 * m.get("max_hp", 1400):
                return True
        return False

    def danger_pen(self, p, obs):
        pen = 0.0
        t = obs["t"]
        for nid, (np_, nt) in self.npcs.items():
            if nid in self.dead_npcs or t - nt > 90:
                continue
            d = hdist(p, np_)
            if d < 70:
                pen += (70 - d) * 0.6
        t_ = obs["t"]
        for c in self.tracker.c.values():
            if t_ - c["t"] > 20:
                continue
            q = Tracker.predict(c, t_)
            d = hdist(p, q)
            if d < 90:
                pen += (90 - d) * 0.35
        for dr in obs.get("drops") or []:
            d = hdist(p, dr["pos"])
            if d < 45:
                pen += (45 - d) * 0.8
        return pen

    def safe_disc(self, obs):
        z = obs["zone"]
        nc, nr = z["next_center"], z["next_radius"]
        if nr <= 1:
            return nc, 0.0
        if nr < 40:
            return nc, nr * 0.45
        return nc, max(0.0, nr * 0.72 - 6)

    def pick_goal(self, obs, me):
        nav = self.nav
        nc, R = self.safe_disc(obs)
        pos = me["pos"]
        if R < 4 or nav is None:
            return list(nc)
        best, bs = None, -1e18
        cands = []
        # our own projection into the disc
        d = hdist(pos, nc)
        if d <= R:
            cands.append((pos[0], pos[1]))
        else:
            f = (R - 2) / d
            cands.append((nc[0] + (pos[0] - nc[0]) * f, nc[1] + (pos[1] - nc[1]) * f))
        for _ in range(140):
            r = R * math.sqrt(random.random())
            a = random.random() * TAU
            cands.append((nc[0] + r * math.cos(a), nc[1] + r * math.sin(a)))
        for p in cands:
            c = nav.cell(*p)
            c = nav.nearest_free(c, 4)
            if c is None:
                continue
            q = nav.xy(c)
            if hdist(q, nc) > R + 3:
                continue
            cov = nav.cover(c)
            cov = min(cov, 18)
            s = cov * 2.5 - hdist(pos, q) * 0.12 - self.danger_pen(q, obs) - hdist(q, nc) * 0.03
            for (up, ut) in self.used_spots:
                if obs["t"] - ut < 120 and hdist(q, up) < 30:
                    s -= 25
            if s > bs:
                bs, best = s, q
        return list(best) if best else list(nc)

    def follow(self, me, goal, t):
        """Return a unit horizontal direction toward goal along a path."""
        pos = me["pos"]
        nav = self.nav
        dx, dy = goal[0] - pos[0], goal[1] - pos[1]
        dist = math.hypot(dx, dy)
        if dist < 0.5:
            return (0.0, 0.0), 0.0
        if nav is None:
            return (dx / dist, dy / dist), dist
        if goal is self.goal:
            return self.follow_field(me, goal, dx, dy, dist)
        need = (self.path_goal is None or hdist(self.path_goal, goal) > 6 or t - self.path_t > 4
                or not self.path)
        if not need and self.pidx < len(self.path):
            if hdist(nav.xy(self.path[self.pidx]), pos) > 10:
                need = True
        if need:
            s = nav.nearest_free(nav.cell(pos[0], pos[1]), 4)
            g = nav.nearest_free(nav.cell(goal[0], goal[1]), 8)
            p = nav.astar(s, g, budget=0.008, maxexp=6000)
            self.path = p or []
            self.pidx = 0
            self.path_goal = list(goal)
            self.path_t = t
        path = self.path
        if not path:
            return (dx / dist, dy / dist), dist
        while self.pidx < len(path) - 1 and hdist(nav.xy(path[self.pidx]), pos) < 2.5:
            self.pidx += 1
        # string pull: furthest visible point a few cells ahead
        tgt = nav.xy(path[self.pidx])
        for k in range(min(len(path) - 1, self.pidx + 12), self.pidx, -1):
            q = nav.xy(path[k])
            if nav.walk_line(pos, q):
                tgt = q
                break
        if self.pidx >= len(path) - 1 and hdist(nav.xy(path[-1]), goal) < 4:
            tgt = goal
        ex, ey = tgt[0] - pos[0], tgt[1] - pos[1]
        e = math.hypot(ex, ey)
        rem = 0.0
        prev = pos
        for k in range(self.pidx, len(path)):
            q = nav.xy(path[k])
            rem += hdist(prev, q)
            prev = q
        if e < 0.3:
            return (dx / dist, dy / dist), dist
        return (ex / e, ey / e), max(rem, dist)

    def field_tick(self, budget):
        nav = self.nav
        if nav is None or self.goal is None:
            return
        gc = nav.nearest_free(nav.cell(self.goal[0], self.goal[1]), 10)
        if gc is None:
            return
        if self.field_next is None or self.field_next.goal != gc:
            if self.field is None or self.field.goal != gc:
                self.field_next = Field(nav, gc)
        if self.field_next is not None:
            self.field_next.step(budget)
            if self.field_next.done:
                self.field = self.field_next
                self.field_next = None

    def follow_field(self, me, goal, dx, dy, dist):
        nav = self.nav
        pos = me["pos"]
        f = self.field
        if f is not None and hdist(nav.xy(f.goal), goal) > 25:
            f = None
        if f is None or dist < 6:
            if nav.walk_line(pos, goal) or f is None:
                return (dx / dist, dy / dist), dist
        c0 = nav.cell(pos[0], pos[1])
        c = c0
        if c is None or f.dist[c] < 0:
            c = nav.nearest_free(c, 4)
        if c is None or f.dist[c] < 0:
            return (dx / dist, dy / dist), dist
        if c != c0:
            # we are hugging a wall: step back out to open ground first
            q = nav.xy(c)
            ex, ey = q[0] - pos[0], q[1] - pos[1]
            e = math.hypot(ex, ey)
            if e > 0.4:
                return (ex / e, ey / e), f.dist[c] * nav.G * 1.1
        cells = nav.descend(f.dist, c)
        tgt = None
        for k in range(len(cells) - 1, 0, -1):
            q = nav.xy(cells[k])
            if nav.walk_line(pos, q):
                tgt = q
                break
        if tgt is None:
            tgt = nav.xy(cells[1]) if len(cells) > 1 else goal
        if f.dist[c] <= 2:
            tgt = goal
        ex, ey = tgt[0] - pos[0], tgt[1] - pos[1]
        e = math.hypot(ex, ey)
        rem = f.dist[c] * nav.G * 1.1
        if e < 0.3:
            return (dx / dist, dy / dist), dist
        return (ex / e, ey / e), max(rem, dist if dist < 6 else rem)

    def hide_spot(self, pos, threat_eye, t):
        """Nearest walkable spot within ~30 m that the threat cannot see."""
        nav = self.nav
        if nav is None:
            return None
        if self.hide_cache and t - self.hide_cache[0] < 0.6 and hdist(self.hide_cache[2], threat_eye) < 15:
            return self.hide_cache[1]
        best, bd = None, 1e9
        checks = 0
        for r in (6, 10, 15, 21, 28):
            for k in range(16):
                a = k * TAU / 16
                q = (pos[0] + r * math.cos(a), pos[1] + r * math.sin(a))
                c = nav.cell(*q)
                if c is None or nav.blk[c]:
                    continue
                # prefer spots away from the threat
                away = hdist(q, threat_eye) - hdist(pos, threat_eye)
                cost = r - away * 0.5
                if cost >= bd:
                    continue
                checks += 1
                if checks > 22:
                    break
                pz = nav.gh[c] + 5.0
                if nav.los(threat_eye, (q[0], q[1], pz)):
                    continue
                if not nav.walk_line(pos, q):
                    continue
                best, bd = q, cost
            if best is not None and r >= 10:
                break
        self.hide_cache = (t, best, list(threat_eye))
        return best

    def has_los(self, a, b):
        if self.nav is None:
            return True
        return self.nav.los(a, b)

    # ------------------------------------------------------------ main
    def act(self, obs):
        t0 = time.perf_counter()
        a = self._act(obs)
        used = time.perf_counter() - t0
        try:
            self.field_tick(max(0.002, 0.010 - used))
        except Exception as e:
            mf.log(f"field: {e!r}")
            self.field_next = None
        return a

    def _act(self, obs):
        t = obs["t"]
        self._t = t
        me = obs["self"]
        pos = me["pos"]
        st = me.get("stats", {})
        scale = 1 + self.cfg["cubes"].get("size_per_cube", 0.05) * me.get("cubes", 0)
        muzzle = [pos[0], pos[1], pos[2] + st.get("muzzle_height", 7 * scale)]
        eye = [pos[0], pos[1], pos[2] + st.get("eye_height", 8 * scale)]
        act = {"tick": obs["tick"]}
        hp_frac = me["hp"] / max(1.0, me["max_hp"])

        # ---- events
        for ev in obs.get("events") or []:
            ty = ev.get("type")
            if ty == "damaged":
                by = ev.get("by")
                if by is not None:
                    if by < 100:
                        self.attackers[by] = t
                    else:
                        self.npc_aggro[by] = t
                if ev.get("bearing") is not None and ev.get("weapon") != "zone":
                    self.hurt_bearing = (ev["bearing"], t)
            elif ty == "kill":
                pass
            elif ty == "hit":
                tg = ev.get("target")
                if tg is not None and 100 <= tg < 1000:
                    self.npc_aggro[tg] = t
            elif ty == "take_failed":
                self.blacklist[ev.get("id")] = t

        # ---- upgrades
        offer = obs.get("offer")
        if offer and offer.get("choices"):
            ids = [c.get("id") for c in offer["choices"]]
            best = min(range(len(ids)), key=lambda k: UPGRADE_PREF.index(ids[k]) if ids[k] in UPGRADE_PREF else 99)
            act["pick"] = best

        # ---- perception
        mechs = [m for m in obs.get("mechs") or [] if m.get("kind") == "mech"]
        npcs = [m for m in obs.get("mechs") or [] if m.get("kind") == "npc"]
        seen_npc_ids = set()
        for n in npcs:
            self.npcs[n["id"]] = (n["pos"], t)
            seen_npc_ids.add(n["id"])
        if mechs:
            self.last_mech_seen = t
        self.tracker.update(obs, pos, t)
        self.infer_enemy_weapons(obs, mechs, t)
        self.local = len(self.tracker.nearby(pos, t, TP["crowd_r"]))
        # threat memory: where danger was last seen or heard (pre-aim there when idle)
        for m in mechs:
            self.threats[("m", m["id"])] = (math.atan2(m["pos"][1] - pos[1], m["pos"][0] - pos[0]), t, 3.0, m["pos"])
        for s_ in obs.get("sounds") or []:
            k = s_["kind"]
            if k in ("gun", "laser", "laser_charge", "lightning", "mortar", "step", "boost", "jet", "reload", "explosion"):
                w = {"near": 2.5, "mid": 1.5, "far": 0.6}.get(s_["dist"], 0.5)
                self.threats[("s", k, round(s_["bearing"], 1))] = (s_["bearing"], t, w, None)
        for ev in obs.get("events") or []:
            if ev.get("type") == "damaged" and ev.get("bearing") is not None and ev.get("weapon") != "zone":
                self.threats[("d", ev.get("by"))] = (ev["bearing"], t, 4.0, None)
        if len(self.threats) > 40 or (obs["tick"] % 20 == 0):
            self.threats = {k: v for k, v in self.threats.items() if t - v[1] < 15}
        for m in mechs:
            m["d"] = math.dist(pos, m["pos"])
        for n in npcs:
            n["d"] = math.dist(pos, n["pos"])

        weapons = me.get("weapons") or [None, None]
        wk = [w["kind"] if w else None for w in weapons]
        gun_i = wk.index("gun") if "gun" in wk else None
        laser_i = wk.index("laser") if "laser" in wk else None
        light_i = wk.index("lightning") if "lightning" in wk else None
        mortar_i = wk.index("mortar") if "mortar" in wk else None

        # ---- read the situation and pick a stance
        #   fight   : shoot it (we are attacked and can win, it is close, it is weak, or an ambush)
        #   evade   : it is hurting us and we would lose: break line of sight and recover
        #   watch   : seen but not worth it: face it, stay quiet, keep hiding
        my_hp = me["hp"]
        my_dps = [k for i, k in enumerate(wk) if k and not (
            (k == "gun" and (weapons[i].get("reload") or 0) > 0.4) or
            (k == "laser" and (weapons[i].get("cooldown") or 0) > 0.8 and not (weapons[i].get("charge") or 0)))]
        target = None
        stance = "watch"
        best = None
        self.spotted = self.am_i_seen(mechs, pos)
        for m in mechs:
            d = m["d"]
            bear_to_me = math.atan2(pos[1] - m["pos"][1], pos[0] - m["pos"][0])
            facing_me = abs(wrap(m["aim"][0] - bear_to_me)) < TP["facing_rad"]
            attacked = t - self.attackers.get(m["id"], -99) < 4
            adv = self.advantage(m, d, my_hp, my_dps, facing_me)
            others = len(self.tracker.nearby(pos, t, TP["crowd_r"], exclude=("m", m["id"])))
            if others:
                # several enemies around: each extra one cuts our odds
                adv = adv / (1.0 + TP["crowd_pen"] * others)
            weak = m.get("hp", 9999) < TP["vulture_hp"] * m.get("max_hp", 1400)
            if attacked:
                stn, pri = ("fight" if adv >= TP["adv_attacked"] else "evade"), 0
            elif m.get("charging") and facing_me and d < 280:
                stn, pri = ("fight" if (laser_i is not None and adv >= TP["adv_laser"]) else "evade"), 1
            elif "laser" in (m.get("weapons") or []) and facing_me and d < 280 and "laser" in self.enemy_kinds_now(m, t):
                # an instant 315 hit is coming: only cover helps
                stn, pri = ("fight" if (laser_i is not None and adv >= TP["adv_laser"]) or d < 40 else "evade"), 1
            elif d < TP["close_d"]:
                stn, pri = ("fight" if adv >= TP["adv_close"] else "evade"), 2
            elif weak and others == 0 and d < TP["vulture_d"] and adv >= TP["adv_vulture"] and not self.trap_risk(m, pos, t, obs):
                stn, pri = "vulture", 3
            elif (not self.spotted and not facing_me and d < TP["ambush_d"] and adv >= TP["adv_ambush"]
                  and others <= TP["ambush_others"] and not self.trap_risk(m, pos, t, obs)):
                stn, pri = "fight", 4
            else:
                stn, pri = "watch", 9
            # among equals, the enemy we can drop fastest first: fewer eyes on us sooner
            ttk = m.get("hp", 1400) / self.dps_at(my_dps, d)
            key = (pri, ttk)
            if best is None or key < best:
                best = key
                target, stance = m, stn
        # remember how long we have been going after an enemy that did not start it
        if target is not None and best is not None and best[0] >= 3:
            if not self.chase or self.chase[0] != target["id"]:
                self.chase = (target["id"], t, target["d"])
        elif target is None or (best is not None and best[0] < 3):
            self.chase = None
        # learn how fast the team reinforces, and leave before they arrive
        if self.engage is not None:
            t0e, base = self.engage
            if self.local > base and t - t0e < 60:
                self.reinf.append(t - t0e)
                self.reinf = self.reinf[-8:]
                self.engage = None
            elif t - self.last_fire_t > 10:
                self.engage = None
        if (target is not None and stance in ("fight", "vulture") and self.engage is not None
                and t - self.engage[0] > TP["hitrun_frac"] * self.reinf_est()
                and target.get("hp", 1400) > 0.3 * target.get("max_hp", 1400) and self.local >= 1):
            stance = "evade"
            self.relocate_until = t + 4.0
        if target is not None and t < self.relocate_until and stance == "fight" and best is not None and best[0] >= 2:
            stance = "evade"
        if target is not None and stance == "watch":
            self.watch = target
            target = None
        else:
            self.watch = None
        self.stance = stance if target is not None else "idle"
        npc_target = None
        if target is None:
            for n in npcs:
                aggro = t - self.npc_aggro.get(n["id"], -99) < 4
                if aggro and hp_frac > TP["npc_fight_hp"] and n["d"] < 80:
                    if npc_target is None or n["d"] < npc_target["d"]:
                        npc_target = n
            target = npc_target
            if target is not None:
                self.stance = "fight"

        # ---- zone / navigation goal
        z = obs["zone"]
        cc, cr = z["center"], z["radius"]
        nc, R = self.safe_disc(obs)
        if (self.goal is None or hdist(self.goal, nc) > R + 4 or t - self.goal_t > 45
                or (self.spotted and t - self.goal_t > 12 and hdist(pos, self.goal) < 8)):
            if self.goal is not None:
                self.used_spots.append((list(self.goal), t))
                self.used_spots = self.used_spots[-10:]
            self.goal = self.pick_goal(obs, me)
            self.goal_t = t
            self.path = []
        goal = self.goal

        # loot / cube detour when quiet
        quiet = target is None and t - self.last_mech_seen > 3
        take_id = None
        drop_slot = None
        loot_goal = None
        reach = self.cfg["loot"].get("pickup_radius", 4)
        special = [k for k in wk if k and k != "gun"]
        best_rank = max([WEAPON_RANK.get(k, 0) for k in special] or [0])
        empty = None in wk
        # remember every weapon we have seen lying around; forget ones that vanished
        vis_ids = set()
        for p in obs.get("pickups") or []:
            self.loot[p["id"]] = (p["kind"], p["pos"], t)
            vis_ids.add(p["id"])
        near_r = self.cfg["vision"].get("near_range", 45) * 0.8
        for lid in list(self.loot):
            k_, lp, lt = self.loot[lid]
            if lid not in vis_ids and hdist(lp, pos) < near_r and t - lt > 0.5:
                del self.loot[lid]     # should be in view and is not: someone took it
        for ev in obs.get("events") or []:
            if ev.get("type") in ("took", "take_failed") and ev.get("id") in self.loot:
                del self.loot[ev["id"]]
        # loot rush: until we hold a special weapon, go get the best one we know of
        rush = None
        if best_rank < 4:
            horizon = TP["loot_r_early"] if t < 120 else TP["loot_r_late"]
            fighting = target is not None and target.get("kind") == "mech"
            bs = None
            for lid, (k_, lp, lt) in self.loot.items():
                rank = WEAPON_RANK.get(k_, 0)
                if rank <= best_rank or k_ in wk:
                    continue
                if self.nav is not None and lp[2] - self.nav.ground(lp[0], lp[1]) > 2.5:
                    continue   # on a roof
                dd = hdist(pos, lp)
                if dd > horizon or (fighting and dd > 25) or hdist(lp, cc) > cr - 8:
                    continue
                sc = dd / (1.0 + 0.5 * rank)
                if bs is None or sc < bs:
                    bs, rush = sc, lp
        for p in obs.get("pickups") or []:
            if p["id"] in self.blacklist and t - self.blacklist[p["id"]] < 10:
                continue
            k = p["kind"]
            if k in wk:
                continue
            rank = WEAPON_RANK.get(k, 0)
            if not empty and rank <= best_rank:
                continue
            if k == "gun" and gun_i is not None:
                continue
            d = hdist(pos, p["pos"])
            if d < reach - 0.3 and abs(p["pos"][2] - pos[2]) < 4:
                take_id = p["id"]
                if not empty:
                    # replace the worst special
                    worst = min((i for i, kk in enumerate(wk) if kk and kk != "gun"),
                                key=lambda i: WEAPON_RANK.get(wk[i], 0), default=None)
                    if worst is None or (laser_i == worst and (weapons[worst].get("charge") or 0) > 0):
                        take_id = None
                    else:
                        drop_slot = worst
                break
            on_roof = self.nav is not None and p["pos"][2] - self.nav.ground(p["pos"][0], p["pos"][1]) > 2.5
            if quiet and d < 55 and not on_roof and hdist(p["pos"], cc) < cr - 10:
                if loot_goal is None or d < hdist(pos, loot_goal):
                    loot_goal = p["pos"]
        if quiet:
            for cu in obs.get("cubes") or []:
                d = hdist(pos, cu["pos"])
                if d < 60 and not (self.nav is not None and cu["pos"][2] - self.nav.ground(cu["pos"][0], cu["pos"][1]) > 2.5) and hdist(cu["pos"], cc) < cr - 10:
                    if loot_goal is None or d < hdist(pos, loot_goal):
                        loot_goal = cu["pos"]
        if rush is not None and take_id is None:
            loot_goal = rush
        self.rushing = rush is not None
        if take_id is not None:
            act["take"] = take_id
            if drop_slot is not None:
                act["drop"] = drop_slot

        # time pressure
        d_cur_edge = cr - hdist(pos, cc)
        outside_next = hdist(pos, nc) > z["next_radius"] - 4
        nav_goal = goal
        if loot_goal is not None and (not outside_next or (self.rushing and t < z["shrink_start"] - 15)):
            nav_goal = loot_goal
        mdir, rem = self.follow(me, nav_goal, t)

        walk = st.get("walk_speed", 11.7)
        if not me.get("in_zone", True) or d_cur_edge < 6:
            speed, boost = 1.0, True
        else:
            deadline = max(1.0, z["shrink_end"] - t) if outside_next else 1e9
            need_v = rem / deadline
            if need_v > 0.85 * walk:
                speed, boost = 1.0, True
            elif need_v > 0.3 * walk or loot_goal is not None or rem > 150:
                speed, boost = 1.0, False
            else:
                speed, boost = 0.4, False   # under 5 m/s: no footstep sounds
        if self.rushing and nav_goal is loot_goal:
            speed, boost = 1.0, me.get("energy", 0) > 25
        if rem < 1.5:
            speed = 0.0

        move = [mdir[0] * speed, mdir[1] * speed]

        # avoid drones we are not fighting
        for n in npcs:
            if target is not None and n["id"] == target["id"]:
                continue
            d = hdist(pos, n["pos"])
            if d < 60:
                ax, ay = pos[0] - n["pos"][0], pos[1] - n["pos"][1]
                a = math.hypot(ax, ay) or 1
                wgt = (60 - d) / 60 * 0.9
                move[0] += ax / a * wgt
                move[1] += ay / a * wgt

        # ---- aim & fire
        fire = [False, False]
        aim = None
        energy = me.get("energy", 0)
        if target is not None:
            T = target["pos"]
            th = (3.5 if target["kind"] == "npc" else 9 * (1 + 0.05 * target.get("cubes", 0)))
            Tc = [T[0], T[1], T[2] + th * 0.5]
            V = target.get("vel", [0, 0, 0])
            d = hdist(pos, T)
            los = self.has_los(muzzle, Tc)
            aim_now = me["aim"]

            def err_ok(want, tol):
                ey = abs(wrap(aim_now[0] - want[0])) * max(d, 1)
                ep = abs(aim_now[1] - want[1]) * max(d, 1)
                return ey < tol and ep < tol * 1.4

            laser_w = weapons[laser_i] if laser_i is not None else None
            charging = laser_w is not None and (laser_w.get("charge") or 0) > 0
            mortar_w = weapons[mortar_i] if mortar_i is not None else None
            gun_w = weapons[gun_i] if gun_i is not None else None

            direct = aim_to(muzzle, Tc)
            mode = "gun"
            if laser_w is not None and (charging or (laser_w.get("cooldown", 1) <= 0 and los and d < laser_w["stats"].get("range", 280) * 0.95)):
                mode = "laser"
            elif mortar_w is not None and mortar_w.get("cooldown", 1) <= 0 and 35 < d < 210 and (
                    not los or gun_w is None or (gun_w.get("reload") or 0) > 0 or d > 120):
                mode = "mortar"

            clus = self.find_cluster(mechs, pos) if mortar_w is not None and mortar_w.get("cooldown", 1) <= 0 else None
            if clus is not None and mode != "laser":
                mode = "mortar"
                T, V = clus[0], clus[1]
                Tc = [T[0], T[1], T[2] + 1.0]
                los = False  # lob high over cover
            if mode == "laser":
                aim = direct
                lstats = laser_w["stats"]
                full = lstats.get("charge", 0.75)
                ch = laser_w.get("charge") or 0
                if ch < full * 0.97:
                    fire[laser_i] = los or charging
                else:
                    # release when on target; keep holding until then (auto-fires after hold_s)
                    fire[laser_i] = not (los and err_ok(aim, 2.0))
                if not los and charging and ch >= full:
                    fire[laser_i] = True
            elif mode == "mortar":
                ms = mortar_w["stats"]
                v, g = ms.get("speed", 72), ms.get("gravity", 22)
                tube = pos[2] + st.get("height", 9) + 0.5
                P = Tc
                pitch = None
                for _ in range(2):
                    dd = hdist(pos, P)
                    pitch = mortar_pitch(dd, (T[2] + 0.5) - tube, v, g, high=not los)
                    if pitch is None:
                        break
                    tof = dd / max(1e-3, v * math.cos(pitch))
                    P = [T[0] + V[0] * tof, T[1] + V[1] * tof, T[2]]
                if pitch is not None:
                    aim = [math.atan2(P[1] - pos[1], P[0] - pos[0]), pitch]
                    if err_ok(aim, 2.5):
                        fire[mortar_i] = True
                else:
                    mode = "gun"
            if mode == "gun" or aim is None:
                if gun_w is not None:
                    gs = gun_w["stats"]
                    P, _ = lead_point(muzzle, Tc, V, gs.get("speed", 252))
                    aim = aim_to(muzzle, P)
                    if los and d < gs.get("range", 230) and (gun_w.get("reload") or 0) <= 0 and err_ok(aim, 2.0 + d * 0.008):
                        fire[gun_i] = True
                else:
                    aim = direct
            if light_i is not None:
                lw = weapons[light_i]
                ls = lw["stats"]
                if (d < ls.get("range", 45) * 0.92 and los and lw.get("cooldown", 1) <= 0
                        and energy > ls.get("energy", 3) + 4
                        and abs(wrap(aim_now[0] - direct[0])) < math.radians(ls.get("cone_deg", 30)) * 0.4):
                    fire[light_i] = True
                if d < ls.get("range", 45) * 1.5 and mode == "gun":
                    aim = [direct[0], aim[1]]
            # keep the laser charge if we switched away from it mid-charge
            if charging and mode != "laser":
                fire[laser_i] = True

            # ---- combat movement
            if t > self.strafe_until:
                self.strafe = -self.strafe if random.random() < 0.6 else self.strafe
                self.strafe_until = t + random.uniform(0.7, 1.6)
            tx, ty_ = T[0] - pos[0], T[1] - pos[1]
            tl = math.hypot(tx, ty_) or 1
            ux, uy = tx / tl, ty_ / tl
            px, py = -uy * self.strafe, ux * self.strafe
            if light_i is not None:
                pref = 28
            elif laser_i is not None:
                pref = 90
            else:
                pref = 55
            their_hp = target.get("hp", 1400)
            retreat = target["kind"] == "mech" and hp_frac < TP["retreat_hp"] and their_hp > my_hp * 1.2
            if target["kind"] == "npc":
                pref = 40
            if self.stance == "vulture":
                pref = min(pref, 35)
            radial = max(-1.0, min(1.0, (d - pref) / 25.0))
            if retreat:
                radial = -1.0
            cm = [px * 0.85 + ux * radial * 0.7, py * 0.85 + uy * radial * 0.7]
            # long-range or losing duels: break line of sight instead of trading shots
            gun_down = gun_w is not None and (gun_w.get("reload") or 0) > 0.5 and laser_i is None and light_i is None
            evade = target["kind"] == "mech" and (self.stance == "evade" or retreat or (gun_down and d < 150))
            if evade:
                teye = [T[0], T[1], T[2] + 8]
                hs = self.hide_spot(pos, teye, t)
                if hs is not None:
                    hx, hy = hs[0] - pos[0], hs[1] - pos[1]
                    hl = math.hypot(hx, hy) or 1
                    cm = [hx / hl + px * 0.25, hy / hl + py * 0.25]
            # zone wins over combat footwork when we are near the edge
            if not me.get("in_zone", True) or d_cur_edge < 15 or outside_next and (z["shrink_end"] - t) < rem / walk + 8:
                cm = [cm[0] * 0.5 + mdir[0], cm[1] * 0.5 + mdir[1]]
            move = cm
            speed_needed = True
            boost = energy > (35 if light_i is not None else 20) and target["kind"] == "mech"
            # dodge an incoming laser
            if target.get("charging"):
                boost = energy > 10
                move = [px, py]
        else:
            # no target: look around / toward threats
            look_yaw = None
            if mechs:
                m0 = min(mechs, key=lambda m: m["d"])
                look_yaw = math.atan2(m0["pos"][1] - pos[1], m0["pos"][0] - pos[0])
            elif self.hurt_bearing and t - self.hurt_bearing[1] < 3:
                look_yaw = self.hurt_bearing[0]
                # being shot by something unseen: get moving, sideways
                if t > self.strafe_until:
                    self.strafe = -self.strafe
                    self.strafe_until = t + 1.2
                b = self.hurt_bearing[0]
                move = [move[0] + -math.sin(b) * self.strafe * 0.8, move[1] + math.cos(b) * self.strafe * 0.8]
                boost = energy > 25
            else:
                snd = None
                for s in obs.get("sounds") or []:
                    if s["kind"] in ("gun", "laser", "laser_charge", "lightning", "step", "boost", "jet", "reload", "mortar") and s["dist"] in ("near", "mid"):
                        if snd is None or (s["dist"] == "near" and snd["dist"] != "near"):
                            snd = s
                if snd is not None:
                    self.look = (snd["bearing"], t + 1.5)
                if self.look and t < self.look[1]:
                    look_yaw = self.look[0]
            if look_yaw is None and t < 3.0:
                look_yaw = wrap(me["aim"][0] + 1.2)      # opening scan: look all the way round for loot
            if look_yaw is None and self.threats:
                # face the most pressing remembered threat (weight fades over ~12 s)
                bestv, by = 0.0, None
                for (b, tt, w, p_) in self.threats.values():
                    if p_ is not None:
                        b = math.atan2(p_[1] - pos[1], p_[0] - pos[0])
                    v = w * max(0.0, 1 - (t - tt) / 12.0)
                    if v > bestv:
                        bestv, by = v, b
                if by is not None and bestv > 0.15:
                    look_yaw = by + 0.35 * math.sin(t * 1.3)
            if look_yaw is None:
                heading = math.atan2(move[1], move[0]) if math.hypot(*move) > 0.05 else None
                if heading is not None:
                    look_yaw = heading + 1.0 * math.sin(t * 0.9)
                else:
                    look_yaw = wrap(t * 0.7 + self.slot)
            aim = [look_yaw, 0.0]
            # unseen and a group is bunched up: lob a shell into it, then move
            if mortar_i is not None and not self.spotted and (weapons[mortar_i].get("cooldown") or 1) <= 0:
                clus = self.find_cluster(mechs, pos)
                if clus is not None:
                    ms = weapons[mortar_i]["stats"]
                    v_, g_ = ms.get("speed", 72), ms.get("gravity", 22)
                    tube = pos[2] + st.get("height", 9) + 0.5
                    Pp = clus[0]
                    pitch = None
                    for _ in range(2):
                        dd = hdist(pos, Pp)
                        pitch = mortar_pitch(dd, clus[0][2] + 0.5 - tube, v_, g_, high=True)
                        if pitch is None:
                            break
                        tof = dd / max(1e-3, v_ * math.cos(pitch))
                        Pp = [clus[0][0] + clus[1][0] * tof, clus[0][1] + clus[1][1] * tof, clus[0][2]]
                    if pitch is not None:
                        aim = [math.atan2(Pp[1] - pos[1], Pp[0] - pos[0]), pitch]
                        ey = abs(wrap(me["aim"][0] - aim[0])) * hdist(pos, Pp)
                        if ey < 3.0 and abs(me["aim"][1] - pitch) < 0.05:
                            fire[mortar_i] = True
                            self.relocate_until = t + 5.0
                            self.goal = None
            # top up the gun while it is quiet (or nothing is shooting at us yet)
            if gun_i is not None and (quiet or (self.watch is not None and self.watch["d"] > 90)):
                gw = weapons[gun_i]
                if (gw.get("ammo", 99) < gw["stats"].get("mag", 12) * 0.6 and (gw.get("reload") or 0) <= 0):
                    act["reload"] = True
            if laser_i is not None and (weapons[laser_i].get("charge") or 0) > 0:
                fire[laser_i] = False  # let it go rather than walk at half speed

        # ---- dodge incoming bullets and shells
        cz = pos[2] + st.get("height", 9) * 0.5
        dodge = None
        soonest = 9.0
        for pr in obs.get("projectiles") or []:
            v = pr.get("vel") or [0, 0, 0]
            vv = v[0] * v[0] + v[1] * v[1] + v[2] * v[2]
            if vv < 1:
                continue
            rx, ry, rz = pr["pos"][0] - pos[0], pr["pos"][1] - pos[1], pr["pos"][2] - cz
            tca = -(rx * v[0] + ry * v[1] + rz * v[2]) / vv
            if not (0 < tca < 0.9):
                continue
            ox, oy, oz = rx + v[0] * tca, ry + v[1] * tca, rz + v[2] * tca
            rad = st.get("radius", 2.5) + (5.0 if pr.get("kind") == "mortar" else 0.8)
            if math.hypot(ox, oy) < rad and abs(oz) < st.get("height", 9) * 0.5 + 1 and tca < soonest:
                soonest = tca
                hl = math.hypot(v[0], v[1]) or 1
                px_, py_ = -v[1] / hl, v[0] / hl          # sidestep perpendicular to the shot
                side = 1.0 if (ox * px_ + oy * py_) <= 0 else -1.0
                dodge = (px_ * side, py_ * side)
        if dodge is not None:
            move = [dodge[0] + move[0] * 0.3, dodge[1] + move[1] * 0.3]
            boost = boost or energy > 12

        # ---- stuck handling
        hv = math.hypot(me["vel"][0], me["vel"][1])
        want = math.hypot(move[0], move[1])
        jump = False
        if want > 0.3 and hv < 1.2 and me.get("grounded", True):
            if self.stuck_since is None:
                self.stuck_since = t
            elif t - self.stuck_since > 0.6:
                self.unstick_until = t + 1.2
                self.stuck_since = None
                self.path = []
                self.strafe = -self.strafe
        else:
            self.stuck_since = None
        if t < self.unstick_until:
            if me.get("grounded", True):
                jump = True
            boost = energy > 8

        ml = math.hypot(move[0], move[1])
        if ml > 1:
            move = [move[0] / ml, move[1] / ml]
        act["move"] = [round(move[0], 3), round(move[1], 3)]
        act["boost"] = bool(boost)
        act["jump"] = jump
        if aim is not None:
            mp = st.get("min_pitch", -0.7)
            xp = st.get("max_pitch", 1.3)
            act["aim"] = [round(wrap(aim[0]), 4), round(max(mp, min(xp, aim[1])), 4)]
        if any(fire):
            if self.engage is None and t - self.last_fire_t > 8:
                self.engage = (t, self.local)
            self.last_fire_t = t
        act["fire"] = fire
        return act


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--precompute":
        nav = Nav(sys.argv[2], {})
        nav.save(sys.argv[3], Nav.fingerprint(sys.argv[2]))
        print("saved", sys.argv[3], file=sys.stderr)
    else:
        mf.run(Bot)
