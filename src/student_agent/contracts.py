from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from . import VARIANT_ID

PUBLIC_CONTRACT_IDS = {
    "l3a-output-v2.schema.json": "https://day09.vinaction.local/contracts/l3a-output-v2.schema.json",
    "l3b-output-v2.schema.json": "https://day09.vinaction.local/contracts/l3b-output-v2.schema.json",
    "trace-event-v1.schema.json": "https://day09.vinaction.local/contracts/trace-event-v1.schema.json",
    "submission-manifest-v2.schema.json": "https://day09.vinaction.local/contracts/submission-manifest-v2.schema.json",
    "mcp-evidence-response-v1.schema.json": "https://day09.vinaction.local/contracts/mcp-evidence-response-v1.schema.json",
}


class ContractError(ValueError):
    pass


class Contracts:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        schemas: dict[str, dict[str, Any]] = {}
        registry = Registry()
        seen_ids: set[str] = set()
        for path in sorted(self.root.glob("*.schema.json")):
            schema = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(schema, dict):
                raise ContractError(f"{path.name}: schema must be a JSON object")
            Draft202012Validator.check_schema(schema)
            schema_id = schema.get("$id")
            if not isinstance(schema_id, str) or not schema_id:
                raise ContractError(f"{path.name}: schema must have a non-empty $id")
            expected_id = PUBLIC_CONTRACT_IDS.get(path.name)
            if expected_id is not None and schema_id != expected_id:
                raise ContractError(f"{path.name}: public contract $id changed")
            if schema_id in seen_ids:
                raise ContractError(f"duplicate schema $id: {schema_id}")
            seen_ids.add(schema_id)
            schemas[path.name] = schema
            resource = Resource.from_contents(schema)
            registry = registry.with_resource(schema_id, resource)
        missing = sorted(set(PUBLIC_CONTRACT_IDS) - set(schemas))
        if missing:
            raise ContractError(f"missing public contracts: {missing}")
        self._schemas = schemas
        self._registry = registry

    def validate(self, schema_name: str, value: Any, label: str) -> None:
        schema = self._schemas.get(schema_name)
        if schema is None:
            raise ContractError(f"contract not found: {schema_name}")
        validator = Draft202012Validator(
            schema, registry=self._registry, format_checker=FormatChecker()
        )
        errors = sorted(validator.iter_errors(value), key=lambda error: list(error.absolute_path))
        if errors:
            error = errors[0]
            location = ".".join(str(part) for part in error.absolute_path) or "$"
            raise ContractError(f"{label}:{location}: {error.message}")

    def validate_output(self, value: Any, label: str) -> None:
        self.validate(f"{VARIANT_ID}-output-v2.schema.json", value, label)

    def validate_trace(self, value: Any, label: str) -> None:
        self.validate("trace-event-v1.schema.json", value, label)

    def validate_manifest(self, value: Any, label: str = "manifest.json") -> None:
        self.validate("submission-manifest-v2.schema.json", value, label)

    def validate_evidence(self, value: Any, label: str = "MCP response") -> None:
        self.validate("mcp-evidence-response-v1.schema.json", value, label)
