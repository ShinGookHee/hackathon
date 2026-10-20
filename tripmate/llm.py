"""Optional natural-language summary via Claude.

The engine's rule-based output is always the source of truth. This module only
rewrites it into a short briefing; on any failure the caller keeps the
rule-based text. Requires `pip install anthropic` and ANTHROPIC_API_KEY.

Settings come from the project-root .env, or env.example when there is no .env
(the file handed out on exam day, copied as-is). Keys: ANTHROPIC_BASE_URL,
ANTHROPIC_API_KEY, ANTHROPIC_MODEL. Both files are git-ignored. Don't `export`
them in the shell: Claude Code reads the same names.
"""
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATHS = (ROOT / ".env", ROOT / "env.example")


def load_env(paths=ENV_PATHS):
    """Minimal python-dotenv replacement: read the first file that exists; existing environment variables win."""
    path = next((p for p in paths if p.exists()), None)
    if path is None:
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_env()

MODEL = os.environ.get("ANTHROPIC_MODEL") or "claude-opus-5-5"
TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "60"))

SYSTEM = """당신은 여행 일정 변경을 돕는 어시스턴트입니다. 아래 JSON은 규칙 기반 엔진이 계산한 결과입니다.
여행자 4명에게 보낼 4~6문장 한국어 요약을 작성하세요.
- JSON에 없는 사실, 시간, 숫자를 만들지 마세요.
- 문장마다 근거 태그를 [D01]~[D08], [입력값], [가정] 형식으로 붙이세요. 태그는 JSON의 sources에서만 가져오세요.
- 강수확률, 지연시간, 데크 폐쇄 같은 예보·추정은 확정 사실처럼 쓰지 마세요("~할 수 있습니다", "예보상").
- 무엇을 유지하고 무엇을 바꾸는지, 지금 가장 먼저 확인할 것 하나를 포함하세요."""


def available():
    """True when a summary can be attempted (key set and SDK installed)."""
    import importlib.util
    return bool(os.environ.get("ANTHROPIC_API_KEY")) and importlib.util.find_spec("anthropic") is not None


def summarize(result):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {"used": False, "error": "ANTHROPIC_API_KEY가 설정되지 않아 규칙 기반 설명만 표시합니다."}
    try:
        import anthropic
    except ImportError:
        return {"used": False, "error": "anthropic 패키지가 없어 규칙 기반 설명만 표시합니다 (pip install anthropic)."}

    rec = next(c for c in result["candidates"] if c["id"] == result["recommended"])
    payload = {
        "recommended": {"label": rec["label"], "metrics": rec["metrics"], "timeline": rec["timeline"]},
        "reasons": result["reasons"],
        "uncertainties": result["uncertainties"],
        "checks": result["checks"],
    }
    try:
        # base_url and api_key are read from the environment by the SDK.
        client = anthropic.Anthropic(timeout=TIMEOUT, max_retries=1)
        resp = client.messages.create(
            model=MODEL,
            max_tokens=2000,
            system=SYSTEM,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        )
    except anthropic.APIError as e:
        return {"used": False, "error": f"AI 요약 실패 ({type(e).__name__}). 규칙 기반 설명만 표시합니다."}
    if resp.stop_reason == "refusal":
        return {"used": False, "error": "AI 요약이 거절되어 규칙 기반 설명만 표시합니다."}
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    allowed = {s for key in ("reasons", "uncertainties", "checks") for r in result[key] for s in r["sources"]}
    cited = set(re.findall(r"\[([^\]]+)\]", text))
    if not cited or not cited <= allowed | {f"D0{i}" for i in range(1, 9)}:
        return {"used": False, "error": "AI 요약에 근거 태그가 없거나 올바르지 않아 버렸습니다. 규칙 기반 설명을 참고하세요."}
    return {"used": True, "model": MODEL, "text": text}
