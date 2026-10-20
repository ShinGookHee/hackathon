"""TripMate web server (standard library only).

  python server.py            # http://localhost:8000
  PORT=9000 python server.py
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tripmate import engine, interpret, llm
from tripmate.scenario import DEFAULT_PATH, load, validate

STATIC = Path(__file__).resolve().parent / "static"


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/default":
            self._send(200, load(DEFAULT_PATH))
        elif self.path == "/api/config":
            self._send(200, {"llm_available": llm.available(), "llm_model": llm.MODEL})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path not in ("/api/plan", "/api/interpret"):
            return self._send(404, {"error": "not found"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            scenario = body.get("scenario") or load(DEFAULT_PATH)
        except (ValueError, json.JSONDecodeError):
            return self._send(400, {"error": "JSON 형식이 올바르지 않습니다."})
        errors = validate(scenario)
        if errors:
            return self._send(400, {"error": " / ".join(errors)})
        if self.path == "/api/interpret":
            return self._send(200, interpret.interpret(body.get("text"), scenario))
        try:
            result = engine.plan(scenario, load(DEFAULT_PATH))
        except (KeyError, ValueError, TypeError, IndexError, StopIteration) as e:
            return self._send(400, {"error": f"시나리오를 처리할 수 없습니다: {type(e).__name__} {e}"})
        if body.get("use_llm"):
            result["llm"] = llm.summarize(result)
        self._send(200, result)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.command, self.path))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"TripMate running at http://localhost:{port}  (Ctrl+C to stop)")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
