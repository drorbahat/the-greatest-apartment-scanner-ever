import json
import pathlib
import time

_EVENTS_DIR = pathlib.Path(__file__).resolve().parents[1] / "artifacts" / "events"


def emit(event_type: str, **payload) -> None:
    _EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = int(time.time() * 1000)
    path = _EVENTS_DIR / f"{ts}_{event_type}.json"
    path.write_text(
        json.dumps({"type": event_type, "ts": ts, **payload}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
