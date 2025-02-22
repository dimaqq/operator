# Copyright 2022 Canonical Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use this
# file except in compliance with the License. You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software distributed under
# the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific language
# governing permissions and limitations under the License.
"""FIXME Docstring."""

from __future__ import annotations

import contextlib
import logging
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from typing import Sequence

# FIXME: single-file Python package can't be marked as py.typed
import otlp_json  # type: ignore
from opentelemetry.instrumentation.urllib import URLLibInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import get_tracer_provider, set_tracer_provider

import ops
import ops._tracing.buffer
import ops.jujucontext
import ops.log

# FIXME otlp_json is typed...
# https://github.com/python/typing/issues/1333

# Trace `urllib` usage when talking to Pebble
URLLibInstrumentor().instrument()

# NOTE: nominally int, although float would work just as well in practice
EXPORTER_TIMEOUT: int = 1  # seconds
"""How much to give OTLP span exporter has to push traces to the backend."""

SENDOUT_FACTOR: int = 2
"""How many buffered chunks to send out for each incoming chunk."""

BUFFER_FILE: str = '.tracing-data.db'
"""Name of the file whither data is buffered, located next to .unit-state.db."""


logger = logging.getLogger(__name__)
_exporter: ProxySpanExporter | None = None


# NOTE: OTEL SDK suppresses errors while exporting data
# TODO: decide if we need to remove this before going to prod
logger.addHandler(logging.StreamHandler())


class ProxySpanExporter(SpanExporter):
    settings: tuple[str | None, str | None] = (None, None)
    cache: dict[str | None, ssl.SSLContext]

    def __init__(self, buffer_path: str):
        self.buffer = ops._tracing.buffer.Buffer(buffer_path)
        self.lock = threading.Lock()
        self.cache = {}

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        """Export a batch of telemetry data.

        Note: to avoid data loops or recursion, this function cannot be instrumented.
        """
        try:
            with suppress_juju_log_handler():
                # Note:
                # this is called in a helper thread, which is daemonic,
                # the MainThread will wait at most 10s for this thread.
                # Margins:
                # - 1s safety margin
                # - 1s for buffered data time overhang
                # - 2s for live data
                deadline = time.monotonic() + 6

                assert spans  # the BatchSpanProcessor won't call us if there's no data
                # TODO:  this will change in the JSON experiment
                # __import__("pdb").set_trace()
                # FIXME can't use stock exporter, must DIY

                rv = self.buffer.pump((otlp_json.encode_spans(spans), otlp_json.CONTENT_TYPE))
                assert rv
                self.do_export(*rv)

                for _ in range(SENDOUT_FACTOR - 1):
                    if time.monotonic() > deadline:
                        break
                    if not (rv := self.buffer.pump()):
                        break
                    self.do_export(*rv)

                return SpanExportResult.SUCCESS
        except Exception:
            logger.exception('export')
            raise

    def ssl_context(self, ca: str | None) -> ssl.SSLContext:
        if context := self.cache.get(ca):
            return context
        context = self._ssl_context(ca)
        self.cache.clear()
        self.cache[ca] = context
        return context

    def _ssl_context(self, ca: str | None) -> ssl.SSLContext:
        # FIXME: What should our protocol range be?
        # this means TLS {v1, v1.1, v1.2}
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        # FIXME: we should probably allow ca=None
        # and then we'd pick up system or certifi certs?
        assert ca
        context.load_verify_locations(cadata=ca)
        # FIXME: what's recommended?
        # context.set_ciphers(...)
        # FIXME: we need to set NPN if we're setting ALPN?
        # Does this work the same way across Py 3.8~3.13?
        context.set_npn_protocols('http/1.1')
        context.set_alpn_protocols('http/1.1')
        # TODO: check that we don't need these:
        # .set_sni_callback
        return context

    def do_export(self, buffered_id: int, data: bytes, mime: str) -> None:
        """Export buffered data and remove it from the buffer on success."""
        url, ca = self.settings
        if not url:
            return

        # FIXME cache

        # FIXME: is this custom code worth it?
        # or would it be easier and safer to use `requests`?
        assert url.startswith(('http://', 'https://'))
        context = self.ssl_context(ca) if url.startswith('https://') else None

        try:
            with urllib.request.urlopen(  # noqa: S310
                urllib.request.Request(  # noqa: S310
                    url,
                    data=data,
                    headers={'Content-Type': mime},
                    method='POST',
                ),
                context=context,
                                        ): pass
        except urllib.error.HTTPError as e:
            # FIXME drop this later
            # - perhaps the collector is shot
            # - or there's a bug converting spans to JSON
            # if it's the latter, the response test/JSON is helpful
            resp = e.fp.read()
            print("FIXME", e.code, str(resp)[:1000])
        except (urllib.error.URLError, TimeoutError, ssl.SSLError):
            pass
        except Exception:
            logger.exception("Failed to send telemetry out")
        else:
            self.buffer.remove(buffered_id)

    def shutdown(self) -> None:
        """Shut down the exporter."""
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        """No-op, as the real exporter doesn't buffer."""
        return True


