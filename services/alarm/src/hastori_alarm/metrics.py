from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

REGISTRY = CollectorRegistry()

MESSAGES = Counter("alarm_messages", "Telemetry messages processed", registry=REGISTRY)
REJECTED = Counter(
    "alarm_rejected", "Messages sent to the dead-letter queue", ["reason"], registry=REGISTRY
)
TRANSITIONS = Counter("alarm_transitions", "Alarm state changes written", ["to"], registry=REGISTRY)
ALARMS_OPEN = Gauge("alarms_open", "Alarms currently open (active or clearing)", registry=REGISTRY)
EVAL_LAG = Histogram(
    "alarm_eval_lag_seconds",
    "Seconds from device timestamp to the message being evaluated",
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 300),
    registry=REGISTRY,
)
EVENT_PUBLISH_FAILURES = Counter(
    "alarm_event_publish_failures", "Redis publish failures for alarm events", registry=REGISTRY
)
RECONNECTS = Counter("alarm_rabbitmq_connects", "RabbitMQ consumer (re)starts", registry=REGISTRY)
