"""Optional second exporter on LiteLLM's existing OTel provider.

Load after arize_phoenix. Endpoint is explicit, no global OTel settings change.
The original exporter and exact ReadableSpan identities remain intact.
"""
import logging
import os
import threading

from litellm.integrations.custom_logger import CustomLogger

log = logging.getLogger(__name__)
_lock = threading.Lock()


def ensure_secondary_exporter():
    endpoint = os.environ.get('DAL_OTLP_TRACES_ENDPOINT')
    if not endpoint:
        return False
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        return False
    with _lock:
        installed = getattr(provider, '_dal_secondary_endpoint', None)
        if installed:
            if installed != endpoint:
                raise RuntimeError('Secondary endpoint changed; restart to apply it')
            return True
        exporter = OTLPSpanExporter(
            endpoint=endpoint, timeout=5,
            # A nonempty explicit map avoids inheriting another backend's auth headers.
            headers={'x-dal-capture': 'dual'},
        )
        processor = BatchSpanProcessor(
            exporter, max_queue_size=256, max_export_batch_size=4,
            schedule_delay_millis=1000, export_timeout_millis=5000,
        )
        provider.add_span_processor(processor)
        provider._dal_secondary_endpoint = endpoint
        log.info('DAL secondary OTel exporter installed; original exporter retained')
    return True


class SecondaryOtelHook(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        try:
            ensure_secondary_exporter()
        except Exception as exc:
            # Export configuration must never fail a model request.
            log.warning('DAL secondary exporter unavailable: %s', type(exc).__name__)
        return data


proxy_handler_instance = SecondaryOtelHook()
