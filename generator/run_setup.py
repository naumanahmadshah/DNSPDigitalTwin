"""Setup entry point (serverless job task or local run).

Builds the network and writes every source system in its native shape into the landing volume,
then generates history. Usage:
  run_setup.py --src <dir containing generator/ and config/> --root /Volumes/<cat>/<schema>/landing [--step all|network|history]
"""
import argparse
import json
import os
import sys
import time


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--root", required=True)
    ap.add_argument("--step", default="all")
    a, _ = ap.parse_known_args(argv)
    sys.path.insert(0, os.path.join(a.src, "generator"))
    from network import Network, load_config
    import writers
    import history

    cfg_dir = os.path.join(a.src, "config")
    cfg = load_config(os.path.join(cfg_dir, "atlas.json"))
    t = time.time()
    net = Network(cfg).build()
    print("network", json.dumps(net.summary()), f"{time.time() - t:.1f}s", flush=True)
    stamp = cfg["as_of"].replace("-", "")
    out = {}
    if a.step in ("all", "network"):
        out["network"] = writers.write_all(net, a.root, stamp)
        import connections   # new connection applications, a case-management export
        out["connections"] = connections.write_connections(net, a.root, stamp)
        print("native files", json.dumps(out["network"], default=str), f"{time.time() - t:.1f}s", flush=True)
    if a.step in ("all", "history"):
        out["history"] = history.write_all(net, a.root, cfg_dir)
        print("history", json.dumps(out["history"]), f"{time.time() - t:.1f}s", flush=True)
    return out


if __name__ == "__main__":
    main()
