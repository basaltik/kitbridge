"""Reconvert every cached benchmark asset with the current exporter (dev helper).

Packages already on the current exporter version are skipped unless ``--force`` is given.
"""
import glob
import json
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path[:0] = [os.path.join(REPO, "plugin", "kitbridge")]
from kitbridge.core import pipeline  # noqa: E402


def main() -> None:
    force = "--force" in sys.argv[1:]
    cache = os.path.expanduser(os.path.join("~", "blenderkit_data", "kitbridge_cache"))
    with open(os.path.join(REPO, "dev", "bench", "assets.json"), encoding="utf-8") as fh:
        assets = json.load(fh)
    for asset in assets:
        short = asset["base_id"][:8]
        hits = sorted(glob.glob(os.path.join(cache, "**", asset["base_id"] + "_512"), recursive=True))
        if not hits:
            print(json.dumps({"base": short, "error": "not cached"}), flush=True)
            continue
        try:
            with open(os.path.join(hits[0], "kitbridge.json"), encoding="utf-8") as fh:
                done = json.load(fh).get("exporter_version", 1)
        except (OSError, ValueError):
            done = 0
        if done >= pipeline.EXPORTER_VERSION and not force:
            print(json.dumps({"base": short, "skipped": True}), flush=True)
            continue
        started = time.time()
        try:
            pkg = pipeline.reconvert(hits[0])
            print(json.dumps({"base": short, "name": asset["name"][:30],
                              "exporter": pkg.manifest.get("exporter_version"),
                              "secs": round(time.time() - started)}), flush=True)
        except Exception as exc:  # keep going; one bad asset must not stop the run
            print(json.dumps({"base": short, "error": str(exc)[:200]}), flush=True)


if __name__ == "__main__":
    main()
