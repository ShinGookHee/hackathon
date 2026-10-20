"""Command-line runner.

  python -m tripmate.cli
  python -m tripmate.cli --set weather.rain_prob=30 --set disruptions.0.transit_extra=[50,60]
  python -m tripmate.cli --input my_scenario.json --json
"""
import argparse
import json
import sys

from . import engine
from .scenario import DEFAULT_PATH, apply_override, load


def render(result):
    lines = []
    rec = next(c for c in result["candidates"] if c["id"] == result["recommended"])
    status = {"safe": "확정 조건 충족", "risky": "주의: 최악의 경우 확정 조건 위반 가능", "infeasible": "경고: 확정 조건을 지킬 수 있는 안이 없음"}[result["status"]]
    if result["status"] == "infeasible":
        lines += [f"== {result['title']} ==", f"확정 조건을 모두 지키는 안 없음 - 위반이 가장 적은 안: [{rec['id']}] {rec['label']}"]
    else:
        lines += [f"== {result['title']} ==", f"추천: [{rec['id']}] {rec['label']}  ({status})"]
    for v in rec["violations"]:
        best = f"빠르면 {v['over_best']}분" if v["over_best"] else "빠르면 지킬 수 있으나"
        lines.append(f"  ! {v['name']} {v['limit']}: {best}, 늦으면 {v['over_worst']}분 초과")
    lines.append("")
    lines.append("-- 고정 / 변경 가능 조건 --")
    for c in result["constraints"]:
        lines.append(f"  [{c['type']}] {c['name']}: {c['rule']}  ({c['why']}) {tags(c['sources'])}")
    lines += ["", "-- 추천 일정 (빠를 때~늦을 때) --"]
    for t in rec["timeline"]:
        mark = {"hard": "[고정]", "flex": "[변경가능]"}[t["constraint"]] if t["type"] != "leg" else "   →"
        end = f" ~ {t['end']}" if t.get("end") else ""
        lines.append(f"  {t['time']:>11}{end:<16} {mark} {t['title']}  {t.get('detail', '')} {tags(t['sources'])}")
        for f in t.get("flags", []):
            lines.append(f"  {'':>24}  ! {f}")
    for title, key in (("추천 이유", "reasons"), ("불확실성", "uncertainties"), ("확인할 항목", "checks")):
        lines += ["", f"-- {title} --"]
        for i, r in enumerate(result[key], 1):
            lines.append(f"  {i}. {r['text']} {tags(r['sources'])}")
    lines += ["", "-- 후보 비교 (순위순, 벌점 낮을수록 좋음) --"]
    for c in sorted(result["candidates"], key=lambda c: c["rank"]):
        m = c["metrics"]
        flag = "추천" if c["id"] == result["recommended"] else ("원안" if c["is_original"] else "")
        lines.append(f"  {c['rank']}위 [{c['id']}] {c['penalty']:>6}  여유 {m['buffer_worst']}~{m['buffer_best']}분  우중야외 {m['rain_overlap_min']}분  "
                     f"택시 {m['taxi_rides']}회  {flag:<4} {c['label']}{'' if c['safe'] else '  (위험)'}")
    if result.get("llm"):
        lines += ["", "-- AI 요약 --", "  " + result["llm"].get("text", result["llm"].get("error", ""))]
    return "\n".join(lines)


def tags(sources):
    return " ".join(f"[{s}]" for s in sources)


def main(argv=None):
    ap = argparse.ArgumentParser(description="TripMate 일정 재계획")
    ap.add_argument("--input", default=str(DEFAULT_PATH), help="시나리오 JSON 경로")
    ap.add_argument("--set", action="append", default=[], metavar="PATH=VALUE", help="값 덮어쓰기 (예: weather.rain_prob=30)")
    ap.add_argument("--json", action="store_true", help="결과를 JSON으로 출력")
    ap.add_argument("--llm", action="store_true", help="ANTHROPIC_API_KEY가 있으면 AI 요약 추가")
    args = ap.parse_args(argv)

    baseline = load(DEFAULT_PATH)
    scenario = load(args.input)
    for item in args.set:
        apply_override(scenario, item)
    result = engine.plan(scenario, baseline)
    if args.llm:
        from . import llm
        result["llm"] = llm.summarize(result)
    if args.json:
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    else:
        print(render(result))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
