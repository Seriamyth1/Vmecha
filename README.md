# quiet-survivor — Mech Royale bot

Survival-first bot for the Builders Buildathon Mech Royale track. Placement is all that scores, so it:

- builds a walk grid from `/arena/v2` (buildings, bridges, slopes) and paths with a distance field built a slice per tick
- picks a covered hide spot inside the next zone circle and walks there quietly (< 5 m/s makes no footstep sound)
- avoids NPC drones and supply-drop scrums, breaks line of sight against long-range or laser duels
- fights back when engaged: faces attackers (rear armor), strafes, leads gun shots, uses laser / lightning / mortar when picked up
- grabs weapons and cubes when nothing is around; prefers armor/HP upgrades

Standard library only. Check it like the scorer does (from the starter kit folder):

```sh
docker build -t quiet-survivor .
python3 check/check.py --map-dir /arena -- docker run --rm -i --network none --cpus 1 --memory 512m \
  --read-only --tmpfs /tmp:rw,size=64m --user 65534:65534 -v "$PWD/arena:/arena:ro" quiet-survivor
```
