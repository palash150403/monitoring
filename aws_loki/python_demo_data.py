from flask import Flask, request, make_response
import requests, gzip, io, time, base64, json

app = Flask(__name__)
LOKI_URL = "http://localhost:3100/loki/api/v1/push"

@app.route('/', methods=['POST'])
def firehose_to_loki():
    request_id = request.headers.get('X-Amz-Firehose-Request-Id', 'unknown')
    content_encoding = request.headers.get('Content-Encoding', '')
    now_ms = int(time.time() * 1000)

    try:
        # 1. Decode Firehose body (supporting gzip)
        raw = request.get_data()
        if content_encoding == 'gzip':
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as f:
                raw = f.read()

        payload = json.loads(raw)
        records = payload.get('records', [])

        loki_stream = {
            'stream': {'source': 'firehose'},
            'values': []
        }

        for rec in records:
            try:
                decoded = base64.b64decode(rec['data']).decode('utf-8')
            except Exception:
                continue
            timestamp_ns = str(int(time.time() * 1e9))
            loki_stream['values'].append([timestamp_ns, decoded])

        if loki_stream['values']:
            loki_payload = {'streams': [loki_stream]}
            r = requests.post(LOKI_URL, json=loki_payload)
            if not (200 <= r.status_code < 300):
                return firehose_error(request_id, now_ms, "Failed to push to Loki"), 500

        return firehose_success(request_id, now_ms), 200

    except Exception as e:
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
        "errorMessage": message[:8192]  # Max error message size
    })
    return make_response_with_headers(body, 500)

def make_response_with_headers(body, status):
    response = make_response(body, status)
    response.headers['Content-Type'] = 'application/json'
    response.headers['Content-Length'] = str(len(body))
    return response

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=9999)
