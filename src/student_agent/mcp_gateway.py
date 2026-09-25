from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .contracts import Contracts


class MCPToolError(RuntimeError):
    """The gateway reached MCP, but the requested tool rejected the call."""


@dataclass(frozen=True)
class ToolSpec:
    """Discovered MCP tool contract. Discovery is the only source of tool names/arguments."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] | None = None

    @property
    def properties(self) -> frozenset[str]:
        props = self.input_schema.get("properties")
        return frozenset(props) if isinstance(props, dict) else frozenset()

    @property
    def required(self) -> frozenset[str]:
        required = self.input_schema.get("required")
        return frozenset(r for r in required if isinstance(r, str)) if isinstance(
            required, list
        ) else frozenset()

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
        }


def _spec_from_tool(tool: Any) -> ToolSpec:
    input_schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None)
    output_schema = getattr(tool, "output_schema", None) or getattr(tool, "outputSchema", None)
    return ToolSpec(
        name=str(tool.name),
        description=str(getattr(tool, "description", "") or ""),
        input_schema=dict(input_schema) if isinstance(input_schema, dict) else {},
        output_schema=dict(output_schema) if isinstance(output_schema, dict) else None,
    )


class EvidenceGateway:
    def __init__(self, session: ClientSession, contracts: Contracts) -> None:
        self._session = session
        self._contracts = contracts
        self._tool_names: tuple[str, ...] | None = None
        self._specs: dict[str, ToolSpec] = {}

    async def _discover(self) -> None:
        response = await self._session.list_tools()
        specs = [_spec_from_tool(tool) for tool in response.tools]
        self._specs = {spec.name: spec for spec in specs}
        self._tool_names = tuple(sorted(self._specs))

    async def list_tools(self) -> list[str]:
        """Discovery runs once per gateway (not per case) and is cached."""
        if self._tool_names is None:
            await self._discover()
        return list(self._tool_names or ())

    async def describe_tools(self) -> dict[str, ToolSpec]:
        if self._tool_names is None:
            await self._discover()
        return dict(self._specs)

    async def call(self, tool_name: str, *, case_id: str, **arguments: Any) -> dict[str, Any]:
        if self._tool_names is not None and tool_name not in self._tool_names:
            raise MCPToolError(f"MCP tool is not available: {tool_name}")
        payload = {"case_id": case_id, **arguments}
        try:
            result = await self._session.call_tool(tool_name, arguments=payload)
        except TimeoutError:
            raise

        # MCP SDK 1.x used camelCase aliases while 2.x exposes snake_case.
        is_error = getattr(result, "is_error", getattr(result, "isError", False))
        if is_error:
            message = " ".join(
                block.text for block in result.content if getattr(block, "text", None)
            )
            raise MCPToolError(f"MCP tool {tool_name} failed: {message or 'unknown error'}")
        evidence = getattr(result, "structured_content", None)
        if evidence is None:  # compatibility with MCP SDK 1.x
            evidence = getattr(result, "structuredContent", None)
        if evidence is None:
            text_blocks = [block.text for block in result.content if getattr(block, "text", None)]
            if len(text_blocks) != 1:
                raise ValueError(f"MCP tool {tool_name} did not return one evidence object")
            evidence = json.loads(text_blocks[0])
        self._contracts.validate_evidence(evidence, f"MCP tool {tool_name}")
        return evidence


@asynccontextmanager
async def connect_gateway(
    endpoint: str, team_api_key: str, contracts: Contracts
) -> AsyncIterator[EvidenceGateway]:
    headers = {"Authorization": f"Bearer {team_api_key}"}
    timeout = httpx2.Timeout(300.0, connect=30.0, write=30.0, pool=30.0)
    async with (
        httpx2.AsyncClient(headers=headers, timeout=timeout) as http_client,
        streamable_http_client(endpoint, http_client=http_client) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        yield EvidenceGateway(session, contracts)
