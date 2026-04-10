from __future__ import annotations

from collections import defaultdict
from itertools import groupby

from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import Footer
from textual.widgets._footer import FooterKey, FooterLabel, KeyGroup


class DynamicFooter(Footer):
    """Footer that lets the app override per-binding labels at runtime."""

    def _binding_description(self, binding: Binding) -> str:
        app = self.app
        if hasattr(app, "get_binding_description"):
            return str(app.get_binding_description(binding))
        return binding.description

    def compose(self) -> ComposeResult:
        if not self._bindings_ready:
            return

        active_bindings = self.screen.active_bindings
        bindings = [
            (binding, enabled, tooltip)
            for (_, binding, enabled, tooltip) in active_bindings.values()
            if binding.show
        ]
        action_to_bindings = defaultdict(list)
        for binding, enabled, tooltip in bindings:
            action_to_bindings[binding.action].append((binding, enabled, tooltip))

        self.styles.grid_size_columns = len(action_to_bindings)

        for group, multi_bindings_iterable in groupby(
            action_to_bindings.values(),
            lambda multi_bindings_: multi_bindings_[0][0].group,
        ):
            multi_bindings = list(multi_bindings_iterable)
            if group is not None and len(multi_bindings) > 1:
                with KeyGroup(classes="-compact" if group.compact else ""):
                    for grouped_bindings in multi_bindings:
                        binding, enabled, tooltip = grouped_bindings[0]
                        description = self._binding_description(binding)
                        yield FooterKey(
                            binding.key,
                            self.app.get_key_display(binding),
                            "",
                            binding.action,
                            disabled=not enabled,
                            tooltip=tooltip or description,
                            classes="-grouped",
                        ).data_bind(compact=Footer.compact)
                yield FooterLabel(group.description)
            else:
                for grouped_bindings in multi_bindings:
                    binding, enabled, tooltip = grouped_bindings[0]
                    description = self._binding_description(binding)
                    yield FooterKey(
                        binding.key,
                        self.app.get_key_display(binding),
                        description,
                        binding.action,
                        disabled=not enabled,
                        tooltip=tooltip or description,
                    ).data_bind(compact=Footer.compact)

        if self.show_command_palette and self.app.ENABLE_COMMAND_PALETTE:
            try:
                _node, binding, enabled, tooltip = active_bindings[
                    self.app.COMMAND_PALETTE_BINDING
                ]
            except KeyError:
                pass
            else:
                description = self._binding_description(binding)
                yield FooterKey(
                    binding.key,
                    self.app.get_key_display(binding),
                    description,
                    binding.action,
                    classes="-command-palette",
                    disabled=not enabled,
                    tooltip=tooltip or binding.tooltip or description,
                )
