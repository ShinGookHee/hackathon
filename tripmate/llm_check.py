"""LLM connection check: python -m tripmate.llm_check"""
import os

from tripmate.llm import MODEL

import anthropic

print("base_url:", os.environ.get("ANTHROPIC_BASE_URL") or "(기본값)")
print("model   :", MODEL)
print("key     :", "설정됨" if os.environ.get("ANTHROPIC_API_KEY") else "없음")
try:
    resp = anthropic.Anthropic(timeout=20, max_retries=0).messages.create(
        model=MODEL, max_tokens=50, messages=[{"role": "user", "content": "OK라고만 답하세요."}]
    )
    print("결과    :", "".join(b.text for b in resp.content if b.type == "text"))
except anthropic.APIStatusError as e:
    print("결과    : 실패", e.status_code, type(e).__name__)
except anthropic.APIError as e:
    print("결과    : 실패", type(e).__name__)
