from prometheus_client import CollectorRegistry, Counter, Histogram

REGISTRY = CollectorRegistry()

MESSAGES = Counter("ingest_messages", "Messages accepted and queued", registry=REGISTRY)
REJECTED = Counter("ingest_rejected", "Messages rejected", ["reason"], registry=REGISTRY)
LAG = Histogram(
    "ingest_lag_seconds",
    "Seconds from device timestamp to committed write",
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 300),
    registry=REGISTRY,
)
BATCH_SIZE = Histogram(
    "ingest_batch_size",
    "Rows per database batch",
    buckets=(1, 5, 10, 25, 50, 100, 250, 500),
    registry=REGISTRY,
)
PUBLISH_FAILURES = Counter(
    "ingest_publish_failures", "RabbitMQ publish failures", registry=REGISTRY
)
