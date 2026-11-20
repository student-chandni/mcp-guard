import json
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional, Union

DENIAL_TEXT = "Blocked by the user: MCP Guard denied this {kind} request ({reason})."


@dataclass
class ApprovalRequest:
    server: str
    method: str
    tool: str
    arguments: Any = None
    client: str = "unknown"
    transport: str = "stdio"
    rpc_id: Any = None
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ApprovalRequest":
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in data.items() if k in allowed})


def parse_message(raw: Union[bytes, str]) -> Optional[Any]:
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None


def is_request(msg: Any) -> bool:
    return isinstance(msg, dict) and "method" in msg and "id" in msg


def target_name(msg: Dict[str, Any]) -> str:
    params = msg.get("params") or {}
    if not isinstance(params, dict):
        return "?"
    # tools/prompts use "name", resources/read uses "uri"
    return str(params.get("name") or params.get("uri") or "?")


def build_approval_request(msg: Dict[str, Any], server: str, client: str,
                           transport: str) -> ApprovalRequest:
    params = msg.get("params") or {}
    if msg.get("method") == "tools/call" and isinstance(params, dict):
        arguments = params.get("arguments")
    elif isinstance(params, dict):
        arguments = {k: v for k, v in params.items() if k != "_meta"}
    else:
        arguments = params
    return ApprovalRequest(
        server=server,
        method=str(msg.get("method")),
        tool=target_name(msg),
        arguments=arguments,
        client=client,
        transport=transport,
        rpc_id=msg.get("id"),
    )


def denial_response(msg: Dict[str, Any], reason: str) -> Dict[str, Any]:
    method = msg.get("method")
    text = DENIAL_TEXT.format(kind=method, reason=reason)
    if method == "tools/call":
        # isError result rather than a JSON-RPC error: Claude passes it to the
        # model as a failed tool call instead of treating the server as broken
        return {
            "jsonrpc": "2.0",
            "id": msg.get("id"),
            "result": {"content": [{"type": "text", "text": text}], "isError": True},
        }
    return {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32001, "message": text}}


def client_label(init_params: Any) -> str:
    if not isinstance(init_params, dict):
        return "unknown"
    info = init_params.get("clientInfo") or {}
    name = str(info.get("name") or "unknown")
    version = info.get("version")
    return f"{name} {version}" if version else name


def dumps_line(msg: Any) -> bytes:
    return json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
