from flask import Flask, request, make_response
import requests
import gzip
import io
import time
import base64
import json
import logging

app = Flask(__name__)
LOKI_URL = "http://localhost:3100/loki/api/v1/push"

# Set up logging
logging.basicConfig(level=logging.INFO)

@app.route('/', methods=['POST'])
def firehose_to_loki():
    request_id = request.headers.get('X-Amz-Firehose-Request-Id', 'unknown')
    content_encoding = request.headers.get('Content-Encoding', '')
    now_ms = int(time.time() * 1000)

    app.logger.info(f"Received request ID: {request_id}")

    try:
        # Decode Firehose body (supporting gzip)
        raw = request.get_data()
        if content_encoding == 'gzip':
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as f:
                raw = f.read()

        payload = json.loads(raw)
        app.logger.info(f"Payload: {payload}")

        records = payload.get('records', [])
        streams = []

        for rec in records:
            try:
                app.logger.info(f"Base64 Data: {rec['data']}")
                decoded_data = base64.b64decode(rec['data'])

                try:
                    decompressed_data = gzip.decompress(decoded_data).decode('utf-8')
                except Exception:
                    decompressed_data = decoded_data.decode('utf-8')

                log_payload = json.loads(decompressed_data)

                if log_payload.get('messageType') == 'DATA_MESSAGE':
                    log_group = log_payload.get('logGroup', 'unknown')
                    log_stream = log_payload.get('logStream', 'unknown')
                    subscription_filters = ','.join(log_payload.get('subscriptionFilters', []))

                    stream = {
                        'stream': {
                            'source': 'firehose',
                            'logGroup': log_group,
                            'logStream': log_stream,
                            'subscriptionFilters': subscription_filters
                        },
                        'values': []
                    }

                    for event in log_payload.get('logEvents', []):
                        timestamp_ns = str(int(event['timestamp']) * 1_000_000)
                        message = event.get('message', '')
                        stream['values'].append([timestamp_ns, message])

                    if stream['values']:
                        streams.append(stream)

            except Exception as e:
                app.logger.error(f"Error processing record: {str(e)}")
                continue

        if streams:
            loki_payload = {'streams': streams}
            r = requests.post(LOKI_URL, json=loki_payload)
            if not (200 <= r.status_code < 300):
                return firehose_error(request_id, now_ms, "Failed to push to Loki"), 500

        return firehose_success(request_id, now_ms), 200

    except Exception as e:
        app.logger.error(f"Error processing logs: {str(e)}")
        return firehose_error(request_id, now_ms, str(e)), 500

def firehose_success(request_id, timestamp):
    body = json.dumps({
        "requestId": request_id,
        "timestamp": timestamp
    })
    return make_response_with_headers(body, 200)

def firehose_error(request_id, timestamp, message):
    body = json.dumps({
        "requestId": request_id,
        "timestamp": timestamp,
        "errorMessage": message[:8192]
    })
    return make_response_with_headers(body, 500)

def make_response_with_headers(body, status):
    response = make_response(body, status)
    response.headers['Content-Type'] = 'application/json'
    response.headers['Content-Length'] = str(len(body))
    return response

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=9999)
