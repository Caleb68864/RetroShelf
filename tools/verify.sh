#!/usr/bin/env bash
# RetroShelf build/verification harness. Exits non-zero on any failure.
# Usage: bash tools/verify.sh
set -uo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
fail=0
step() { printf '\n=== %s ===\n' "$1"; }
chk() { if [ "$1" -ne 0 ]; then echo "FAIL: $2"; fail=1; else echo "ok: $2"; fi; }

step "compileall (syntax/build)"
$PY -m compileall -q app tests; chk $? "compileall"

step "pytest (full suite)"
$PY -m pytest -q >/tmp/rs_pytest.txt 2>&1; rc=$?
tail -1 /tmp/rs_pytest.txt; chk $rc "pytest"

step "app import (entrypoint)"
KAVITA_OPDS_URL="http://kavita:5000/api/opds/K" $PY -c "import app.main; assert app.main.app" ; chk $? "import app.main:app"

step "docker static validation"
$PY tools/../tests/validate_docker.py >/dev/null 2>&1; chk $? "validate_docker"

step "no-JS / no-Grid / no external asset in templates+css"
if grep -rniE "<script|javascript:|onclick|onload|fetch\(|xmlhttprequest|display:[[:space:]]*grid|grid-template|@font-face|https?://" app/templates app/static >/tmp/rs_js.txt 2>&1; then
  echo "POSSIBLE VIOLATIONS:"; cat /tmp/rs_js.txt; fail=1; else echo "ok: clean"; fi

step "apiKey must not appear in any rendered body (integration grep)"
$PY - <<'PYEOF'
import re, httpx, sys
from app.config import load_config
from app.main import create_app
from app.kavita import KavitaClient
from app.ids import IdCodec
from app.main import FeedCache
from contextlib import asynccontextmanager
from fastapi.testclient import TestClient
import pathlib
FIX = pathlib.Path("tests/fixtures")
ROOT = (FIX/"opds_root.xml").read_text(); ACQ=(FIX/"opds_acquisition.xml").read_text()
cfg = load_config({"KAVITA_OPDS_URL":"http://kavita:5000/api/opds/SECRETKEY","BRIDGE_ID_SECRET":"s"})
def h(req):
    p=req.url.path
    if p=="/api/opds/SECRETKEY": return httpx.Response(200,text=ROOT)
    if "recently-added" in p or "search" in p: return httpx.Response(200,text=ACQ)
    if "/download/" in p:
        async def g():
            yield b"BYTES"
        return httpx.Response(200,content=g())
    if "/api/image" in p:
        async def g():
            yield b"IMG"
        return httpx.Response(200,content=g(),headers={"Content-Type":"image/png"})
    return httpx.Response(404)
app=create_app(cfg)
t=httpx.MockTransport(h)
@asynccontextmanager
async def ls(a):
    c=httpx.AsyncClient(transport=t,timeout=httpx.Timeout(connect=5,read=None,write=None,pool=5))
    a.state.http=c; a.state.kavita=KavitaClient(cfg,c); a.state.ids=IdCodec("s"); a.state.cache=FeedCache(0)
    yield
    await c.aclose()
app.router.lifespan_context=ls
leaks=[]
with TestClient(app) as cl:
    home=cl.get("/").text
    for path in ["/","/help"]:
        body=cl.get(path).text
        if "SECRETKEY" in body or "/api/opds/" in body: leaks.append(path)
    fid=re.search(r'/feed/([\w\-.]+)',home).group(1)
    page=cl.get(f"/feed/{fid}").text
    if "SECRETKEY" in page or "/api/opds/" in page: leaks.append("/feed root")
    for fid2 in re.findall(r'/feed/([\w\-.]+)"',page):
        sub=cl.get(f"/feed/{fid2}").text
        if "SECRETKEY" in sub or "/api/opds/" in sub: leaks.append("/feed sub")
        for bid in re.findall(r'/book/([\w\-.]+)"',sub):
            bd=cl.get(f"/book/{bid}").text
            if "SECRETKEY" in bd or "/api/opds/" in bd or "/api/image" in bd: leaks.append("/book")
print("leaks:",leaks)
sys.exit(1 if leaks else 0)
PYEOF
chk $? "no apiKey leak in rendered pages"

printf '\n========== %s ==========\n' "$([ $fail -eq 0 ] && echo 'BUILD VERIFY: PASS' || echo 'BUILD VERIFY: FAIL')"
exit $fail
