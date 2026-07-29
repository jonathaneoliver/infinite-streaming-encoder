#!/usr/bin/env python3
"""Static check: every `$.field` a Step Functions Map state references must be
present in that Map's ItemSelector.

Step Functions resolves `"Value.$": "$.est_vmaf"` against the CURRENT state's
input, and inside a Map that input is whatever the ItemSelector built — not the
enclosing scope. Reference a field the selector does not project and the
execution dies at runtime with:

    States.Runtime: The JSONPath '$.est_vmaf' specified for the field 'Value.$'
    could not be found in the input '{...}'

Nothing catches that earlier: the template is valid JSON, `tofu validate` passes,
and the apply succeeds. It surfaces only when a real encode reaches the state.
Which is exactly how #157 shipped broken — the workflow has TWO nested Maps
(variants, then chunks within a variant), the field was added to the outer
selector and both Environment blocks, and the inner chunk selector was missed.
Whole-variant encodes would have worked; every chunked encode failed.

Run over infra/terraform/modules/workflow/definition.json.tpl. Terraform
interpolations (${...}) are stubbed before parsing, since they are not JSON.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

TEMPLATE = Path("infra/terraform/modules/workflow/definition.json.tpl")

# A whole-string JSONPath into the current scope: "$.field". Deliberately NOT
# matching "$$.Map.Item.Value" (context object, always available) or longer paths
# like "$.a.b" — a nested lookup means the field is a struct and projecting the
# root is enough.
_SCOPE_REF = re.compile(r"\$\.([A-Za-z_][A-Za-z0-9_]*)$")


def _refs(node: object, out: set[str]) -> set[str]:
    """Every `$.field` referenced anywhere beneath node."""
    if isinstance(node, dict):
        for value in node.values():
            if isinstance(value, str):
                m = _SCOPE_REF.fullmatch(value)
                if m:
                    out.add(m.group(1))
            else:
                _refs(value, out)
    elif isinstance(node, list):
        for value in node:
            _refs(value, out)
    return out


def _selector_fields(state: dict) -> set[str]:
    """The field names a Map's ItemSelector projects into its item scope."""
    sel = state.get("ItemSelector") or {}
    return {k[:-2] if k.endswith(".$") else k for k in sel}


def _walk(states: dict | None, path: str) -> list[tuple[str, list[str]]]:
    """Report (map_path, missing_fields) for every Map whose body references a
    field its own ItemSelector does not project.

    Descends through EVERY state container, not just Map bodies: this workflow
    nests its Maps inside a Parallel, and an earlier version of this check that
    only recursed into Map bodies found zero Maps and passed vacuously.
    """
    problems: list[tuple[str, list[str]]] = []
    for name, state in (states or {}).items():
        if not isinstance(state, dict):
            continue
        here = f"{path}/{name}"

        if state.get("Type") == "Map":
            projected = _selector_fields(state)
            processor = state.get("ItemProcessor") or state.get("Iterator") or {}
            # Only the Map's BODY resolves against the item scope. ItemsPath and
            # ItemSelector resolve against the ENCLOSING scope, so they are
            # excluded — including them would flag every correct selector.
            missing = sorted(_refs(processor, set()) - projected)
            if missing:
                problems.append((here, missing))

        # Recurse into any nested states, whatever the parent type.
        for container in ("ItemProcessor", "Iterator"):
            sub = state.get(container) or {}
            problems += _walk(sub.get("States"), here)
        for i, branch in enumerate(state.get("Branches") or []):
            problems += _walk((branch or {}).get("States"), f"{here}[{i}]")
    return problems


def main() -> int:
    if not TEMPLATE.exists():
        print(f"{TEMPLATE}: not found (run from the repo root)", file=sys.stderr)
        return 1
    raw = TEMPLATE.read_text()
    # ${job_queue_arn} etc. are Terraform interpolations, not JSON. Stub them to
    # a literal so the document parses; their values are irrelevant here.
    doc = json.loads(re.sub(r"\$\{[^}]*\}", "TF", raw))

    problems = _walk(doc.get("States"), "")
    if not problems:
        return 0
    print(f"{TEMPLATE}: Map body references fields its ItemSelector does not project:",
          file=sys.stderr)
    for map_path, missing in problems:
        print(f"  {map_path}: {', '.join(missing)}", file=sys.stderr)
    print("  Add them to that Map's ItemSelector, or the execution fails at "
          "runtime with States.Runtime.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
