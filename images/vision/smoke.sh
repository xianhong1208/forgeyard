#!/bin/sh
# Smoke test of a built vision image: each task starts without the network, answers /health, and gives a
# real answer for a generated test image (two coloured squares on grey); bad input is a 400.
#   images/vision/smoke.sh forgeyard-vision:ci
set -eu
IMAGE="$1"
here="$(cd "$(dirname "$0")" && pwd)"
img="$(mktemp -d)/test.png"
python3 "$here/testimage.py" "$img"
for task in detect segment depth; do
  name="fy-vision-smoke-$task"
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker run -d --name "$name" --network none "$IMAGE" --task "$task" >/dev/null
  ok=""
  for _ in $(seq 1 90); do
    if docker exec "$name" /opt/vision/bin/python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)" 2>/dev/null; then
      ok=1; break
    fi
    sleep 2
  done
  [ -n "$ok" ] || { docker logs "$name"; echo "$task: never healthy" >&2; exit 1; }
  docker cp "$img" "$name:/tmp/test.png"
  docker exec "$name" /opt/vision/bin/python -c "
import base64, json, sys, urllib.request
task = '$task'
body = {'image': base64.b64encode(open('/tmp/test.png', 'rb').read()).decode()}
if task == 'segment':
    body['box'] = [40, 40, 120, 120]
req = urllib.request.Request('http://127.0.0.1:8000/v1/' + task, data=json.dumps(body).encode(),
                             headers={'Content-Type': 'application/json'})
out = json.load(urllib.request.urlopen(req, timeout=120))
assert out['model'] and out['width'] == 256 and out['height'] == 192, out
if task == 'detect':
    assert isinstance(out['detections'], list)
if task == 'segment':
    best = out['masks'][0]
    assert best['area'] > 3000 and best['box'][0] < 60 and best['box'][2] > 100, best
if task == 'depth':
    assert base64.b64decode(out['depth_png'])[:8] == b'\x89PNG\r\n\x1a\n' and out['max'] > out['min'], out
try:
    urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8000/v1/' + task, data=b'{\"image\": \"eA==\"}',
                                                  headers={'Content-Type': 'application/json'}), timeout=30)
    sys.exit('a bad image was accepted')
except urllib.error.HTTPError as e:
    assert e.code == 400, e.code
print(task, 'ok', out['ms'], 'ms')
"
  docker rm -f "$name" >/dev/null
done
