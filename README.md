# jitsi-whisper-bridge

## What's this about?
Jitsi allows you to use a transcription service.
Rather than submitting your data to foreign organizations, many users
prefer to use a service that is under their control.
Georgi Gerganov has written the wonderful
[whisper.cpp](https://github.com/ggml-org/whisper.cpp.git).
You can use it for multi-lingual speech regocnition and translation.
If works on powerful CPUs or you use nVidia (CUDA), Radeon (HIP) or
Apple acceleration. You can use smaller models if you have neither GPUs
nor powerful CPUs available. (I use XTX7900 with HIP and ggml-large-v3-turbo.bin.)

The jitsi-whisper-bridge does the translation from jigasi to whisper-server
and feeds the results back.

## Jitsi config snippets
### `web/config.js`
```js
config.transcription = {
        enabled: true,
        translationLanguages: ['auto', 'en', 'de', 'es', 'fr', 'nl', 'sk', 'ua', 'pl', 'sv', 'cn'],
        translationLanguagesHead: ['auto', 'en'],
        useAppLanguage: true,
        preferredLanguage: 'auto',
        disableStartForAll: false,
        autoCaptionOnRecord: false,
};
```
### `jigasi/sip-communicator.properties`
```
org.jitsi.jigasi.ENABLE_TRANSCRIPTION=true
org.jitsi.jigasi.transcription.ENABLE_TRANSLATION=true
org.jitsi.jigasi.transcription.DIRECTORY=/tmp/transcripts
org.jitsi.jigasi.transcription.BASE_URL=https://{{ .Env.PUBLIC_URL }}/transcripts
org.jitsi.jigasi.transcription.jetty.port=-1
org.jitsi.jigasi.transcription.ADVERTISE_URL=true
org.jitsi.jigasi.transcription.SAVE_JSON=false
org.jitsi.jigasi.transcription.SEND_JSON=true
org.jitsi.jigasi.transcription.SAVE_TXT=true
org.jitsi.jigasi.transcription.SEND_TXT=false
org.jitsi.jigasi.transcription.RECORD_AUDIO=false
org.jitsi.jigasi.transcription.RECORD_AUDIO_FORMAT=wav
org.jitsi.jigasi.transcription.customService=org.jitsi.jigasi.transcription.WhisperTranscriptionService
org.jitsi.jigasi.transcription.whisper.websocket_url=wss://{{ .Env.BRIDGE_SERVER_AND_PORT }}/streaming-whisper/ws
# Add JWT configuration
org.jitsi.jigasi.transcription.whisper.private_key=<PASTE_BASE64_KEY_HERE>
org.jitsi.jigasi.transcription.whisper.private_key_name=whisper-key-1
org.jitsi.jigasi.transcription.whisper.jwt_audience=whisper-service
#org.jitsi.jigasi.xmpp.acc.USER_ID=transcriber@hidden.meet.jitsi
#org.jitsi.jigasi.xmpp.acc.PASS={{ Env.TRANSCRIBER_PASSWD }}
#org.jitsi.jigasi.xmpp.acc.ANONYMOUS_AUTH=false
# For testing without TLS
#org.jitsi.jigasi.xmpp.acc.ALLOW_NON_SECURE=true
```

## Jitsi bridge config
### Example config `/etc/whisper-bridge/config.yaml`:
```yaml
# Whisper Bridge Configuration

server:
  host: "127.0.0.1"  # Bind to localhost (nginx proxies to this)
  port: 9000
  ping_interval: 20
  ping_timeout: 20
  max_size: 10485760  # 10MB

whisper:
  url: "http://localhost:8080/inference"
  # url: "https://whisper.example.com:8443/inference"  # If remote
  timeout: 30
  sample_rate: 16000
  chunk_duration_ms: 3000  # 3 seconds for better recognition
  silence_threshold: 50

jwt:
  enabled: true
  public_key_path: "/etc/whisper-bridge/whisper-public-key.pem"
  audience: "whisper-service"

language:
  # Special code that triggers auto-detection in Whisper
  auto_detect_code: "auto"
  default: "en"

hallucination_filter:
  enabled: true
  min_length: 3
  patterns:
    # "Thank you" in multiple languages
    en:
      - "^thank you[\s!.]*$"
      - "^thanks[\s!.]*$"
      - "^thank[\s!.]*$"
    de:
      - "^danke[\s!.]*$"
      - "^vielen dank[\s!.]*$"
      - "^dankeschön[\s!.]*$"
    es:
      - "^gracias[\s!.]*$"
    fr:
      - "^merci[\s!.]*$"
      - "^merci beaucoup[\s!.]*$"
    nl:
      - "^dank je[\s!.]*$"
      - "^bedankt[\s!.]*$"
      - "^dank u[\s!.]*$"
    sk:
      - "^ďakujem[\s!.]*$"
      - "^vďaka[\s!.]*$"
    pl:
      - "^dziekuje.[\s!.]*$"
    sv:
      - "^tack[\s!.]*$"
      - "^tack så mycket[\s!.]*$"
    cn:
      - "^谢谢[\s!.]*$"
      - "^多谢[\s!.]*$"
      - "^感谢[\s!.]*$"
    # Common hallucinations (all languages)
    common:
      - "^thanks for watching[\s!.]*$"
      - "^please subscribe[\s!.]*$"
      - "^like and subscribe[\s!.]*$"
      - "^\[music\][\s.]*$"
      - "^\[applause\][\s.]*$"
      - "^subtitles by[\s.]*$"
      - "^www\..*\.com[\s.]*$"
      - "^off[\s.]*$"
```

## Usage examples
```bash
# Use config file
./jitsi-whisper-bridge.py -c /etc/whisper-bridge/config.yaml

# Override specific settings
./jitsi-whisper-bridge.py -c /etc/whisper-bridge/config.yaml --host 0.0.0.0 --port 9001

# Disable JWT for testing
./jitsi-whisper-bridge.py --no-jwt --whisper-url http://192.168.1.100:8080/inference

# Debug mode
./jitsi-whisper-bridge.py -v -c /etc/whisper-bridge/config.yaml

# Help
./jitsi-whisper-bridge.py --help
```

### systemd unit
```ini
[Unit]
Description=Jitsi Whisper WebSocket Bridge
After=network.target

[Service]
Type=simple
User=whisper
Group=whisper
WorkingDirectory=/opt/whisper-bridge
ExecStart=/usr/bin/python3 /opt/whisper-bridge/jitsi-whisper-bridge.py -c /etc/whisper-bridge/config.yaml
Restart=always
RestartSec=10

# Security hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/log/whisper-bridge

[Install]
WantedBy=multi-user.target

```

### whisper user and service enablement
```bash
# Create user and directories
useradd -r -s /bin/false whisper
mkdir -p /opt/whisper-bridge /var/log/whisper-bridge
chown whisper:whisper /var/log/whisper-bridge

# Copy bridge script
cp jitsi-whisper-bridge.py /opt/whisper-bridge/
chown -R whisper:whisper /opt/whisper-bridge

# Enable and start
systemctl daemon-reload
systemctl enable whisper-bridge
systemctl start whisper-bridge
systemctl status whisper-bridge
```


## Security
### Keys for JWT
```bash
# Generate RSA key pair
openssl genrsa -out whisper-private-key.pem 2048
openssl rsa -in whisper-private-key.pem -pubout -out whisper-public-key.pem

# Convert private key to PKCS8 format (required by Java)
openssl pkcs8 -topk8 -inform PEM -outform DER -nocrypt \
  -in whisper-private-key.pem -out whisper-private-key-pkcs8.der

# Base64 encode for Jigasi (single line, no wrapping)
echo "Base64 encoded private key for Jigasi:"
base64 -w 0 whisper-private-key-pkcs8.der
echo
```
### Reverse proxy for jitsi-whisper-bridge (nginx)
```nginx
server {
    listen 443 ssl http2;
    server_name whisper.yourdomain.com;
    
    # TLS Configuration
    ssl_certificate /etc/letsencrypt/live/whisper.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/whisper.yourdomain.com/privkey.pem;
    
    # Modern TLS settings
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;
    
    # WebSocket proxy to bridge
    location /streaming-whisper/ws/ {
        proxy_pass http://127.0.0.1:9000;
        proxy_http_version 1.1;
        
        # WebSocket upgrade headers
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        
        # Forward real client info
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        # Long timeout for persistent connections
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
    }
    
    # Optional: Health check endpoint
    location /health {
        return 200 "OK\n";
        add_header Content-Type text/plain;
    }
}

# Redirect HTTP to HTTPS
server {
    listen 80;
    server_name whisper.yourdomain.com;
    return 301 https://$server_name$request_uri;
}
```
### Reverse proxy in front of whisper.cpp (nginx)
```nginx
server {
    listen 8443 ssl http2;
    server_name whisper.yourdomain.com;
    
    ssl_certificate /etc/letsencrypt/live/whisper.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/whisper.yourdomain.com/privkey.pem;
    
    location /inference {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        client_max_body_size 10M;
    }
}
```
But you would probably in addition require some authentication (client cert or so) to not
expose your whisper.cpp `whisper-server` service to the world or you run it locally, in
which case you don't need the SSL setup.
