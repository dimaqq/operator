from __future__ import annotations

import ops
import ops.testing


class DifferentSecretRefreshesCharm(ops.CharmBase):
    def __init__(self, framework: ops.Framework):
        super().__init__(framework)
        framework.observe(self.on.start, self._on_start)
        framework.observe(self.on.install, self._on_install)

    def _on_start(self, event: ops.StartEvent):
        self.unit.add_secret({'foo': 'bar'}, label='my-secret')

    def _on_install(self, event: ops.InstallEvent):
        secret = self.model.get_secret(label='my-secret')
        secret.set_info(description="DADA")
        assert secret.get_info().description


def test_secret_values_are_in_sync():
    ctx = ops.testing.Context(DifferentSecretRefreshesCharm, meta={'name': 'foo'})
    state = ctx.run(ctx.on.start(), ops.testing.State())
    state = ctx.run(ctx.on.install(), state)
