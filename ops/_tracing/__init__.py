# Copyright 2025 Canonical Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""The tracing facility of the Operator Framework.

TODO: quick start, usage example.
"""

from __future__ import annotations

import warnings
from contextlib import contextmanager
from typing import Generator

import opentelemetry.trace

import ops.version
from ops.jujucontext import _JujuContext

tracer = opentelemetry.trace.get_tracer('ops', ops.version.version)


try:
    from ops_tracing import (
        mark_observed,
        set_tracing_destination,
        setup_tracing,
    )
except ImportError:

    def mark_observed() -> None: ...

    def set_tracing_destination(url: str | None, ca: str | None) -> None:
        warnings.warn(
            'Tracing is not enabled, but set_tracing_destination() was called. '
            "Ensure that 'ops[tracing]' is installed.",
            UserWarning,
            stacklevel=3,
        )
        if url and not url.startswith(('http://', 'https://')):
            raise ValueError('Only HTTP and HTTPS tracing destinations are supported.')

    @contextmanager
    def setup_tracing(
        juju_context: _JujuContext, charm_class_name: str
    ) -> Generator[None, None, None]:
        yield


__all__ = [
    'mark_observed',
    'set_tracing_destination',
    'setup_tracing',
]
