"""Small MCP 2026-07-28 tools-only binding. No protocol session or SDK dependency."""

import base64
import hmac
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pydantic import ValidationError

from . import __version__

VERSION = "2026-07-28"
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

    def input_schema(self, schema):
        result = schema.model_json_schema()
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
        if (
            not isinstance(message, dict)
            or message.get("jsonrpc") != "2.0"
            or not isinstance(message.get("method"), str)
        ):
            return 400, error(None, -32600, "Expected one JSON-RPC request")
        request_id, method = message.get("id"), message["method"]
        if "id" not in message:
            if method in ("notifications/cancelled", "notifications/initialized"):
                return 202, None
            return 400, None
        if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
            return 400, error(None, -32600, "Request id must be a string or integer")
        params = message.get("params", {})
        if not isinstance(params, dict):
            return 400, error(request_id, -32602, "params must be an object")
        meta = params.get("_meta", {})
        if not isinstance(meta, dict):
            return 400, error(request_id, -32602, "_meta must be an object")
        # Legacy compatibility is deliberately confined to stdio. It establishes no
        # app session: namespace and episode handles are still explicit on every tool.
        legacy = headers is None and PREFIX + "protocolVersion" not in meta
        if legacy and method == "initialize":
            return 200, {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "graph-memory", "version": __version__},
                    "instructions": "Use graph-memory for remembering, recall, and corrections. Background workers process saved messages.",
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
        if headers is not None:
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
                "instructions": "Use graph-memory for remembering, recall, and corrections. Background workers process saved messages.",
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            if params.get("cursor"):
                return 400, error(request_id, -32602, "Unknown cursor")
            result = {
                "tools": [
                    {
                        "name": name,
                        "description": description,
                        "inputSchema": self.input_schema(schema),
                        "annotations": {
                            "readOnlyHint": name in READ_ONLY,
                            "destructiveHint": name in ("memory_merge", "memory_retract"),
                            "openWorldHint": name
                            in ("memory_extract", "memory_dream_run", "memory_ingest"),
                        },
                    }
                    for name, (schema, _, description) in sorted(
                        self.service.session_tools().items()
                    )
                    if not self.read_only or name in READ_ONLY
                ],
                "ttlMs": 300000,
                "cacheScope": "public",
            }
        elif method == "tools/call":
            name, arguments = params.get("name"), params.get("arguments", {})
            if not isinstance(name, str) or name not in self.service.session_tools():
                return 400, error(request_id, -32602, "Unknown tool")
            if self.read_only and name not in READ_ONLY:
                return 403, error(request_id, -32602, "This server permits retrieval only")
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
                    request_id, -32602, "Namespace is outside this server's authorized scope"
                )
            try:
                schema, handler, _ = self.service.session_tools()[name]
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
            except ValueError as exc:
                result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
            except Exception:
                result = {
                    "content": [
                        {
                            "type": "text",
                            "text": "Operation failed; durable receipts can be retried. Check service connectivity and model configuration.",
                        }
                    ],
                    "isError": True,
                }
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
                _, response = protocol.dispatch(json.loads(line))
            except (ValueError, UnicodeError):
                response = error(None, -32700, "Invalid JSON")
        if response is not None:
            print(json.dumps(response), flush=True)


def http_server(protocol, host="127.0.0.1", port=8765, token=None, origins=()):
    if host not in ("127.0.0.1", "::1", "localhost") and not token:
        raise ValueError("Non-loopback HTTP requires MEMORY_HTTP_TOKEN")
    if token and protocol.namespace is None:
        raise ValueError("Token-authenticated HTTP requires MEMORY_NAMESPACE to bind authorization")
    allowed_origins = set(origins) | {f"http://localhost:{port}", f"http://127.0.0.1:{port}"}

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
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.respond(405, error(None, -32600, "Use POST /mcp"))

        do_DELETE = do_GET

        def do_POST(self):
            if self.path != "/mcp":
                return self.respond(404, error(None, -32600, "Unknown endpoint"))
            if self.headers.get("Origin") and self.headers["Origin"] not in allowed_origins:
                return self.respond(403, error(None, -32600, "Origin not allowed"))
            if token and not hmac.compare_digest(
                self.headers.get("Authorization", ""), "Bearer " + token
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
            except (ValueError, UnicodeError):
                return self.respond(400, error(None, -32700, "Invalid JSON"))
            status, response = protocol.dispatch(
                message, {k.lower(): v for k, v in self.headers.items()}
            )
            self.respond(status, response)

    return ThreadingHTTPServer((host, port), Handler)
