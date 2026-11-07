"""Fetch benchmark packages, reusing the normal Kitbridge core session."""
from __future__ import annotations
import argparse, json, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "plugin", "kitbridge"))
sys.path.insert(0, os.path.join(ROOT, "dev"))
from cli import _CoreSession, _run_search
from kitbridge.core import auth, paths, pipeline

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--assets", default=os.path.join(os.path.dirname(__file__), "assets.json"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--resolution", default="512")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    entries = json.load(open(args.assets, encoding="utf-8"))[:args.limit or None]
    with _CoreSession("bench") as session:
        for entry in entries:
            try:
                cached = paths.find_asset_cache_dir(entry["base_id"], args.resolution)
                if cached and os.path.isfile(os.path.join(cached, "kitbridge.json")) and not args.force:
                    print(json.dumps({"base_id": entry["base_id"], "path": cached, "cached": True}))
                    continue
                rows = _run_search(session, query=entry.get("query", entry["name"]),
                                   asset_type=entry["type"], page_size=50, timeout=60)
                chosen = next((r for r in rows if r.get("assetBaseId") == entry["base_id"]), None)
                if chosen is None:
                    raise RuntimeError("asset was not returned by its recorded search query")
                pkg = pipeline.fetch_asset(session.client, chosen, resolution=args.resolution,
                                           api_key=auth.get_api_key(session.client),
                                           poller=session.poller, dispatcher=session.dispatcher,
                                           convert_timeout=900)
                print(json.dumps({"base_id": entry["base_id"], "path": pkg.path, "cached": pkg.is_cached}))
            except Exception as exc:
                print(json.dumps({"base_id": entry["base_id"], "error": str(exc)}))
    return 0
if __name__ == "__main__":
    raise SystemExit(main())
