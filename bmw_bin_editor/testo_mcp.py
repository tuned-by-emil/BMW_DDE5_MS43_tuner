from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


class TestOMcpError(RuntimeError):
    pass


def _jsonish(value: Any) -> Any:
    """Best-effort unwrap of MCP content/structuredContent into ordinary Python data."""
    if isinstance(value, dict):
        # MCP tool result wrapper.
        if value.get("isError"):
            msg = value.get("error") or value.get("structuredContent") or value.get("content") or value
            raise TestOMcpError(str(msg))
        if "structuredContent" in value and value["structuredContent"] not in (None, {}):
            return _jsonish(value["structuredContent"])
        if "content" in value and isinstance(value["content"], list):
            # Prefer JSON text payloads if present.
            texts = [x.get("text") for x in value["content"] if isinstance(x, dict) and x.get("type") == "text" and x.get("text")]
            for text in texts:
                try:
                    return _jsonish(json.loads(text))
                except Exception:
                    pass
            if len(texts) == 1:
                return texts[0]
        # Common TestO response envelopes.
        has_named_record = any(k in value for k in ("name", "resultName", "result_name", "key", "id"))
        if not has_named_record:
            for key in ("data", "result", "value", "results", "payload"):
                if key in value and len(value) <= 4:
                    return _jsonish(value[key])
        return {k: _jsonish(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonish(v) for v in value]
    if isinstance(value, str):
        s = value.strip()
        if s.startswith(("{", "[")):
            try:
                return _jsonish(json.loads(s))
            except Exception:
                return value
    return value


def flatten_numeric_fields(data: Any) -> dict[str, float]:
    """Flatten a TestO job result and keep numeric leaf values by their field names.

    The MCP docs intentionally leave the exact structuredContent shape to the tool,
    so this accepts nested dict/list result sets and several common value wrappers.
    """
    out: dict[str, float] = {}

    def add(name: str, value: Any):
        if isinstance(value, bool):
            out[name] = float(value)
            return
        if isinstance(value, (int, float)):
            out[name] = float(value)
            return
        if isinstance(value, str):
            try:
                out[name] = float(value.strip().replace(",", "."))
            except Exception:
                return

    def walk(obj: Any, inherited_name: str | None = None):
        if isinstance(obj, dict):
            # EDIABAS-ish records often carry name/result + value/wert fields.
            name = None
            for nk in ("name", "resultName", "result_name", "result", "key", "id"):
                v = obj.get(nk)
                if isinstance(v, str) and v:
                    name = v
                    break
            for vk in ("value", "wert", "numericValue", "number"):
                if vk in obj and (name or inherited_name):
                    add(name or inherited_name or vk, obj[vk])
            for k, v in obj.items():
                if k in {"unit", "einheit", "info", "description", "label"}:
                    continue
                if isinstance(v, (dict, list)):
                    walk(v, k)
                else:
                    add(k, v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item, inherited_name)
        elif inherited_name:
            add(inherited_name, obj)

    walk(_jsonish(data))
    return out


def find_named_list(data: Any, candidate_keys: tuple[str, ...]) -> list[str]:
    """Extract a list of names from loosely structured MCP tool results."""
    root = _jsonish(data)
    found: list[str] = []

    def walk(obj: Any):
        if isinstance(obj, dict):
            # Direct item records, e.g. [{"name":"DDE5"}, ...].
            for nk in ("name", "ecu", "job", "filename", "file"):
                if isinstance(obj.get(nk), str) and obj.get(nk).strip():
                    found.append(obj[nk])
                    break
            for key, value in obj.items():
                if key.lower() in {x.lower() for x in candidate_keys} and isinstance(value, list):
                    for item in value:
                        if isinstance(item, str):
                            found.append(item)
                        elif isinstance(item, dict):
                            for nk in ("name", "ecu", "job", "filename", "file"):
                                if isinstance(item.get(nk), str):
                                    found.append(item[nk]); break
                walk(value)
        elif isinstance(obj, list):
            for item in obj:
                if isinstance(item, str):
                    found.append(item)
                else:
                    walk(item)

    walk(root)
    # Preserve order while removing duplicates/noise.
    result: list[str] = []
    seen = set()
    for x in found:
        x = x.strip()
        if not x or x in seen:
            continue
        seen.add(x); result.append(x)
    return result


@dataclass
class TestOMcpClient:
    endpoint: str = "http://127.0.0.1:13721/mcp"
    bearer_token: str = ""
    timeout: float = 4.0

    def __post_init__(self):
        self.endpoint = self.endpoint.rstrip("/")
        self._next_id = 1
        self.initialized = False

    @property
    def health_url(self) -> str:
        base = self.endpoint
        if base.endswith("/mcp"):
            base = base[:-4]
        return base + "/health"

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.bearer_token:
            h["Authorization"] = "Bearer " + self.bearer_token
        return h

    def health(self) -> bool:
        req = urllib.request.Request(self.health_url, headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.read().decode("utf-8", "replace").strip().lower() == "ok"
        except Exception:
            return False

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        ident = self._next_id; self._next_id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}}).encode("utf-8")
        req = urllib.request.Request(self.endpoint, data=body, headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise TestOMcpError(f"HTTP {exc.code}: {detail or exc.reason}") from exc
        except Exception as exc:
            raise TestOMcpError(str(exc)) from exc
        if isinstance(data, dict) and data.get("error"):
            raise TestOMcpError(str(data["error"]))
        return data.get("result") if isinstance(data, dict) else data

    def initialize(self) -> Any:
        result = self.rpc("initialize", {})
        self.initialized = True
        return result

    def ensure_initialized(self):
        if not self.initialized:
            self.initialize()

    def list_tools(self) -> Any:
        self.ensure_initialized()
        return self.rpc("tools/list", {})

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        self.ensure_initialized()
        return self.rpc("tools/call", {"name": name, "arguments": arguments or {}})

    def list_ecus(self, name_filter: str = "") -> list[str]:
        args = {"filter": name_filter} if name_filter else {}
        data = self.call_tool("testo_list_ecus", args)
        return find_named_list(data, ("ecus", "items", "files", "data"))

    def list_jobs(self, ecu: str) -> list[str]:
        data = self.call_tool("testo_list_jobs", {"ecu": ecu})
        return find_named_list(data, ("jobs", "items", "data"))

    def describe_job(self, ecu: str, job: str) -> Any:
        return _jsonish(self.call_tool("testo_describe_job", {"ecu": ecu, "job": job}))

    def run_job(self, ecu: str, job: str, params: dict[str, Any] | None = None) -> Any:
        return _jsonish(self.call_tool("testo_run_job", {"ecu": ecu, "job": job, "params": params or {}}))
