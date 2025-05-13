# AWS Logs using py script

## Setup on AWS side 

### Steps:-

1. Create a bucket.
2. Create a lambda function.
3. Deploy and test to create a log group.
4. create a data fire hose stream with GZIP enabled, source -> Direct PUT, Destinatin -> HTTP endpoint.
5. create a policy (this is full access policy)
```
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "VisualEditor0",
            "Effect": "Allow",
            "Action": [
                "cloudwatch:*",
                "s3:*",
                "firehose:*",
                "lambda:*",
                "kinesis:*"
            ],
            "Resource": "*"
        }
    ]
}
```
6. Attach this policy to a role. edit it's trust relationship like.
```
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {
                "Service": [
                    "logs.us-east-1.amazonaws.com",
                    "firehose.amazonaws.com"
                ]
            },
            "Action": "sts:AssumeRole"
        }
    ]
}
```
7. Now, go to your log group over there create a subsription group and while creating attach the above role.
8. test the fire hose stream with demo data.

## On VM side

### Steps:-

1. launch a docker compose file
```
version: '3'
services:
  loki:
    image: grafana/loki:3.0.0
    ports:
      - "3100:3100"
    command: -config.file=/etc/loki/local-config.yaml
    volumes:
      - lokidata:/loki
    networks:
      - loki

  grafana:
    environment:
      - GF_PATHS_PROVISIONING=/etc/grafana/provisioning
      - GF_AUTH_ANONYMOUS_ENABLED=true
      - GF_AUTH_ANONYMOUS_ORG_ROLE=Admin
    entrypoint:
      - sh
      - -euc
      - |
        mkdir -p /etc/grafana/provisioning/datasources
        cat <<EOF > /etc/grafana/provisioning/datasources/ds.yaml
        apiVersion: 1
        datasources:
        - name: Loki
          type: loki
          access: proxy
          orgId: 1
          url: http://loki:3100
          basicAuth: false
          isDefault: false
          version: 1
          editable: false
        EOF
        /run.sh
    image: grafana/grafana:11.0.0
    ports:
      - "3000:3000"
    networks:
      - loki

networks:
  loki:

volumes:
  lokidata:
    driver: local
    driver_opts:
      type: 'none'
      o: 'bind'
      device: ./volumes/loki
```
-   to run this file create a volume in the folder where this compose file is present with the following path ./volumes/loki

2. Install Nginx
3. create certificates and keys using 'letsencrypt'
4. go to /etc/nginx/sites-enabled/
5. now create a new file and add this config
```
server {
    listen 80;
    server_name <domian-name>;  # Your server's IP address or domain name

    location / {
        return 301 https://$host$request_uri;  # Redirect HTTP to HTTPS
    }
}

server {
    listen 443 ssl;
    server_name <domian-name>;  # Your server's IP address or domain name

    ssl_certificate /etc/letsencrypt/live/<domian-name>/fullchain.pem;  # Path to your SSL certificate
    ssl_certificate_key /etc/letsencrypt/live/<domian-name>/privkey.pem;  # Path to your SSL key

    location / {
        proxy_pass http://localhost:9999;  # Forward traffic to Alloy
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        add_header Content-Type application/json always;
    }
}
```
6. restart nginx
7. install python or its virtual env.
8. run this py flask app that runs on port 9999
```
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
```
