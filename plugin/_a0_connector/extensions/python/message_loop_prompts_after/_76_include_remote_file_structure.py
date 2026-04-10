from __future__ import annotations

import time

from agent import LoopData
from helpers.extension import Extension

from plugins._a0_connector.helpers.ws_runtime import latest_remote_tree_for_context


class IncludeRemoteFileStructure(Extension):
    async def execute(self, loop_data: LoopData = LoopData(), **kwargs):
        if not self.agent:
            return

        context_id = getattr(self.agent.context, "id", "")
        if not context_id:
            return

        snapshot = latest_remote_tree_for_context(context_id, max_age_seconds=90.0)
        if not snapshot:
            return

        file_structure = str(snapshot.get("tree") or "").strip()
        if not file_structure:
            return

        folder = str(snapshot.get("root_path") or "").strip() or "unknown"
        generated_at = str(snapshot.get("generated_at") or "unknown")
        updated_at = float(snapshot.get("updated_at") or 0.0)
        age_seconds = max(0, int(time.time() - updated_at))

        prompt = self.agent.read_prompt(
            "agent.extras.remote_file_structure.md",
            folder=folder,
            generated_at=generated_at,
            age_seconds=age_seconds,
            file_structure=file_structure,
        )
        loop_data.extras_temporary["remote_file_structure"] = prompt
