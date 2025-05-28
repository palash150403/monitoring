import json
import time
import threading
from flask import Flask, jsonify, request
from azure.eventhub import EventHubConsumerClient
import requests

app = Flask(__name__)

# 🔧 Config - Hardcoded values here
EVENT_HUB_CONNECTION_STR = "<connection-string>"
EVENT_HUB_NAME = "<event-hub-name>"
LOKI_PUSH_URL = "http://localhost:3100/loki/api/v1/push"
LOKI_LABELS = {"job": "azure", "source": "eventhub"}

# State tracking
listener_thread = None
listener_active = False

def send_to_loki(log_entries):
    streams = []

    for log, labels in log_entries:
        stream = {
            "stream": {**LOKI_LABELS, **labels},
            "values": [[str(int(time.time() * 1e9)), log]]
        }
        streams.append(stream)

    response = requests.post(LOKI_PUSH_URL, json={"streams": streams})
    if not response.ok:
        print("Failed to push to Loki:", response.status_code, response.text)


def on_event(partition_context, event):
    try:
        log_entries = []
        body = event.body_as_str(encoding='UTF-8')
        data = json.loads(body)

        def extract_labels(record):
            resource_id = record.get("resourceId", "")
            parts = resource_id.upper().split("/")

            labels = {}
            try:
                if "PROVIDERS" in parts:
                    i = parts.index("PROVIDERS")
                    labels["resource_type"] = parts[i + 1] if len(parts) > i + 1 else "unknown"
                    labels["resource_name"] = parts[i + 2] if len(parts) > i + 2 else "unknown"
            except Exception as e:
                print("Error parsing resourceId:", e)

            return labels

        if isinstance(data, dict) and 'records' in data:
            for record in data['records']:
                log_json = json.dumps(record)
                labels = extract_labels(record)
                log_entries.append((log_json, labels))
        else:
            log_json = json.dumps(data)
            labels = extract_labels(data)
            log_entries.append((log_json, labels))

        if log_entries:
            send_to_loki(log_entries)

        partition_context.update_checkpoint(event)
    except Exception as e:
        print("Error processing event:", e)


def start_eventhub_listener():
    global listener_active

    client = EventHubConsumerClient.from_connection_string(
        conn_str=EVENT_HUB_CONNECTION_STR,
        consumer_group="$Default",
        eventhub_name=EVENT_HUB_NAME,
    )

    with client:
        listener_active = True
        print("Event Hub listener started.")
        try:
            client.receive(
                on_event=on_event,
                starting_position="-1",  # from beginning
            )
        except Exception as e:
            print("Error in listener:", e)
        finally:
            listener_active = False
            print("Event Hub listener stopped.")

@app.route("/start", methods=["POST"])
def start_listener():
    global listener_thread, listener_active

    if listener_active:
        return jsonify({"status": "already running"})

    listener_thread = threading.Thread(target=start_eventhub_listener, daemon=True)
    listener_thread.start()
    return jsonify({"status": "started"})

@app.route("/status", methods=["GET"])
def status():
    return jsonify({"active": listener_active})

@app.route("/stop", methods=["POST"])
def stop():
    return jsonify({"note": "Use CTRL+C to stop the app or restart the container. Graceful shutdown not supported by SDK."})

if __name__ == "__main__":
    listener_thread = threading.Thread(target=start_eventhub_listener, daemon=True)
    listener_thread.start()
    print("Background Event Hub listener started automatically.")
    
    app.run(host="0.0.0.0", port=5000)
