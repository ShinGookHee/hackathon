"""Rule-based replanning engine.

Every candidate plan is simulated twice: with the fastest ("best") and the
slowest ("worst") travel estimates. Hard constraints are judged on the worst
track, so a plan is only called safe if it holds even when estimates go badly.
"""
import copy
import itertools

TRACKS = ("best", "worst")
IDX = {"best": 0, "worst": 1}
USER = "입력값"


def to_min(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def fmt(m):
    m = int(round(m))
    return f"{m // 60:02d}:{m % 60:02d}"


def fmt_range(lo, hi):
    return fmt(lo) if round(lo) == round(hi) else f"{fmt(lo)}~{fmt(hi)}"


def won(n):
    return f"{int(n):,}원"


def won_range(lo, hi):
    return won(lo) if lo == hi else f"{won(lo)}~{won(hi)}"


# Final consonant of how the last character is read aloud: (has_batchim, is_rieul).
# Latin letters and digits use their Korean names (L 엘, M 엠, 1 일, 3 삼 ...).
LETTER_FINAL = {"L": (True, True), "R": (True, True), "M": (True, False), "N": (True, False)}
DIGIT_FINAL = {"0": (True, False), "1": (True, True), "3": (True, False), "6": (True, False),
               "7": (True, True), "8": (True, True)}


def final_consonant(word):
    w = word.rstrip(" )]'\"")
    if not w:
        return False, False
    c = w[-1]
    if "가" <= c <= "힣":
        jong = (ord(c) - 0xAC00) % 28
        return jong != 0, jong == 8
    if c.isascii() and c.isalpha():
        return LETTER_FINAL.get(c.upper(), (False, False))
    return DIGIT_FINAL.get(c, (False, False))


def particle(word, pair):
    """Pick the particle for `word`: pair is '을/를', '이/가', '은/는', '과/와' or '으로/로'."""
    with_final, without = pair.split("/")
    has, rieul = final_consonant(word)
    if pair == "으로/로":
        return without if not has or rieul else with_final
    return with_final if has else without


def josa(word, pair):
    return f"{word}{particle(word, pair)}"


def get_path(obj, path):
    for key in path.split("."):
        if isinstance(obj, list):
            key = int(key)
        elif not isinstance(obj, dict) or key not in obj:
            return None
        obj = obj[key]
    return obj


class Planner:
    def __init__(self, scenario, baseline=None):
        self.s = scenario
        self.baseline = baseline
        self.stops = scenario["stops"]
        self.stop_by_id = {s["id"]: s for s in self.stops}
        self.legs = {(l["from"], l["to"]): l for l in scenario["legs"]}
        self.pref = scenario["preferences"]
        self.weather = scenario["weather"]
        self.p_rain = self.weather["rain_prob"] / 100

    # ---- evidence tagging -------------------------------------------------
    def src(self, sources, *paths):
        """Return the D0x sources, plus a user-input tag if any value at
        `paths` differs from the baseline scenario."""
        out = list(sources)
        if self.baseline is not None:
            for p in paths:
                if get_path(self.s, p) != get_path(self.baseline, p):
                    out.append(USER)
                    break
        return sorted(set(out), key=lambda x: (x == USER, x))

    def stop_path(self, sid, field):
        i = next(i for i, s in enumerate(self.stops) if s["id"] == sid)
        return f"stops.{i}.{field}"

    def leg_path(self, a, b, field):
        i = next(i for i, l in enumerate(self.s["legs"]) if (l["from"], l["to"]) == (a, b))
        return f"legs.{i}.{field}"

    # ---- travel model -----------------------------------------------------
    def leg_duration(self, a, b, mode, depart, track):
        leg = self.legs[(a, b)]
        k = IDX[track]
        sources = list(leg.get("sources", []))
        disrupted = False
        transit = leg["transit"][k]
        taxi = (max(5, leg["transit"][0] - self.s["taxi"]["faster_by"][1]), leg["transit"][1])[k]
        worst_arrive = depart + leg["transit"][1]
        for d in self.s.get("disruptions", []):
            # apply when any part of the trip [depart, worst arrival] falls in the window
            if (d["from"], d["to"]) == (a, b) and depart < to_min(d["window"][1]) and worst_arrive > to_min(d["window"][0]):
                disrupted = True
                sources += d["sources"]
                transit += d["transit_extra"][k]
                if "taxi_during" in d:
                    taxi = d["taxi_during"][k]
        if mode == "taxi":
            sources += self.s["taxi"]["sources"]
        return (taxi if mode == "taxi" else transit), disrupted, sources

    def aim_for(self, stop):
        """Time we want to *arrive* at a stop, and how much buffer to keep."""
        if not stop or stop.get("constraint") != "hard":
            return None
        if "entry_deadline" in stop:
            return to_min(stop["entry_deadline"]) - self.pref["target_buffer_min"]
        if "entry_window" in stop:
            return to_min(stop["entry_window"][0])
        return None

    # ---- candidates -------------------------------------------------------
    def candidates(self):
        flex = [s for s in self.stops if s.get("alternatives")]
        options = []
        for s in flex:
            opts = [("full", s["stay"])]
            alt = s["alternatives"]
            if "short_stay" in alt and alt["short_stay"] < s["stay"]:
                opts.append(("short", alt["short_stay"]))
            if "indoor_substitute" in alt:
                opts.append(("indoor", alt["indoor_substitute"]["stay"]))
            options.append([(s["id"],) + o for o in opts])
        taxi_legs = [k for k in self.legs if any((d["from"], d["to"]) == k for d in self.s.get("disruptions", []))]
        mode_opts = [[(k, "transit"), (k, "taxi")] for k in taxi_legs]
        out = []
        for combo in itertools.product(*options, *mode_opts):
            stop_choice = {c[0]: (c[1], c[2]) for c in combo if len(c) == 3}
            modes = {c[0]: c[1] for c in combo if len(c) == 2}
            out.append({"stop_choice": stop_choice, "modes": modes})
        return out

    def label(self, cand):
        parts = []
        for sid, (kind, stay) in cand["stop_choice"].items():
            st = self.stop_by_id[sid]
            if kind == "full":
                parts.append(f"{st['name']} 원안 유지({stay}분)")
            elif kind == "short":
                parts.append(f"{st['name']} {stay}분으로 축소")
            else:
                parts.append(f"{st['name']} → {st['alternatives']['indoor_substitute']['name']} 대체")
        for (a, b), mode in cand["modes"].items():
            na, nb = self.stop_by_id[a]["name"].split()[0], self.stop_by_id[b]["name"].split()[0]
            parts.append(f"{na}→{nb} {'택시' if mode == 'taxi' else '대중교통'}")
        return " + ".join(parts)

    # ---- simulation -------------------------------------------------------
    def simulate(self, cand, track):
        k = IDX[track]
        t = to_min(self.s["start"]["time"])
        prev = "START"
        rows, checks = [], []
        outdoor = []
        for i, stop in enumerate(self.stops):
            sid = stop["id"]
            mode = cand["modes"].get((prev, sid), "transit")
            dur, disrupted, lsrc = self.leg_duration(prev, sid, mode, t, track)
            rows.append({"type": "leg", "from": prev, "to": sid, "mode": mode, "depart": t,
                         "duration": dur, "disrupted": disrupted, "sources": lsrc})
            arrive = t + dur
            begin = arrive
            if "entry_window" in stop:
                w0, w1 = map(to_min, stop["entry_window"])
                begin = max(arrive, w0)
                checks.append({"stop": sid, "kind": "entry_window", "limit": w1, "arrive": arrive,
                               "ok": arrive <= w1, "hard": stop["constraint"] == "hard"})
            if "last_entry" in stop:
                le = to_min(stop["last_entry"])
                checks.append({"stop": sid, "kind": "last_entry", "limit": le, "arrive": arrive,
                               "ok": arrive <= le, "hard": stop["constraint"] == "hard"})
            if "entry_deadline" in stop:
                dl = to_min(stop["entry_deadline"])
                checks.append({"stop": sid, "kind": "entry_deadline", "limit": dl, "arrive": arrive,
                               "ok": arrive <= dl, "hard": stop["constraint"] == "hard", "buffer": dl - arrive})

            kind, stay = cand["stop_choice"].get(sid, ("full", stop.get("stay", 0)))
            name = stop["name"]
            indoor = stop.get("indoor", True)
            if kind == "indoor":
                name = stop["alternatives"]["indoor_substitute"]["name"]
                indoor = True
            planned = stay
            nxt = self.stops[i + 1] if i + 1 < len(self.stops) else None
            aim = self.aim_for(nxt)
            if nxt and aim is not None and stay:
                # leave early enough that the *worst* next leg still arrives on time
                cap = aim - self.legs[(sid, nxt["id"])]["transit"][1]
                for _ in range(2):
                    nmode = cand["modes"].get((sid, nxt["id"]), "transit")
                    worst, _, _ = self.leg_duration(sid, nxt["id"], nmode, cap, "worst")
                    cap = aim - worst
                floor = 0 if stop.get("flex_stay") else stop.get("min_stay", 0)
                if begin + stay > cap:
                    stay = max(floor, cap - begin)
            depart = begin + stay
            rows.append({"type": "stop", "id": sid, "name": name, "arrive": arrive, "begin": begin,
                         "end": depart, "stay": stay, "planned_stay": planned, "kind": kind,
                         "indoor": indoor, "constraint": stop["constraint"]})
            if not indoor and stay > 0:
                outdoor.append((sid, begin, depart))
            if "event_start" in stop:
                depart = max(depart, to_min(stop["event_start"]))
            t = depart
            prev = sid
        return {"rows": rows, "checks": checks, "outdoor": outdoor}

    # ---- evaluation -------------------------------------------------------
    def evaluate(self, cand):
        sims = {tr: self.simulate(cand, tr) for tr in TRACKS}
        pen = []   # (points, text, sources)
        hard_fail_best = hard_fail_worst = False

        def add(points, text, sources, code):
            pen.append({"code": code, "points": round(points, 1), "text": text, "sources": sources})

        for cb, cw in zip(sims["best"]["checks"], sims["worst"]["checks"]):
            st = self.stop_by_id[cw["stop"]]
            field = {"entry_window": "entry_window", "last_entry": "last_entry", "entry_deadline": "entry_deadline"}[cw["kind"]]
            srcs = self.src(st["sources"], self.stop_path(st["id"], field))
            if cw["hard"]:
                if not cb["ok"]:
                    hard_fail_best = True
                    add(200, f"{st['name']} 확정 조건({fmt(cw['limit'])})을 가장 빠른 이동에서도 지킬 수 없음", srcs, f"hard:{st['id']}")
                elif not cw["ok"]:
                    hard_fail_worst = True
                    add(80, f"이동이 늦어지면 {st['name']} 확정 조건({fmt(cw['limit'])})을 넘길 수 있음", srcs, f"hard_risk:{st['id']}")
                if cw["kind"] == "entry_deadline" and cw["ok"]:
                    tb = self.pref["target_buffer_min"]
                    if cw["buffer"] < tb:
                        add((tb - cw["buffer"]) * 1.5, f"{st['name']} 입장 여유가 최악 기준 {cw['buffer']}분으로 목표({tb}분)보다 적음",
                            self.src(srcs + ["D07"], "preferences.target_buffer_min"), f"buffer:{st['id']}")
            else:
                if not cb["ok"]:
                    add(30, f"{st['name']} 입장 마감({fmt(cw['limit'])}) 전에 도착하지 못함", srcs, f"entry:{st['id']}")
                elif not cw["ok"]:
                    add(15, f"이동이 늦어지면 {st['name']} 입장 마감({fmt(cw['limit'])})을 넘길 수 있음", srcs, f"entry:{st['id']}")

        for r in sims["worst"]["rows"]:
            if r["type"] == "stop" and self.stop_by_id[r["id"]].get("flex_stay") and r["stay"] < self.stop_by_id[r["id"]].get("min_stay", 0):
                add(10, f"최악의 경우 {r['name']} 체류가 {int(r['stay'])}분으로 줄어듦", self.stop_by_id[r["id"]]["sources"], f"stay_cut:{r['id']}")

        # weather exposure
        p = self.p_rain
        rw = list(map(to_min, self.weather["rain_window"]))
        wsrc = self.src(self.weather["sources"], "weather.rain_prob", "weather.rain_window")
        overlap = 0
        for tr in TRACKS:
            for sid, a, b in sims[tr]["outdoor"]:
                overlap = max(overlap, max(0, min(b, rw[1]) - max(a, rw[0])))
        outdoor_len = max((b - a for _, a, b in sims["worst"]["outdoor"]), default=0)
        if overlap:
            add(overlap * 0.25 * p * p, f"강수 예보 시간대({self.weather['rain_window'][0]}~{self.weather['rain_window'][1]}, 확률 {self.weather['rain_prob']}%)에 야외 체류 최대 {int(overlap)}분", wsrc, "rain_exposure")
            limit = self.pref["max_outdoor_in_rain_min"]
            if self.weather["rain_prob"] >= self.pref["rain_prob_threshold"] and outdoor_len > limit:
                add(20, f"비 가능성이 높은데({self.weather['rain_prob']}%) 연속 야외 체류 {int(outdoor_len)}분 → 일행 기준 {limit}분 초과",
                    self.src(wsrc + ["D07"], "preferences.max_outdoor_in_rain_min", "preferences.rain_prob_threshold"), "rain_limit")

        # preference for the outdoor stop
        for sid, (kind, stay) in cand["stop_choice"].items():
            st = self.stop_by_id[sid]
            if kind == "short":
                add((st["stay"] - stay) / st["stay"] * 10 * (1 - p), f"{st['name']} 체류 축소 ({st['stay']}→{stay}분)", st["sources"], f"pref:{sid}")
            elif kind == "indoor":
                add(10 * (1 - p) + 2, f"{st['name']} 방문을 실내로 대체 (가고 싶어 한 일행 {st.get('wanted_by', 1)}명)", ["D07"], f"pref:{sid}")

        # transport and budget
        party = self.s["meta"]["party_size"]
        fare = self.pref["transit_fare_per_person_krw"]
        taxi_rides = sum(1 for r in sims["worst"]["rows"] if r["type"] == "leg" and r["mode"] == "taxi")
        transit_legs = sum(1 for r in sims["worst"]["rows"] if r["type"] == "leg" and r["mode"] == "transit")
        tf = self.s["taxi"]["fare_per_ride_krw"]
        cost = [transit_legs * party * fare + taxi_rides * tf[0], transit_legs * party * fare + taxi_rides * tf[1]]
        budget = self.pref["budget_krw"]
        bsrc = self.src(["D07"], "preferences.budget_krw", "taxi.fare_per_ride_krw", "preferences.transit_fare_per_person_krw")
        if taxi_rides:
            add(2 * taxi_rides + 10 * cost[1] / budget * 0.4, f"택시 {taxi_rides}회 이용 (추가 비용)", bsrc, "taxi_cost")
        if taxi_rides > self.pref["max_taxi_rides"]:
            add(30 * (taxi_rides - self.pref["max_taxi_rides"]), f"택시 {taxi_rides}회로 일행 기준({self.pref['max_taxi_rides']}회) 초과", bsrc, "taxi_limit")
        if cost[1] > budget:
            add(25 if cost[0] <= budget else 50, f"교통비 추정 {won_range(*cost)}이 예산 {won(budget)}을 넘을 수 있음", bsrc, "budget")

        violations = []
        for cb, cw in zip(sims["best"]["checks"], sims["worst"]["checks"]):
            if cw["hard"] and not cw["ok"]:
                violations.append({"stop": cw["stop"], "name": self.stop_by_id[cw["stop"]]["name"], "limit": fmt(cw["limit"]),
                                   "over_best": max(0, cb["arrive"] - cb["limit"]), "over_worst": cw["arrive"] - cw["limit"]})

        c_check = next((c for c in sims["worst"]["checks"] if c["kind"] == "entry_deadline"), None)
        c_best = next((c for c in sims["best"]["checks"] if c["kind"] == "entry_deadline"), None)
        return {
            "label": self.label(cand),
            "cand": cand,
            "sims": sims,
            "penalties": sorted(pen, key=lambda x: -x["points"]),
            "penalty": round(sum(x["points"] for x in pen), 1),
            "violations": violations,
            "feasible": not hard_fail_best,
            "safe": not hard_fail_best and not hard_fail_worst,
            "metrics": {
                "buffer_worst": c_check["buffer"] if c_check else None,
                "buffer_best": c_best["buffer"] if c_best else None,
                "rain_overlap_min": int(overlap),
                "taxi_rides": taxi_rides,
                "cost_krw": cost,
            },
        }

    # ---- presentation -----------------------------------------------------
    def timeline(self, ev):
        b, w = ev["sims"]["best"]["rows"], ev["sims"]["worst"]["rows"]
        out = [{"time": self.s["start"]["time"], "title": self.s["start"]["name"], "type": "anchor",
                "constraint": "hard", "sources": self.s["start"]["sources"]}]
        for rb, rw in zip(b, w):
            if rb["type"] == "leg":
                to = self.stop_by_id[rb["to"]]["name"]
                mode = "택시" if rb["mode"] == "taxi" else "대중교통"
                item = {"type": "leg", "time": fmt_range(rb["depart"], rw["depart"]),
                        "title": f"{mode} 이동 → {to}", "detail": f"{rb['duration']}~{rw['duration']}분 (추정)",
                        "mode": rb["mode"], "constraint": "flex", "sources": sorted(set(rb["sources"] + rw["sources"]))}
                if rb["disrupted"] or rw["disrupted"]:
                    item["flags"] = ["선로 점검 지연 구간 (지연폭 불확실)" if rb["mode"] == "transit" else "점검 시간대 택시 (도로 상황 불확실)"]
                out.append(item)
            else:
                st = self.stop_by_id[rb["id"]]
                flags = []
                if st.get("notes") and rb["kind"] != "indoor":
                    flags.append(st["notes"])
                if not rw["indoor"]:
                    flags.append(f"야외 · 강수확률 {self.weather['rain_prob']}% (예보)")
                if rw["stay"] < rw["planned_stay"]:
                    flags.append(f"늦어지면 체류 {int(rw['stay'])}분으로 단축")
                if st.get("outdoor_deck") and self.weather["wind_ms"] >= self.pref["deck_wind_threshold_ms"]:
                    flags.append("야외 데크 폐쇄 가능 (미확정)")
                detail = []
                for key, lbl in (("entry_window", "입장"), ("last_entry", "입장 마감"), ("entry_deadline", "입장 마감")):
                    if key in st:
                        v = st[key]
                        detail.append(f"{lbl} {v[0]}~{v[1]}" if isinstance(v, list) else f"{lbl} {v}")
                if "event_start" in st:
                    detail.append(f"시작 {st['event_start']}")
                else:
                    detail.append(f"체류 {int(rw['stay'])}~{int(rb['stay'])}분")
                srcs = st["sources"] + (self.weather["sources"] if not rw["indoor"] or rb["kind"] == "indoor" else [])
                out.append({"type": "stop", "time": fmt_range(rb["begin"], rw["begin"]),
                            "end": fmt_range(rb["end"], rw["end"]) if "event_start" not in st else None,
                            "arrive": fmt_range(rb["arrive"], rw["arrive"]),
                            "title": rb["name"], "detail": " · ".join(detail),
                            "constraint": "hard" if st["constraint"] == "hard" else "flex",
                            "changed": rb["kind"] != "full" or rw["stay"] < rw["planned_stay"],
                            "flags": flags, "sources": sorted(set(srcs))})
        return out

    def plan(self):
        evs = [self.evaluate(c) for c in self.candidates()]
        # IDs follow generation order (fixed per scenario structure), not rank,
        # so the same option keeps the same letter when inputs change.
        for i, e in enumerate(evs):
            e["id"] = chr(ord("A") + i)
        # when no plan is safe, prefer the one that breaks fewer / smaller hard constraints
        ranked = sorted(evs, key=lambda e: (not e["feasible"], not e["safe"], len(e["violations"]),
                                            sum(v["over_worst"] for v in e["violations"]), e["penalty"]))
        for i, e in enumerate(ranked, 1):
            e["rank"] = i
        best = ranked[0]
        original = next(e for e in evs if all(k == "full" for k, _ in e["cand"]["stop_choice"].values())
                        and all(m == "transit" for m in e["cand"]["modes"].values()))
        return {
            "title": self.s["meta"]["title"],
            "recommended": best["id"],
            "status": "safe" if best["safe"] else ("risky" if best["feasible"] else "infeasible"),
            "candidates": [self.public(e, e is original) for e in evs],
            "constraints": self.constraint_table(),
            "reasons": self.reasons(best, ranked, original),
            "uncertainties": self.uncertainties(best),
            "checks": self.checklist(best),
            "documents": self.s.get("documents", {}),
        }

    def public(self, e, is_original):
        return {"id": e["id"], "rank": e["rank"], "label": e["label"], "is_original": is_original, "safe": e["safe"],
                "feasible": e["feasible"], "violations": e["violations"], "penalty": e["penalty"], "metrics": e["metrics"],
                "penalties": e["penalties"], "timeline": self.timeline(e)}

    def constraint_table(self):
        rows = []
        for st in self.stops:
            if st["constraint"] == "hard":
                why = "예약·입장 규칙이 정해진 확정 조건"
            elif st.get("indoor") is False:
                why = "날씨에 따라 축소·대체 가능 (일행 합의)"
            else:
                why = "당일권, 시간 조정 가능"
            rule = []
            for key, lbl in (("entry_window", "입장"), ("last_entry", "입장 마감"), ("entry_deadline", "입장 마감")):
                if key in st:
                    v = st[key]
                    rule.append(f"{lbl} {v[0]}~{v[1]}" if isinstance(v, list) else f"{lbl} {v}")
            rows.append({"name": st["name"], "type": "고정" if st["constraint"] == "hard" else "변경 가능",
                         "rule": ", ".join(rule) or "-", "why": why, "sources": st["sources"]})
        rows.append({"name": "이동수단", "type": "변경 가능", "rule": f"택시 최대 {self.pref['max_taxi_rides']}회, 예산 {won(self.pref['budget_krw'])}",
                     "why": "상황에 따라 선택", "sources": self.src(["D07", "D08"], "preferences.budget_krw", "preferences.max_taxi_rides")})
        return rows

    def reasons(self, best, evs, original):
        out = []
        m = best["metrics"]
        hard = [s for s in self.stops if s["constraint"] == "hard"]
        c = next((s for s in hard if "entry_deadline" in s), None)
        if c and m["buffer_worst"] is not None:
            srcs = self.src(c["sources"] + ["D03"] + (["D06"] if any(r.get("disrupted") for r in best["sims"]["worst"]["rows"]) else []),
                            self.stop_path(c["id"], "entry_deadline"), "disruptions", "legs")
            if m["buffer_worst"] >= 0:
                out.append({"text": f"{c['name']} 입장 마감 {c['entry_deadline']}: 이동이 가장 늦어지는 경우에도 {m['buffer_worst']}분 여유가 남습니다 (빠르면 {m['buffer_best']}분).", "sources": srcs})
            else:
                out.append({"text": f"{c['name']} 입장 마감 {c['entry_deadline']}: 이동이 가장 늦어지면 {-m['buffer_worst']}분 늦을 수 있습니다. 현장 이동 상황을 보며 앞 일정을 더 줄여야 합니다.", "sources": srcs})
        other_hard = [s for s in hard if s is not c]
        if other_hard:
            ok = all(ch["ok"] for ch in best["sims"]["worst"]["checks"] if ch["stop"] in {s["id"] for s in other_hard})
            names = ", ".join(s["name"] for s in other_hard)
            out.append({"text": f"{names} 예약 시간은 {'최악의 이동시간에도 지킬 수 있어 그대로 유지합니다' if ok else '최악의 경우 지키기 어려울 수 있습니다'}.",
                        "sources": sorted({x for s in other_hard for x in s["sources"]} | {"D03"})})
        for sid, (kind, stay) in best["cand"]["stop_choice"].items():
            st = self.stop_by_id[sid]
            if kind == "full":
                alts = [e for e in evs if e["cand"]["modes"] == best["cand"]["modes"] and e["cand"]["stop_choice"][sid][0] != "full"]
                head = f"{josa(st['name'], '을/를')} 원안({stay}분)대로 유지합니다."
                out.append(self.diff_reason(head, best, alts[0] if alts else None, "줄이거나 대체하면"))
            else:
                alt = self.find(evs, {**best["cand"]["stop_choice"], sid: ("full", st["stay"])}, best["cand"]["modes"])
                verb = f"{stay}분으로 줄인" if kind == "short" else "실내 카페로 대체한"
                out.append(self.diff_reason(f"{josa(st['name'], '을/를')} {verb} 이유:", best, alt, f"원안({st['stay']}분)대로 가면"))
        for leg, mode in best["cand"]["modes"].items():
            na, nb = self.stop_by_id[leg[0]]["name"], self.stop_by_id[leg[1]]["name"]
            alt = self.find(evs, best["cand"]["stop_choice"], {**best["cand"]["modes"], leg: "transit" if mode == "taxi" else "taxi"})
            lo, hi = best["metrics"]["cost_krw"]
            disrupted = any(r["type"] == "leg" and (r["from"], r["to"]) == leg and r["disrupted"]
                            for e in (best, alt) if e for r in e["sims"]["worst"]["rows"])
            extra = self.src(["D06"], "disruptions") if disrupted else []
            if mode == "taxi":
                head = f"{na}→{nb} 구간은 택시를 1회 이용합니다 (추정 교통비 {won_range(lo, hi)})."
                out.append(self.diff_reason(head, best, alt, "대중교통으로 가면", extra))
            else:
                head = f"{na}→{nb} 구간은 대중교통으로 이동합니다 (추정 교통비 {won_range(lo, hi)})."
                out.append(self.diff_reason(head, best, alt, "택시를 타면", extra))
        if original is not best:
            gains, _ = self.penalty_diff(best, original)
            if gains:
                out.append({"text": "원안 그대로 진행할 때 걸리는 점: " + " / ".join(p["text"] for p in gains[:3]),
                            "sources": sorted({s for p in gains[:3] for s in p["sources"]})})
        runner = next((e for e in evs if e is not best and e["feasible"]), None)
        if runner:
            gains, _ = self.penalty_diff(best, runner)
            diff = " / ".join(p["text"] for p in gains[:2]) or "차이가 작음"
            out.append({"text": f"차선안 {runner['id']}({runner['label']}){particle(runner['id'], '은/는')} '{diff}' 부담이 있어 추천안보다 뒤에 둡니다.",
                        "sources": sorted({s for p in gains[:2] for s in p["sources"]}) or ["D08"]})
        return out

    @staticmethod
    def find(evs, stop_choice, modes):
        return next((e for e in evs if e["cand"]["stop_choice"] == stop_choice and e["cand"]["modes"] == modes), None)

    @staticmethod
    def penalty_diff(best, alt):
        """Penalties `alt` has beyond `best` (what best avoids), and penalties
        `best` has beyond `alt` (what best accepts), keyed by penalty code."""
        bp = {p["code"]: p for p in best["penalties"]}
        ap = {p["code"]: p for p in alt["penalties"]}
        gains = [p for c, p in ap.items() if p["points"] - bp.get(c, {"points": 0})["points"] >= 0.5]
        costs = [p for c, p in bp.items() if p["points"] - ap.get(c, {"points": 0})["points"] >= 0.5]
        return sorted(gains, key=lambda p: -p["points"]), sorted(costs, key=lambda p: -p["points"])

    def diff_reason(self, head, best, alt, alt_phrase, extra_sources=()):
        """Build a reason sentence from the actual penalty difference to `alt`."""
        if alt is None:
            return {"text": head, "sources": ["D08"]}
        gains, costs = self.penalty_diff(best, alt)
        parts = [head]
        if gains:
            parts.append(f"{alt_phrase}({alt['id']}안): " + " / ".join(p["text"] for p in gains[:2]) + ".")
        if costs:
            parts.append("대신 감수하는 점: " + " / ".join(p["text"] for p in costs[:2]) + ".")
        srcs = {s for p in gains[:2] + costs[:2] for s in p["sources"]} | set(extra_sources)
        return {"text": " ".join(parts), "sources": sorted(srcs or {"D08"}, key=lambda x: (x in (USER, "가정"), x))}

    def uncertainties(self, best):
        w, prev = self.weather, self.weather.get("previous")
        wsrc = self.src(w["sources"], "weather.rain_prob", "weather.rain_window", "weather.wind_ms")
        out = []
        txt = f"강수확률 {w['rain_prob']}%({w['rain_window'][0]}~{w['rain_window'][1]})는 예보입니다. 비가 반드시 온다는 뜻이 아니며, 시간대와 강도는 바뀔 수 있습니다."
        if prev and prev["rain_prob"] != w["rain_prob"]:
            txt += f" 이전 예보({prev['rain_prob']}%, {prev['rain_window'][0]}~{prev['rain_window'][1]})에서 바뀌었으므로 추가 변경 가능성도 있습니다."
        out.append({"text": txt, "level": "high" if w["rain_prob"] >= 50 else "medium", "sources": wsrc})
        for d in self.s.get("disruptions", []):
            a, b = self.stop_by_id[d["from"]]["name"], self.stop_by_id[d["to"]]["name"]
            out.append({"text": f"{a}→{b} 선로 점검 지연({d['window'][0]}~{d['window'][1]})은 약 {d['transit_extra'][0]}~{d['transit_extra'][1]}분 추가로 안내된 추정치입니다. 실제 지연은 현장 상황에 따라 다릅니다.",
                        "level": "high", "sources": self.src(d["sources"], "disruptions")})
        if best["metrics"]["taxi_rides"]:
            out.append({"text": "택시 소요시간은 실시간 교통에 따라 달라지고, 대중교통보다 빠르지 않을 수도 있습니다.", "level": "medium", "sources": ["D03", "D06"]})
        if self.s["taxi"].get("fare_source") == "assumption" or self.pref.get("transit_fare_source") == "assumption":
            out.append({"text": f"택시 요금({won(self.s['taxi']['fare_per_ride_krw'][0])}~{won(self.s['taxi']['fare_per_ride_krw'][1])}/회, 4명 1대)과 대중교통 요금({won(self.pref['transit_fare_per_person_krw'])}/인)은 자료에 없어 가정한 값입니다.",
                        "level": "medium", "sources": self.src(["가정"], "taxi.fare_per_ride_krw", "preferences.transit_fare_per_person_krw")})
        out.append({"text": "구간 이동시간은 평상시 기준 추정치이며 대기시간이 포함되지 않았습니다.", "level": "medium", "sources": ["D03"]})
        if w["wind_ms"] >= self.pref["deck_wind_threshold_ms"]:
            o = next((s for s in self.stops if s.get("outdoor_deck")), None)
            if o:
                out.append({"text": f"풍속 예보 {w['wind_ms']}m/s로 {o['name']} 야외 데크가 닫힐 수 있으나 폐쇄는 확정되지 않았습니다. 실내 전망층은 정상 운영 예정입니다.",
                            "level": "medium", "sources": self.src(["D04", "D05"], "weather.wind_ms")})
        return out

    def checklist(self, best):
        out = []
        now = to_min(self.s["meta"].get("decision_time", "00:00"))
        for st in self.stops:
            if st.get("refund_deadline"):
                rd = to_min(st["refund_deadline"])
                if now < rd:
                    out.append({"text": f"{st['refund_deadline']} 전까지 {st['name']} 방문 여부 확정 (이후 취소 시 환불 불가). 추천안은 방문 유지입니다.", "due": st["refund_deadline"], "sources": st["sources"]})
                else:
                    out.append({"text": f"{st['name']} 환불 기한({st['refund_deadline']})이 지났습니다. 취소해도 환불되지 않으므로 방문을 유지하는 편이 유리합니다.", "sources": st["sources"]})
        rows = best["sims"]["worst"]["rows"]
        for sid, (kind, _) in best["cand"]["stop_choice"].items():
            leg = next(r for r in rows if r["type"] == "leg" and r["to"] == sid)
            out.append({"text": f"{fmt(leg['depart'])}경 {josa(self.stop_by_id[leg['from']]['name'], '을/를')} 나서기 전 최신 강수 예보 확인 → "
                                f"{'비가 약하면 한강 체류를 늘리고, 강하면 바로 실내 대체' if kind != 'indoor' else '비 가능성이 크게 낮아졌으면 한강 짧은 방문 검토'}",
                        "due": fmt(leg["depart"]), "sources": self.src(self.weather["sources"], "weather.rain_prob")})
        for r in rows:
            if r["type"] == "leg" and r["disrupted"]:
                out.append({"text": f"{fmt(r['depart'])} 출발 전 {self.stop_by_id[r['from']]['name']}→{self.stop_by_id[r['to']]['name']} 실시간 운행·도로 상황 확인 ({'택시 호출 대기시간 포함' if r['mode'] == 'taxi' else '지연이 길면 택시 1회로 전환'})",
                            "due": fmt(r["depart"]), "sources": ["D06"]})
        o = next((s for s in self.stops if s.get("outdoor_deck")), None)
        if o and self.weather["wind_ms"] >= self.pref["deck_wind_threshold_ms"]:
            out.append({"text": f"{o['name']} 도착 전 야외 데크 운영 여부 확인 (실내 전망층 입장 마감 {o.get('last_entry', '-')})", "sources": ["D05"]})
        c = next((s for s in self.stops if "entry_deadline" in s), None)
        if c:
            leg = next(r for r in rows if r["type"] == "leg" and r["to"] == c["id"])
            out.append({"text": f"늦어도 {fmt(leg['depart'])}에는 {josa(c['name'], '으로/로')} 출발 (입장 마감 {c['entry_deadline']}, 티켓 환불 불가)", "due": fmt(leg["depart"]), "sources": c["sources"]})
            out.append({"text": f"공연 종료 시각과 {c['name']}→서울역 이동시간 확인 (자료에 없음, 서울역 출발 21:30)", "sources": ["D01"]})
        return out


def plan(scenario, baseline=None):
    return Planner(copy.deepcopy(scenario), baseline).plan()
