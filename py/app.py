import json
import time
import threading
from flask import Flask, jsonify, request
from azure.eventhub import EventHubConsumerClient
import requests

app = Flask(__name__)

# 🔧 Config
EVENT_HUB_CONNECTION_STR = ""
EVENT_HUB_NAME = "loki-eventhub"
LOKI_PUSH_URL = "http://localhost:3100/loki/api/v1/push"

# State tracking
listener_thread = None
listener_active = False

def send_to_loki(log_entries):
    streams = []

    for log, labels in log_entries:
        stream = {
            "stream": labels,  # Use only extracted labels
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
                    raw_type = f"{parts[i + 1]}/{parts[i + 2]}" if len(parts) > i + 2 else "UNKNOWN"

                    RESOURCE_TYPE_MAPPING = {
                        "MICROSOFT.WEB/SITES": "WebApp",
                        "MICROSOFT.DOCUMENTDB/DATABASEACCOUNTS": "CosmosDB",
                        "MICROSOFT.INSIGHTS/COMPONENTS": "AppInsights",
                        "MICROSOFT.KEYVAULT/VAULTS": "KeyVault",
                        "MICROSOFT.STORAGE/STORAGEACCOUNTS": "StorageAccount",
                    }

                    friendly_type = RESOURCE_TYPE_MAPPING.get(raw_type, raw_type)
                    labels["resource_type"] = friendly_type
                    labels["resource_name"] = parts[i + 3] if len(parts) > i + 3 else "unknown"

                if "level" in record:
                    labels["LogLevel"] = str(record["level"])
                if "category" in record:
                    labels["category"] = str(record["category"])

            except Exception as e:
                print("Error parsing resourceId or labels:", e)

            return labels

        def extract_log_line(record, resource_type):
            if resource_type == "WebApp":
                return record.get("resultDescription", None)
            else:
                return json.dumps(record)

        if isinstance(data, dict) and 'records' in data:
            for record in data['records']:
                labels = extract_labels(record)
                resource_type = labels.get("resource_type", "")
                log_line = extract_log_line(record, resource_type)

                if log_line:  # Only include if we have something meaningful
                    log_entries.append((log_line, labels))
        else:
            labels = extract_labels(data)
            resource_type = labels.get("resource_type", "")
            log_line = extract_log_line(data, resource_type)

            if log_line:
                log_entries.append((log_line, labels))

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