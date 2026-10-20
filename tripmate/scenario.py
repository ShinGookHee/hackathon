import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "data" / "scenario_default.json"


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def apply_override(scenario, item):
    """Apply 'a.b.0.c=VALUE'; VALUE is parsed as JSON when possible."""
    path, _, raw = item.partition("=")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = raw
    keys = path.strip().split(".")
    obj = scenario
    for k in keys[:-1]:
        obj = obj[int(k)] if isinstance(obj, list) else obj[k]
    last = keys[-1]
    if isinstance(obj, list):
        obj[int(last)] = value
    else:
        obj[last] = value


def validate(scenario):
    """Return a list of human-readable problems (empty if OK)."""
    errors = []
    for key in ("start", "stops", "legs", "weather", "preferences", "taxi", "meta"):
        if key not in scenario:
            errors.append(f"'{key}' 항목이 없습니다")
    if errors:
        return errors
    ids = ["START"] + [s["id"] for s in scenario["stops"]]
    legs = {(l["from"], l["to"]) for l in scenario["legs"]}
    for a, b in zip(ids, ids[1:]):
        if (a, b) not in legs:
            errors.append(f"{a}→{b} 이동시간(legs)이 없습니다")
    for l in scenario["legs"]:
        if l["transit"][0] > l["transit"][1]:
            errors.append(f"{l['from']}→{l['to']} 이동시간 최소값이 최대값보다 큽니다")
    rp = scenario["weather"].get("rain_prob")
    if not isinstance(rp, (int, float)) or not 0 <= rp <= 100:
        errors.append("강수확률은 0~100 사이 숫자여야 합니다")
    return errors
