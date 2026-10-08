# Copyright (c) 2026 AIXENI (aixeni.xyz). All rights reserved. Proprietary, see LICENSE. Copying, modifying or redistributing any part of this file without written permission is prohibited.
# AN-AIXENI-7f3c9e21
"""enable_tools: lets Astra switch on extra tool groups mid-task (keeps the local model's prompt small)."""
from ..paths import is_public
from . import GROUP_INFO, tool

_groups = {k: v for k, v in GROUP_INFO.items() if True}
_desc = "; ".join(f"{k} = {v}" for k, v in _groups.items())


@tool("enable_tools", "Switch on extra tool groups you need for this task (they appear from your next step). "
      f"Groups: {_desc}.",
      {"groups": {"type": "array", "items": {"type": "string", "enum": list(_groups)}}}, ["groups"],
      label="Loading tools")
def enable_tools(ctx, groups):
    if ctx.groups is None:
        return "All tools are already available."
    added = [g for g in groups or [] if g in GROUP_INFO and g not in ctx.groups]
    ctx.groups.update(added)
    return f"Enabled: {', '.join(added) or 'nothing new'}. Active groups: {', '.join(sorted(ctx.groups)) or 'core'}."
