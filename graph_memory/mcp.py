"""Small MCP 2026-07-28 tools-only binding. No protocol session or SDK dependency."""

import base64
import hmac
import json
import logging
import re
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pydantic import ValidationError

from . import __version__

VERSION = "2026-07-28"
LEGACY_VERSIONS = ("2025-03-26", "2025-06-18", "2025-11-25")
PREFIX = "io.modelcontextprotocol/"
MAX_BODY = 4_000_000
READ_ONLY = {
    "memory_status",
    "memory_recall",
    "memory_latest",
    "memory_render",
    "memory_evidence",
    "memory_search_entities",
}


DENIED, BUSY = -32001, -32002
LOOPBACK = {"127.0.0.1", "localhost", "[::1]"}
HOST = re.compile(r"(\[[0-9a-f:.]+\]|[^:\[\]]+)(?::\d+)?")
INSTRUCTIONS = "Use graph-memory for remembering, recall, and corrections. Background workers process saved messages."
log = logging.getLogger("graph_memory.mcp")


def failure(where, exc):
    # Type and correlation id only: arguments and exception text can carry user data.
    request = uuid.uuid4().hex
    log.error("%s failed: %s request_id=%s", where, type(exc).__name__, request)
    return request


def error(request_id, code, message, data=None):
    result = {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
    if data is not None:
        result["error"]["data"] = data
    return result


def decoded_header(value):
    if value and value.startswith("=?base64?") and value.endswith("?="):
        return base64.b64decode(value[9:-2], validate=True).decode("utf-8")
    return value


class Protocol:
    def __init__(self, service, namespace=None, read_only=False):
        self.service, self.namespace = service, namespace
        self.read_only = read_only
        self.renders = threading.BoundedSemaphore(2)
        self.tools = service.session_tools()
        self.restrict_cypher(False)

    def restrict_cypher(self, restricted=True):
        # Render Cypher runs against the whole database and is only output-filtered,
        # so counts and booleans about other namespaces leak through what gets drawn.
        # A token-bound server is a namespace boundary; local unbound use is not.
        self.cypher_restricted = restricted
        self.catalog = [
            {
                "name": name,
                "description": description,
                "inputSchema": self.input_schema(schema, name),
                "annotations": {
                    "readOnlyHint": name in READ_ONLY,
                    "destructiveHint": name in ("memory_merge", "memory_retract"),
                    # Content-addressed: repeating an ingest returns the same receipt.
                    "idempotentHint": name == "memory_ingest" or name in READ_ONLY,
                    "openWorldHint": name == "memory_ingest",
                },
            }
            for name, (schema, _, description) in sorted(self.tools.items())
            if not self.read_only or name in READ_ONLY
        ]

    def input_schema(self, schema, name=None):
        result = schema.model_json_schema()
        if name == "memory_render" and self.cypher_restricted:
            for hidden in ("cypher", "parameters"):
                result["properties"].pop(hidden, None)
        if self.namespace is None:
            return result

        def hide_scope(node):
            if isinstance(node, dict):
                if "properties" in node and "namespace" in node["properties"]:
                    node["properties"].pop("namespace")
                    node["required"] = [k for k in node.get("required", []) if k != "namespace"]
                for child in node.values():
                    hide_scope(child)
            elif isinstance(node, list):
                for child in node:
                    hide_scope(child)

        hide_scope(result)
        return result

    def dispatch(self, message, headers=None):
        try:
            return self.route(message, headers)
        except Exception as exc:
            request = failure("dispatch", exc)
            request_id = message.get("id") if isinstance(message, dict) else None
            if isinstance(message, dict) and "id" not in message:
                return 202, None
            if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
                request_id = None
            return 500, error(request_id, -32603, f"Internal error (request {request})")

    def route(self, message, headers=None):
        if (
            not isinstance(message, dict)
            or message.get("jsonrpc") != "2.0"
            or not isinstance(message.get("method"), str)
        ):
            return 400, error(None, -32600, "Expected one JSON-RPC request")
        request_id, method = message.get("id"), message["method"]
        if "id" not in message:
            return 202, None  # Notifications never get a response, known or not.
        if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
            return 400, error(None, -32600, "Request id must be a string or integer")
        params = message.get("params", {})
        if not isinstance(params, dict):
            return 400, error(request_id, -32602, "params must be an object")
        meta = params.get("_meta", {})
        if not isinstance(meta, dict):
            return 400, error(request_id, -32602, "_meta must be an object")
        # Legacy (2025-era) clients initialize but get no app session on either
        # transport: namespace and episode handles stay explicit on every tool.
        legacy = PREFIX + "protocolVersion" not in meta
        if legacy and headers is not None:
            declared = headers.get("mcp-protocol-version")
            if declared is not None and declared not in LEGACY_VERSIONS:
                return 400, error(
                    request_id,
                    -32022,
                    "Unsupported protocol version",
                    {"supported": [VERSION, *LEGACY_VERSIONS], "requested": declared},
                )
        if legacy and method == "initialize":
            requested = params.get("protocolVersion")
            return 200, {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": requested
                    if requested in LEGACY_VERSIONS
                    else LEGACY_VERSIONS[-1],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "graph-memory", "version": __version__},
                    "instructions": INSTRUCTIONS,
                },
            }
        version = meta.get(PREFIX + "protocolVersion")
        if not legacy:
            if not isinstance(version, str):
                return 400, error(request_id, -32602, "Required protocolVersion missing")
            if version != VERSION:
                return 400, error(
                    request_id,
                    -32022,
                    "Unsupported protocol version",
                    {"supported": [VERSION], "requested": version},
                )
            info = meta.get(PREFIX + "clientInfo")
            if info is not None and (
                not isinstance(info, dict)
                or not all(isinstance(info.get(k), str) for k in ("name", "version"))
            ):
                return 400, error(request_id, -32602, "Invalid clientInfo name/version")
            if not isinstance(meta.get(PREFIX + "clientCapabilities"), dict):
                return 400, error(request_id, -32602, "Required clientCapabilities object missing")
        if headers is not None and not legacy:
            expected = {"mcp-protocol-version": version, "mcp-method": method}
            if method in ("tools/call", "prompts/get", "resources/read"):
                expected["mcp-name"] = params.get("uri" if method == "resources/read" else "name")
            try:
                if any(not v or decoded_header(headers.get(k)) != v for k, v in expected.items()):
                    return 400, error(
                        request_id, -32020, "Missing or mismatched MCP routing headers"
                    )
            except (ValueError, UnicodeError):
                return 400, error(request_id, -32020, "Malformed MCP header encoding")
        if method == "server/discover":
            result = {
                "supportedVersions": [VERSION],
                "capabilities": {"tools": {}},
                "ttlMs": 300000,
                "cacheScope": "public",
                "instructions": INSTRUCTIONS,
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            if params.get("cursor"):
                return 400, error(request_id, -32602, "Unknown cursor")
            result = {"tools": self.catalog, "ttlMs": 300000, "cacheScope": "public"}
        elif method == "tools/call":
            name, arguments = params.get("name"), params.get("arguments", {})
            if not isinstance(name, str) or name not in self.tools:
                return 400, error(request_id, -32602, "Unknown tool")
            if self.read_only and name not in READ_ONLY:
                return 403, error(request_id, DENIED, "This server permits retrieval only")
            if not isinstance(arguments, dict):
                return 400, error(request_id, -32602, "Tool arguments must be an object")
            # A bound endpoint supplies its scope; explicit legacy arguments must
            # still agree, so hiding the field never weakens authorization.
            arguments = dict(arguments)
            if self.namespace is not None:
                if name == "memory_ingest":
                    if isinstance(arguments.get("transcript"), dict):
                        arguments["transcript"] = dict(arguments["transcript"])
                        arguments["transcript"].setdefault("namespace", self.namespace)
                else:
                    arguments.setdefault("namespace", self.namespace)
            ns = arguments.get("namespace")
            if name == "memory_ingest" and isinstance(arguments.get("transcript"), dict):
                ns = arguments["transcript"].get("namespace")
            if self.namespace is not None and ns != self.namespace:
                return 403, error(
                    request_id, DENIED, "Namespace is outside this server's authorized scope"
                )
            rendering = name == "memory_render"
            if rendering and self.cypher_restricted and arguments.get("cypher") is not None:
                return 403, error(
                    request_id,
                    DENIED,
                    "Custom Cypher is disabled on a token-bound server; omit cypher to render the namespace",
                )
            if rendering and not self.renders.acquire(blocking=False):
                return 503, error(request_id, BUSY, "Rendering is busy; retry shortly")
            try:
                schema, handler, _ = self.tools[name]
                output = handler(schema.model_validate(arguments))
                rendered_image = output.pop("image", None) if name == "memory_render" else None
                result = {
                    "content": [{"type": "text", "text": json.dumps(output)}],
                    "structuredContent": output,
                    "isError": False,
                }
                if rendered_image is not None:
                    result["content"].append({"type": "image", **rendered_image})
            except ValidationError as exc:
                # Omit input values, which can include private transcript content.
                detail = [
                    {"loc": e["loc"], "msg": e["msg"]} for e in exc.errors(include_input=False)
                ]
                result = {
                    "content": [{"type": "text", "text": json.dumps(detail)}],
                    "isError": True,
                }
            except Exception as exc:
                # Only the engine's own ValueError messages are written for clients;
                # subclasses (JSON, Unicode) and other errors can quote user data.
                if type(exc) is ValueError:
                    text = str(exc)
                else:
                    text = (
                        "Operation failed; durable receipts can be retried. Check service "
                        f"connectivity and model configuration. Request id: {failure(name, exc)}"
                    )
                result = {"content": [{"type": "text", "text": text}], "isError": True}
            finally:
                if rendering:
                    self.renders.release()
        else:
            return 404, error(request_id, -32601, "Method not found")
        if not legacy:
            result["resultType"] = "complete"
            result["_meta"] = {
                PREFIX + "serverInfo": {"name": "graph-memory", "version": __version__}
            }
        return 200, {"jsonrpc": "2.0", "id": request_id, "result": result}


def stdio(protocol):
    while True:
        line = sys.stdin.buffer.readline(MAX_BODY + 1)
        if not line:
            break
        if len(line) > MAX_BODY:
            response = error(None, -32600, "Request too large")
            while line and not line.endswith(b"\n"):
                line = sys.stdin.buffer.readline(MAX_BODY + 1)
        else:
            try:
                message = json.loads(line)
            except (ValueError, UnicodeError, RecursionError):
                response = error(None, -32700, "Invalid JSON")
            else:
                _, response = protocol.dispatch(message)
        if response is not None:
            print(json.dumps(response), flush=True)


def host_allowed(value, extra=()):
    value = (value or "").strip().lower()
    match = HOST.fullmatch(value)
    if not match:
        return False
    # The name is what defeats DNS rebinding. The port is not compared because a
    # published container port differs from the one this process is bound to.
    return match[1] in LOOPBACK or match[1] in extra or value in extra


def http_server(protocol, host="127.0.0.1", port=8765, token=None, origins=(), hosts=()):
    if host not in ("127.0.0.1", "::1", "localhost") and not token:
        raise ValueError("Non-loopback HTTP requires MEMORY_HTTP_TOKEN")
    if token and protocol.namespace is None:
        raise ValueError("Token-authenticated HTTP requires MEMORY_NAMESPACE to bind authorization")
    if token:
        protocol.restrict_cypher()
    credential = ("Bearer " + token).encode() if token else None
    hosts = {h.strip().lower() for h in hosts if h.strip()}
    origins = {o.strip() for o in origins if o.strip()}
    slots = threading.BoundedSemaphore(16)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(30)

        def log_message(self, *_):
            pass  # Never log request data or credentials.

        def respond(self, status, response):
            body = json.dumps(response).encode() if response is not None else b""
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if status == 405:
                self.send_header("Allow", "POST")
            if status == 503:
                self.send_header("Retry-After", "5")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.respond(405, error(None, -32600, "Use POST /mcp"))

        do_DELETE = do_PUT = do_GET

        def do_POST(self):
            if not slots.acquire(blocking=False):
                return self.respond(503, error(None, BUSY, "Server is busy; retry shortly"))
            try:
                self.handle_post()
            finally:
                slots.release()

        def handle_post(self):
            if not host_allowed(self.headers.get("Host"), hosts):
                return self.respond(403, error(None, -32600, "Host not allowed"))
            if self.path != "/mcp":
                return self.respond(404, error(None, -32600, "Unknown endpoint"))
            origin = self.headers.get("Origin")
            local = {f"http://{name}:{self.server.server_port}" for name in LOOPBACK}
            if origin and origin not in origins and origin not in local:
                return self.respond(403, error(None, -32600, "Origin not allowed"))
            # Bytes: compare_digest rejects non-ASCII str, which must be a 401, not a crash.
            if credential and not hmac.compare_digest(
                self.headers.get("Authorization", "").encode("latin-1", "replace"), credential
            ):
                return self.respond(401, error(None, -32600, "Unauthorized"))
            if self.headers.get_content_type() != "application/json":
                return self.respond(415, error(None, -32600, "Use application/json"))
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self.respond(400, error(None, -32600, "Invalid content length"))
            if length <= 0 or length > MAX_BODY or self.headers.get("Transfer-Encoding"):
                return self.respond(413, error(None, -32600, "Invalid request size/framing"))
            try:
                message = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeError, RecursionError):
                return self.respond(400, error(None, -32700, "Invalid JSON"))
            status, response = protocol.dispatch(
                message, {k.lower(): v for k, v in self.headers.items()}
            )
            self.respond(status, response)

    server = ThreadingHTTPServer((host, port), Handler)
    server.request_slots = slots
    return server
