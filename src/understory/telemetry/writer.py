"""Append-only Parquet writer for the two log families.

Write-only by design (design section 8.3). The server's log identity holds
`objectCreator` on the log prefixes and nothing else, so this module never
reads, lists, or deletes anything under a prefix. It creates new objects with
unique names and that is all. Locally the prefixes are directories; in GCS
deployments they are `gs://bucket/path` prefixes and files are uploaded with
google-cloud-storage, imported only when a gs:// prefix is configured.

Lifecycle

    writer = TelemetryWriter(tenant_cfg.log, tenant=tenant_cfg.name)
    writer.emit(ToolCalled(...))        # from any thread, sync or async code
    writer.emit_text(TextRecord(...))
    writer.close()                      # at shutdown; flushes and joins

`emit` and `emit_text` append to in-memory buffers under a lock and return at
once. A daemon thread flushes every `flush_interval_s` seconds, or sooner when
a buffer reaches `max_buffer` records. Each flush writes one Parquet file per
family that has anything buffered, to

    <prefix>/dt=YYYY-MM-DD/<YYYYMMDDTHHMMSSffffff>Z-<uuid4>.parquet

There is no asyncio dependency: the thread and lock make the writer safe to
call from coroutines without awaiting. Failures to write are logged and the
batch is dropped; nothing raises into the caller. With `config.enabled` False
every method is a no-op and no thread is started.
"""

from __future__ import annotations

import importlib
import logging
import os
import tempfile
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from understory.telemetry.events import BaseEvent, TextRecord, events_to_table, text_to_table
from understory.tenant import LogConfig

logger = logging.getLogger(__name__)

DEFAULT_FLUSH_INTERVAL_S = 5.0
DEFAULT_MAX_BUFFER = 500


class _LocalSink:
    """Writes tables under a directory prefix. Never lists or reads it."""

    def __init__(self, prefix: str) -> None:
        self.root = Path(prefix)

    def write(self, relative: str, table: pa.Table) -> str:
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write to a sibling temp name and rename so a concurrent reader's
        # glob never picks up a half-written file.
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".parquet", dir=target.parent)
        os.close(fd)
        try:
            pq.write_table(table, tmp, compression="snappy")
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return str(target)


class _GcsSink:
    """Uploads tables under a gs://bucket/prefix. Needs only objectCreator."""

    def __init__(self, prefix: str) -> None:
        rest = prefix[len("gs://") :]
        bucket, _, path = rest.partition("/")
        if not bucket:
            raise ValueError(f"gs:// prefix has no bucket: {prefix!r}")
        self.bucket_name = bucket
        self.path = path.strip("/")
        self._client = None

    def _bucket(self):
        if self._client is None:
            storage = importlib.import_module("google.cloud.storage")
            self._client = storage.Client()
        return self._client.bucket(self.bucket_name)

    def write(self, relative: str, table: pa.Table) -> str:
        name = f"{self.path}/{relative}" if self.path else relative
        with tempfile.TemporaryDirectory() as tmpdir:
            local = os.path.join(tmpdir, "batch.parquet")
            pq.write_table(table, local, compression="snappy")
            blob = self._bucket().blob(name)
            blob.upload_from_filename(local, content_type="application/octet-stream")
        return f"gs://{self.bucket_name}/{name}"


def _make_sink(prefix: str) -> _LocalSink | _GcsSink:
    if prefix.startswith("gs://"):
        return _GcsSink(prefix)
    return _LocalSink(prefix)


class TelemetryWriter:
    """Buffered, thread-backed Parquet appender for one tenant.

    Construct once per tenant at server start and `close()` at shutdown. See
    the module docstring for the lifecycle and the file layout.
    """

    def __init__(
        self,
        config: LogConfig,
        tenant: str,
        *,
        flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S,
        max_buffer: int = DEFAULT_MAX_BUFFER,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.tenant = tenant
        self.enabled = bool(config.enabled)
        self.flush_interval_s = flush_interval_s
        self.max_buffer = max(1, max_buffer)
        self._clock = clock or (lambda: datetime.now(UTC))

        self._lock = threading.Lock()
        self._events: list[BaseEvent] = []
        self._text: list[TextRecord] = []
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._closed = False
        self._thread: threading.Thread | None = None

        if not self.enabled:
            return
        self._events_sink = _make_sink(config.events_prefix)
        self._text_sink = _make_sink(config.text_prefix)
        self._thread = threading.Thread(
            target=self._run, name=f"understory-telemetry-{tenant}", daemon=True
        )
        self._thread.start()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def emit(self, event: BaseEvent) -> None:
        """Buffer an event. Returns immediately; never raises."""
        if not self.enabled or self._closed:
            return
        try:
            if not event.tenant:
                event = event.model_copy(update={"tenant": self.tenant})
            with self._lock:
                self._events.append(event)
                full = len(self._events) >= self.max_buffer
            if full:
                self._wake.set()
        except Exception:
            logger.exception("telemetry: failed to buffer event")

    def emit_text(self, record: TextRecord) -> None:
        """Buffer a text record for the text prefix. Returns immediately; never raises."""
        if not self.enabled or self._closed:
            return
        try:
            if not record.tenant:
                record = record.model_copy(update={"tenant": self.tenant})
            with self._lock:
                self._text.append(record)
                full = len(self._text) >= self.max_buffer
            if full:
                self._wake.set()
        except Exception:
            logger.exception("telemetry: failed to buffer text record")

    def flush(self) -> None:
        """Write whatever is buffered now, on the calling thread. Never raises."""
        if not self.enabled:
            return
        try:
            self._flush_once()
        except Exception:
            logger.exception("telemetry: flush failed")

    def close(self) -> None:
        """Stop the background thread and flush. Safe to call more than once."""
        if not self.enabled or self._closed:
            return
        self._closed = True
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=max(self.flush_interval_s, 1.0) + 30.0)
        self.flush()

    def __enter__(self) -> TelemetryWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def pending(self) -> tuple[int, int]:
        """(events, text records) currently buffered. For tests and health checks."""
        with self._lock:
            return len(self._events), len(self._text)

    # ------------------------------------------------------------------ #
    # Background thread
    # ------------------------------------------------------------------ #

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=self.flush_interval_s)
            self._wake.clear()
            try:
                self._flush_once()
            except Exception:
                logger.exception("telemetry: background flush failed")

    def _flush_once(self) -> None:
        with self._lock:
            events, self._events = self._events, []
            text, self._text = self._text, []
        if events:
            self._write("events", self._events_sink, events_to_table(events), len(events))
        if text:
            self._write("text", self._text_sink, text_to_table(text), len(text))

    def _write(self, family: str, sink: _LocalSink | _GcsSink, table: pa.Table, n: int) -> None:
        relative = self._relative_path()
        try:
            where = sink.write(relative, table)
            logger.debug("telemetry: wrote %d %s rows to %s", n, family, where)
        except Exception:
            # The batch is dropped rather than retried so a broken sink cannot
            # grow the buffer without bound. The count is in the log line.
            logger.exception("telemetry: dropping %d %s rows after write failure", n, family)

    def _relative_path(self) -> str:
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        now = now.astimezone(UTC)
        stamp = now.strftime("%Y%m%dT%H%M%S%f") + "Z"
        return f"dt={now:%Y-%m-%d}/{stamp}-{uuid.uuid4().hex}.parquet"
