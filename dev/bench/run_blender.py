"""Drive the Blender reference render for cached packages."""
from __future__ import annotations
import argparse, json, os, subprocess, sys
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "plugin", "kitbridge"))
sys.path.insert(0, os.path.join(ROOT, "dev"))
from cli import _CoreSession
from kitbridge.core import blender, paths

BENCH_HDR_BASE_ID = "2f62aff5-3a19-43e1-9c6b-f52a898bf676"  # "Dramatic Twilight Sunset Landscape"


def bench_hdr_package():
    """Cached package of the bench HDRI (any resolution); c4d_render.py uses the same one."""
    root = paths.cache_root()
    for name in sorted(os.listdir(root)):
        if name.startswith(BENCH_HDR_BASE_ID) and os.path.isfile(os.path.join(root, name, "kitbridge.json")):
            return os.path.join(root, name)
    raise SystemExit("Bench HDRI not cached: py -3 dev/cli.py get %s --type hdr --res 1024" % BENCH_HDR_BASE_ID)


def bench_hdr_file():
    pkg = bench_hdr_package()
    rel = json.load(open(os.path.join(pkg, "kitbridge.json"), encoding="utf-8"))["hdr"]
    return os.path.join(pkg, *rel.split("/"))


def _source(pkg):
    src = os.path.join(pkg, "source")
    return next((os.path.join(src, f) for f in os.listdir(src) if f.lower().endswith(".blend")), "")
def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--assets", default=os.path.join(os.path.dirname(__file__),"assets.json"))
    ap.add_argument("--limit",type=int,default=0); ap.add_argument("--force",action="store_true"); ap.add_argument("--timeout",type=float,default=1800)
    a=ap.parse_args(); entries=json.load(open(a.assets,encoding="utf-8"))[:a.limit or None]
    bench=os.path.abspath(os.path.join(paths.cache_root(), "..", "kitbridge_bench")); os.makedirs(bench,exist_ok=True)
    script=os.path.join(os.path.dirname(__file__),"blender_render.py")
    with _CoreSession("bench") as s:
        found=blender.find_blender_with_version(client=s.client,publish=False)
    if not found: raise SystemExit("Blender 4.2+ not found")
    exe=found[0]
    hdr=bench_hdr_file()
    for e in entries:
        pkg=paths.find_asset_cache_dir(e["base_id"],"512"); outdir=os.path.join(bench,e["base_id"]); os.makedirs(outdir,exist_ok=True)
        out=os.path.join(outdir,"ref.png"); source=_source(pkg) if pkg else ""
        rec={"base_id":e["base_id"],"out":out}
        try:
            if not source: raise RuntimeError("cached package has no source .blend")
            if not a.force and os.path.isfile(out) and os.path.getmtime(out)>=os.path.getmtime(source): rec["skipped"]=True
            else:
                params=os.path.join(outdir,"blender_params.json")
                json.dump({"out":out,"package":pkg,"geometry":os.path.join(pkg,"geometry.fbx"),"hdr":hdr},open(params,"w"))
                p=subprocess.run([exe,"-b","--factory-startup",source,"--python",script,"--",params],
                                 timeout=a.timeout,cwd=ROOT,capture_output=True,text=True)
                if p.returncode: raise RuntimeError((p.stderr or p.stdout)[-2000:])
                if not os.path.isfile(out):
                    raise RuntimeError((p.stderr or p.stdout)[-4000:] or "Blender exited without ref.png")
                rec["stdout"]=p.stdout.splitlines()[-1] if p.stdout else ""
        except Exception as exc: rec["error"]=str(exc)
        print(json.dumps(rec))
if __name__=="__main__": main()
