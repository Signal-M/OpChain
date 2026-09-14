"""链路配置加载：主链路 + 子链。"""
import json
import os


def load_chain(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_subchains(dirpath):
    subs = {}
    if not os.path.isdir(dirpath):
        return subs
    for fn in os.listdir(dirpath):
        if fn.endswith("_subchain.json"):
            with open(os.path.join(dirpath, fn), encoding="utf-8") as f:
                c = json.load(f)
                subs[c["name"]] = c
    return subs
