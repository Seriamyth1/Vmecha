"""Crude kinematic check: does the bot reach the zone from random spawns without getting stuck?"""
import sys, json, gzip, math, random, time
sys.path.insert(0, sys.argv[1])
import importlib.util
spec = importlib.util.spec_from_file_location("botmod", sys.argv[1] + "/bot.py")
bm = importlib.util.module_from_spec(spec); spec.loader.exec_module(bm)
kit = sys.argv[2]
hello = json.loads(gzip.open(kit + "/check/stream.jsonl.gz", "rt").readline())
hello["map"]["dir"] = kit + "/arena"
obs0 = json.loads(gzip.open(kit + "/check/stream.jsonl.gz", "rt").readlines()[1])
random.seed(int(sys.argv[3]) if len(sys.argv) > 3 else 0)
results = []
DBG = {9}
import collections; TM = collections.defaultdict(float)
worst = 0
for trial in range(int(sys.argv[4]) if len(sys.argv) > 4 else 6):
    bot = bm.Bot(dict(hello, seed=trial))
    for nm in ("_act", "field_tick", "pick_goal", "follow"):
        f = getattr(bot, nm)
        def w(*a, _f=f, _n=nm, **k):
            t0 = time.perf_counter(); r = _f(*a, **k); TM[_n] = max(TM[_n], time.perf_counter() - t0); return r
        setattr(bot, nm, w)
    nav = bot.nav
    while True:
        p = [random.uniform(-450, 500), random.uniform(-300, 350)]
        c = nav.cell(*p)
        if nc := (c is not None and not nav.blk[c]): break
    nc_ = [random.uniform(-150, 150), random.uniform(-150, 150)]
    pos = [p[0], p[1], nav.ground(*p)]
    vel = [0, 0]
    stuck_ticks = 0
    for tick in range(0, 1400, 2):
        t = tick / 20
        o = json.loads(json.dumps(obs0))
        o["tick"], o["t"] = tick, t
        o["self"]["pos"] = pos; o["self"]["vel"] = [vel[0], vel[1], 0]
        o["zone"] = {"center": [41.3, -72.8], "radius": 560, "next_center": nc_, "next_radius": 240,
                     "shrink_start": 60, "shrink_end": 100, "dps": 20}
        t0 = time.perf_counter(); a = bot.act(o); worst = max(worst, time.perf_counter() - t0)
        sp = 19.5 if a.get("boost") else 11.7
        mv = a.get("move", [0, 0])
        nxp = [pos[0] + mv[0] * sp * 0.1, pos[1] + mv[1] * sp * 0.1]
        def ok(q):
            c = nav.cell(*q); return c is not None and not nav.raw[c]
        if ok(nxp): pass
        elif ok([nxp[0], pos[1]]): nxp = [nxp[0], pos[1]]
        elif ok([pos[0], nxp[1]]): nxp = [pos[0], nxp[1]]
        else: nxp = pos[:2]
        vel = [(nxp[0] - pos[0]) * 10, (nxp[1] - pos[1]) * 10]
        pos = [nxp[0], nxp[1], nav.ground(*nxp)]
        if trial in DBG and tick % 100 == 0: print(trial, t, [round(v,1) for v in pos], a.get("move"), a.get("jump"), round(bot.unstick_until,1), a.get("boost"), "blk", nav.blk[nav.cell(*pos[:2])], "fd", bot.field and bot.field.dist[nav.cell(*pos[:2])], [round(v) for v in bot.goal])
    dz = math.hypot(pos[0] - nc_[0], pos[1] - nc_[1])
    dg = math.hypot(pos[0] - bot.goal[0], pos[1] - bot.goal[1])
    results.append((round(math.hypot(p[0]-nc_[0], p[1]-nc_[1])), round(dz), round(dg)))
print("start->center, end->center, end->goal:", results)
print({k: round(v*1000,1) for k,v in TM.items()}); print("worst act ms", round(worst * 1000, 1))
