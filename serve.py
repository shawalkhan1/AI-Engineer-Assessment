"""Local Aerlink review desk. Run with .venv/Scripts/python.exe serve.py."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from aerlink.config import load_config, redact

ROOT = Path(__file__).resolve().parent
JOBS = {}
LOCK = threading.Lock()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--enable-ai", action="store_true", help="Allow paid OpenAI calls for submitted cases")
    args = parser.parse_args()
    config = load_config(require_openai_key=args.enable_ai)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        ops_port = sock.getsockname()[1]
    environment = dict(os.environ, OPS_HOST="127.0.0.1", OPS_PORT=str(ops_port), OPS_BASE_URL=f"http://127.0.0.1:{ops_port}", OPS_API_KEY=config.ops_api_key)
    # Each desk session owns an isolated mock server; never reset an existing server.
    interpreter = getattr(sys, "_base_executable", sys.executable)
    ops = subprocess.Popen([interpreter, str(ROOT / "env/ops_server.py")], cwd=ROOT, env=environment,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    origin = f"http://127.0.0.1:{args.port}"

    def work(job_id, payload):
        destination = ROOT / "artifacts" / "desk" / job_id
        inbound = ROOT / "state" / "desk" / job_id
        inbound.mkdir(parents=True)
        (inbound / "inbound.txt").write_text(payload["message"], encoding="utf-8")
        meta = {"case_id": job_id, "from": payload.get("sender", ""), "received_at": payload.get("received_at", "")}
        (inbound / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        command = [sys.executable, str(ROOT / "run.py"), "--inbound", str(inbound / "inbound.txt"),
                   "--meta", str(inbound / "meta.json"), "--output", str(destination),
                   "--journal", str(ROOT / "state/desk-journal.sqlite3"), "--run-ceiling-usd", "0.25"]
        if not args.enable_ai:
            command.append("--no-model")
        if payload.get("dry_run", True):
            command.append("--dry-run")
        try:
            result = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True, text=True, timeout=600)
            record_path = destination / (job_id + ".json")
            record = json.loads(record_path.read_text(encoding="utf-8")) if record_path.exists() else None
            check = subprocess.run([sys.executable, "tools/inspect_run.py", str(destination)], cwd=ROOT,
                                   env=environment, capture_output=True, text=True, timeout=120) if record else None
            JOBS[job_id] = {"status": "complete" if result.returncode == 0 and check and check.returncode == 0 else "failed",
                            "record": record, "audit_passed": bool(check and check.returncode == 0),
                            "details": redact(result.stdout + result.stderr + (check.stdout if check else ""), config.openai_api_key, config.ops_api_key)}
        except Exception as exc:
            JOBS[job_id] = {"status": "failed", "details": redact(str(exc), config.openai_api_key, config.ops_api_key)}
        finally:
            LOCK.release()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, code, data, content_type="application/json"):
            body = data.encode() if isinstance(data, str) else json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def allowed(self):
            return self.headers.get("Host") in {f"127.0.0.1:{args.port}", f"localhost:{args.port}"}

        def do_GET(self):
            if not self.allowed():
                return self.send(403, {"error": "Local access only"})
            path = urlsplit(self.path).path
            if path == "/":
                return self.send(200, (ROOT / "desk.html").read_text(encoding="utf-8"), "text/html")
            if path == "/health":
                return self.send(200, {"service": "aerlink-desk", "mode": "AI enabled" if args.enable_ai else "Local fallback", "operations_running": ops.poll() is None})
            if path == "/cases":
                return self.send(200, [{"id": p.parent.name, "message": p.read_text(encoding="utf-8"),
                    **json.loads((p.parent / "meta.json").read_text(encoding="utf-8"))} for p in sorted((ROOT / "cases").glob("*/inbound.txt"))])
            if path.startswith("/jobs/"):
                job = JOBS.get(path.split("/")[-1])
                return self.send(200 if job else 404, job or {"error": "Unknown run"})
            return self.send(404, {"error": "Not found"})

        def do_POST(self):
            if not self.allowed() or self.headers.get("Origin") not in {origin, f"http://localhost:{args.port}"}:
                return self.send(403, {"error": "Same-origin local requests only"})
            if self.path != "/run":
                return self.send(404, {"error": "Not found"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 100000:
                    raise ValueError("Message is too large or empty")
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict) or not isinstance(payload.get("message"), str) or not payload["message"].strip():
                    raise ValueError("Enter a passenger message")
                if any(not isinstance(payload.get(k, ""), str) for k in ("sender", "received_at")):
                    raise ValueError("Sender and received time must be text")
                if not isinstance(payload.get("dry_run", True), bool):
                    raise ValueError("Invalid preview setting")
            except (ValueError, TypeError) as exc:
                return self.send(400, {"error": str(exc)})
            if not LOCK.acquire(blocking=False):
                return self.send(409, {"error": "A case is already running. Please wait."})
            job_id = "desk-" + uuid.uuid4().hex[:12]
            JOBS[job_id] = {"status": "running"}
            threading.Thread(target=work, args=(job_id, payload), daemon=True).start()
            return self.send(202, {"id": job_id})

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Aerlink desk: {origin} ({'AI enabled' if args.enable_ai else 'local fallback'})", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        ops.terminate()
        ops.wait(timeout=10)


if __name__ == "__main__":
    main()