@contextlib.contextmanager
def suppress_juju_log_handler():
    handlers = [h for h in logging.root.handlers if isinstance(h, ops.log.JujuLogHandler)]
    if not handlers:
        yield
        return

    juju_log_handler = handlers[0]
    token = juju_log_handler.drop.set(True)
    try:
        yield
    finally:
        juju_log_handler.drop.reset(token)


def setup_tracing(charm_class_name: str) -> None:
    global _exporter
    # FIXME would it be better to pass Juju context explicitly?
    juju_context = ops.jujucontext._JujuContext.from_dict(os.environ)
    app_name = '' if juju_context.unit_name is None else juju_context.unit_name.split('/')[0]
    service_name = f'{app_name}-charm'  # only one COS charm sets custom value

    resource = Resource.create(
        attributes={
            # https://opentelemetry.io/docs/languages/sdk-configuration/general/
            # https://github.com/open-telemetry/semantic-conventions/tree/main/docs/resource#semantic-attributes-with-dedicated-environment-variable
            #
            # TODO:
            # service.namespace: model uuid, because that's globally unique
            # (find some field): model name, human-readable
            # service.name: unit name (?)
            # service.instance.id: /etc/machine-id (set for vms, empty for k8s)
            # (these follow Juju topology by Simme)
            #
            # alternatively, one could argue that:
            # service.name: app name
            # (some field): unit name
            # - could abuse instance id, though that's not what it's for
            #
            # alternatively still:
            # service.namespace: model uuid
            # service.name: charm name (perhaps not app name?)
            # service.instance.id: unit name?
            #
            # OTEL defines some standard-ish attributes:
            # service.name        required
            # service.instance.id recommended
            # service.namespace   recommended -- maybe model name?
            # service.version     recommended
            #
            # FIXME: this is quite important, I think
            # because we're setting the stage for future Juju telemetry
            # ideally we'd agree what these fields mean for the end users
            # to have a consistent view over their deployment.
            'service.name': charm_class_name,
            'service.namespace': juju_context.model_uuid,
            'service.instance.id': juju_context.unit_name,
            # charm lib tags
            'compose_service': service_name,  # FIXME why is this copy needed?
            'charm_type': charm_class_name,
            # these may not be needed if included in service.xxx
            # Following same attribute names as charm_tracing lib
            # FIXME: decide if it makes sense
            'juju_unit': juju_context.unit_name,
            'juju_application': app_name,
            'juju_model': juju_context.model_name,
            'juju_model_uuid': juju_context.model_uuid,
        }
    )
    provider = TracerProvider(resource=resource)

    # How

    buffer_path = str(juju_context.charm_dir / BUFFER_FILE)
    _exporter = ProxySpanExporter(buffer_path)
    span_processor = BatchSpanProcessor(_exporter)
    provider.add_span_processor(span_processor)
    set_tracer_provider(provider)


def set_tracing_destination(
    *,
    url: str | None,
    ca: str | None,
) -> None:
    # FIXME only if it's a path, obv...
    # should we also check that this path exists?
    if ca is not None and not ca.startswith('/'):
        raise ValueError(f'{ca=} must be an absolute path')

    assert _exporter, 'tracing has not been set up'
    _exporter.settings = (url, ca)

    _exporter.buffer.mark_observed()


def shutdown_tracing() -> None:
    """Shutdown tracing, which is expected to flush the buffered data out."""
    get_tracer_provider().shutdown()  # type: ignore
