#!/usr/bin/env python3
"""
WebSocket bridge between Jitsi (Jigasi) and whisper.cpp
WITH JWT AUTHENTICATION, TLS SUPPORT, AND CONFIGURATION FILE
(c) Kurt Garloff <consulting@garloff.de>, 1/2026
Code was mainly created by Claude AI under my supervision.
SPDX-License-Identifier: Apache-2.0
"""

import asyncio
import websockets
import json
import wave
import io
import requests
import logging
import re
import argparse
import yaml
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import jwt

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(funcName)s: %(message)s'
)
logger = logging.getLogger(__name__)

# ============================================================================
# CONFIGURATION
# ============================================================================

DEFAULT_CONFIG = {
    'server': {
        'host': '127.0.0.1',
        'port': 9000,
        'ping_interval': 20,
        'ping_timeout': 20,
        'max_size': 10485760  # 10MB
    },
    'whisper': {
        'url': 'http://localhost:8080/inference',
        'timeout': 30,
        'sample_rate': 16000,
        'chunk_duration_ms': 3000,
        'silence_threshold': 50
    },
    'jwt': {
        'enabled': True,
        'public_key_path': '/etc/whisper-bridge/whisper-public-key.pem',
        'audience': 'whisper-service'
    },
    'language': {
        'auto_detect_code': 'auto',  # Special code for auto-detection
        'default': 'en'
    },
    'hallucination_filter': {
        'enabled': True,
        'min_length': 3,
        'patterns': {
            # Thank you in multiple languages
            'en': [r"^thank you[\s!.]*$", r"^thanks[\s!.]*$"],
            'de': [r"^danke[\s!.]*$", r"^vielen dank[\s!.]*$"],
            'fr': [r"^merci[\s!.]*$"],
            'es': [r"^gracias[\s!.]*$"],
            'nl': [r"^dank je[\s!.]*$", r"^bedankt[\s!.]*$", r"^dank u wel[\s!.]*$"],
            'sk': [r"^ďakujem[\s!.]*$"],
            'pl': [r"^dziekuje.[\s!.]*$"],
            'sv': [r"^tack[\s!.]*$"],
            'cn': [r"^谢谢[\s!.]*$", r"^多谢[\s!.]*$"],
            # Common hallucinations (all languages)
            'common': [
                r"^thanks for watching[\s!.]*$",
                r"^please subscribe[\s!.]*$",
                r"^like and subscribe[\s!.]*$",
                r"^\[music\][\s.]*$",
                r"^\[applause\][\s.]*$",
                r"^subtitles by[\s.]*$",
                r"^www\..*\.com[\s.]*$",
                r"^off[\s.]*$"
            ]
        }
    }
}

class Config:
    """Configuration manager"""
    def __init__(self, config_dict):
        self.data = config_dict
    
    def get(self, path, default=None):
        """Get config value by dot-separated path"""
        keys = path.split('.')
        value = self.data
        for key in keys:
            if isinstance(value, dict):
                value = value.get(key)
            else:
                return default
            if value is None:
                return default
        return value

def load_config(config_path=None):
    """Load configuration from file or use defaults"""
    config = DEFAULT_CONFIG.copy()
    
    if config_path and Path(config_path).exists():
        logger.info(f"Loading configuration from {config_path}")
        try:
            with open(config_path, 'r') as f:
                user_config = yaml.safe_load(f)
                # Deep merge user config into defaults
                _deep_merge(config, user_config)
            logger.info("Configuration loaded successfully")
        except Exception as e:
            logger.error(f"Failed to load config file: {e}")
            logger.info("Using default configuration")
    else:
        if config_path:
            logger.warning(f"Config file not found: {config_path}")
        logger.info("Using default configuration")
    
    return Config(config)

def _deep_merge(base, update):
    """Deep merge update dict into base dict"""
    for key, value in update.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value

# ============================================================================
# JWT AUTHENTICATION
# ============================================================================

class JWTAuthenticator:
    def __init__(self, config):
        self.enabled = config.get('jwt.enabled', True)
        self.audience = config.get('jwt.audience', 'whisper-service')
        self.public_key = None
        
        if self.enabled:
            key_path = config.get('jwt.public_key_path')
            if not key_path:
                logger.error("JWT enabled but no public_key_path specified")
                raise ValueError("Missing JWT public key path")
            
            try:
                with open(key_path, 'r') as f:
                    self.public_key = f.read()
                logger.info(f"Loaded JWT public key from {key_path}")
            except FileNotFoundError:
                logger.error(f"JWT public key not found: {key_path}")
                raise
        else:
            logger.warning("JWT authentication is DISABLED")
    
    async def verify(self, token):
        """Verify JWT token"""
        if not self.enabled:
            return {"iss": "no-auth-mode"}
        
        try:
            payload = jwt.decode(
                token,
                self.public_key,
                algorithms=["RS256"],   # RSA 2048
                audience=self.audience
            )
            logger.info(f"JWT verified - issuer: {payload.get('iss', 'unknown')}")
            return payload
        except jwt.ExpiredSignatureError:
            logger.error("JWT token has expired")
            return None
        except jwt.InvalidAudienceError:
            logger.error(f"JWT audience mismatch - expected {self.audience}")
            return None
        except jwt.InvalidTokenError as e:
            logger.error(f"Invalid JWT token: {e}")
            return None
        except Exception as e:
            logger.error(f"JWT verification error: {e}", exc_info=True)
            return None

# ============================================================================
# HALLUCINATION FILTERING
# ============================================================================

class HallucinationFilter:
    def __init__(self, config):
        self.enabled = config.get('hallucination_filter.enabled', True)
        self.min_length = config.get('hallucination_filter.min_length', 3)
        self.patterns = config.get('hallucination_filter.patterns', {})
        
        # Compile all patterns for efficiency
        self.compiled_patterns = {}
        for lang, patterns in self.patterns.items():
            self.compiled_patterns[lang] = [
                re.compile(pattern, re.IGNORECASE) for pattern in patterns
            ]
        
        total_patterns = sum(len(p) for p in self.compiled_patterns.values())
        logger.info(f"Hallucination filter loaded with {total_patterns} patterns across {len(self.compiled_patterns)} languages")
    
    def is_hallucination(self, text, language='en'):
        """Check if text is a hallucination"""
        if not self.enabled:
            return False
        
        if not text or len(text.strip()) == 0:
            return True
        
        text_lower = text.lower().strip()
        
        # Check length
        if len(text_lower) < self.min_length:
            logger.info(f"Filtered short text: '{text}'")
            return True
        
        # Check language-specific patterns
        lang_code = language.split('-')[0]  # Handle en-US -> en
        
        # Check patterns for detected language
        if lang_code in self.compiled_patterns:
            for pattern in self.compiled_patterns[lang_code]:
                if pattern.match(text_lower):
                    logger.info(f"Filtered hallucination ({lang_code}): '{text}'")
                    return True
        
        # Always check common patterns
        if 'common' in self.compiled_patterns:
            for pattern in self.compiled_patterns['common']:
                if pattern.match(text_lower):
                    logger.info(f"Filtered common hallucination: '{text}'")
                    return True
        
        return False

# ============================================================================
# AUDIO PROCESSING
# ============================================================================

class AudioBuffer:
    def __init__(self, sample_rate):
        self.sample_rate = sample_rate
        self.buffer = bytearray()
        self.client_id = None
        self.language = "en"
    
    def add_chunk(self, audio_data):
        self.buffer.extend(audio_data)
    
    def get_duration_ms(self):
        num_samples = len(self.buffer) // 2
        return (num_samples / self.sample_rate) * 1000
    
    def get_rms_energy(self):
        if len(self.buffer) < 2:
            return 0
        import struct
        try:
            samples = struct.unpack(f'{len(self.buffer)//2}h', bytes(self.buffer))
            sum_squares = sum(s*s for s in samples)
            rms = (sum_squares / len(samples)) ** 0.5
            return rms
        except:
            return 0
    
    def to_wav(self):
        if len(self.buffer) == 0:
            return None
        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, 'wb') as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(self.sample_rate)
            wav_file.writeframes(bytes(self.buffer))
        wav_buffer.seek(0)
        return wav_buffer
    
    def clear(self):
        self.buffer = bytearray()

# ============================================================================
# WHISPER TRANSCRIPTION
# ============================================================================

class WhisperClient:
    def __init__(self, config, hallucination_filter):
        self.url = config.get('whisper.url')
        self.timeout = config.get('whisper.timeout', 30)
        self.auto_detect_code = config.get('language.auto_detect_code', 'auto')
        self.default_language = config.get('language.default', 'en')
        self.silence_threshold = config.get('whisper.silence_threshold', 50)
        self.filter = hallucination_filter
        self.executor = ThreadPoolExecutor(max_workers=2)
        
        logger.info(f"Whisper client configured: {self.url}")
        logger.info(f"Auto-detect language code: '{self.auto_detect_code}'")
    
    async def transcribe(self, audio_buffer):
        """Transcribe audio buffer"""
        try:
            energy = audio_buffer.get_rms_energy()
            
            if energy < self.silence_threshold:
                logger.debug(f"Skipping silence (energy={energy:.0f})")
                return None
            
            wav_data = audio_buffer.to_wav()
            if wav_data is None:
                return None
            
            wav_bytes = wav_data.read()
            
            # Handle language parameter
            language = audio_buffer.language.split('-')[0] if audio_buffer.language else self.default_language
            
            # Check if user selected auto-detection
            if language == self.auto_detect_code:
                # Don't send language parameter - let Whisper auto-detect
                logger.info(f"→ Whisper: {len(wav_bytes)} bytes, energy={energy:.0f}, lang=AUTO-DETECT")
                language_param = None
                detect_language = language  # Use for logging
            else:
                # Force specific language
                logger.info(f"→ Whisper: {len(wav_bytes)} bytes, energy={energy:.0f}, lang={language}")
                language_param = language
                detect_language = language
            
            loop = asyncio.get_event_loop()
            
            def sync_request():
                try:
                    # Build request data
                    data = {
                        'temperature': '0.0',
                        'response-format': 'json'
                    }
                    
                    # Only add language if not auto-detecting
                    if language_param:
                        data['language'] = language_param
                    
                    response = requests.post(
                        self.url,
                        files={'file': ('audio.wav', wav_bytes, 'audio/wav')},
                        data=data,
                        timeout=self.timeout
                    )
                    
                    if response.status_code == 200:
                        return response.json()
                    else:
                        logger.error(f"Whisper error {response.status_code}: {response.text[:200]}")
                        return None
                except Exception as e:
                    logger.error(f"Whisper request error: {e}")
                    return None
            
            result = await loop.run_in_executor(self.executor, sync_request)
            
            if result:
                text = result.get('text', '').strip()
                detected_lang = result.get('language', detect_language)  # Whisper returns detected language
                
                logger.info(f"← Whisper: '{text}' (detected: {detected_lang})")
                
                if self.filter.is_hallucination(text, detected_lang):
                    return None
                
                logger.info(f"✓ TRANSCRIPT ({detected_lang}): '{text}'")
                return text
            return None
        except Exception as e:
            logger.error(f"Transcription error: {e}", exc_info=True)
            return None

# ============================================================================
# WEBSOCKET HANDLER
# ============================================================================

async def handle_client(websocket, path, config, jwt_auth, whisper_client):
    client_addr = websocket.remote_address
    logger.info(f"{'='*70}")
    logger.info(f"NEW CONNECTION from {client_addr}")
    logger.info(f"Path: {path}")
    
    # JWT Authentication
    if jwt_auth.enabled:
        auth_header = websocket.request_headers.get('Authorization', '')
        
        if not auth_header.startswith('Bearer '):
            logger.error(f"Missing Authorization header from {client_addr}")
            await websocket.close(1008, "Unauthorized - Missing Bearer token")
            return
        
        token = auth_header.replace('Bearer ', '').strip()
        payload = await jwt_auth.verify(token)
        
        if not payload:
            logger.error(f"JWT validation failed from {client_addr}")
            await websocket.close(1008, "Unauthorized - Invalid token")
            return
        
        logger.info(f"✓ Authenticated: {payload.get('iss', 'unknown')}")
    
    # Path validation
    if not path.startswith('/streaming-whisper/ws/'):
        logger.error(f"Invalid path: {path}")
        await websocket.close(1008, "Invalid path")
        return
    
    meeting_id = path.split('/')[-1].split('?')[0]
    logger.info(f"Meeting ID: {meeting_id}")
    logger.info(f"{'='*70}")
    
    sample_rate = config.get('whisper.sample_rate')
    chunk_duration_ms = config.get('whisper.chunk_duration_ms')
    
    audio_buffer = AudioBuffer(sample_rate)
    message_count = 0
    transcript_count = 0
    last_transcript = ""
    
    try:
        async for message in websocket:
            message_count += 1
            
            if isinstance(message, bytes):
                if len(message) < 60:
                    continue
                
                # Parse header: "CLIENT_ID|LANGUAGE"
                try:
                    header_bytes = message[:60]
                    header = header_bytes.decode('utf-8', errors='ignore').rstrip('\x00 ')
                    
                    parts = header.split('|')
                    if len(parts) >= 2:
                        participant_id = parts[0].strip()
                        audio_buffer.client_id = participant_id
                        audio_buffer.language = parts[1].strip()
                        
                        if message_count == 1:
                            logger.info(f"Participant: {participant_id}, Language: {audio_buffer.language}")
                
                except Exception as e:
                    logger.error(f"Header parsing error: {e}")
                    continue
                
                # Buffer audio
                audio_data = message[60:]
                if len(audio_data) > 0:
                    audio_buffer.add_chunk(audio_data)
                
                # Process when threshold reached
                if audio_buffer.get_duration_ms() >= chunk_duration_ms:
                    text = await whisper_client.transcribe(audio_buffer)
                    
                    # Send response
                    if text and text != last_transcript:
                        transcript_count += 1
                        response = {
                            "type": "final",
                            "participant_id": audio_buffer.client_id,
                            "text": text,
                            "variance": 0.0
                        }
                        last_transcript = text
                        logger.info(f"→ Jigasi: TRANSCRIPT #{transcript_count}: '{text}'")
                    else:
                        response = {
                            "type": "partial",
                            "participant_id": audio_buffer.client_id,
                            "text": "",
                            "variance": 1.0
                        }
                    
                    try:
                        await websocket.send(json.dumps(response, ensure_ascii=False))
                    except Exception as e:
                        logger.error(f"Send failed: {e}")
                        raise
                    
                    audio_buffer.clear()
            
            elif isinstance(message, str):
                logger.debug(f"Text message: {message}")
    
    except websockets.exceptions.ConnectionClosed as e:
        logger.info(f"Connection closed: code={e.code}, reason={e.reason}")
    except Exception as e:
        logger.error(f"Handler error: {e}", exc_info=True)
    finally:
        logger.info(f"Session ended - Messages: {message_count}, Transcripts: {transcript_count}")

# ============================================================================
# MAIN
# ============================================================================

async def main():
    # Parse command line arguments
    parser = argparse.ArgumentParser(
        description='Jitsi-Whisper WebSocket Bridge',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Configuration file format (YAML):
  server:
    host: "127.0.0.1"
    port: 9000
  whisper:
    url: "http://localhost:8080/inference"
    chunk_duration_ms: 3000
  jwt:
    enabled: true
    public_key_path: "/path/to/key.pem"
    audience: "whisper-service"
  language:
    auto_detect_code: "auto"  # Use this in Jitsi for auto-detection
        """
    )
    parser.add_argument(
        '-c', '--config',
        help='Path to configuration file (YAML)',
        default='/etc/whisper-bridge/config.yaml'
    )
    parser.add_argument(
        '--host',
        help='Server bind address (overrides config)'
    )
    parser.add_argument(
        '--port',
        type=int,
        help='Server port (overrides config)'
    )
    parser.add_argument(
        '--whisper-url',
        help='Whisper server URL (overrides config)'
    )
    parser.add_argument(
        '--no-jwt',
        action='store_true',
        help='Disable JWT authentication'
    )
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Enable debug logging'
    )
    
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # Load configuration
    config = load_config(args.config)
    
    # Apply command-line overrides
    if args.host:
        config.data['server']['host'] = args.host
    if args.port:
        config.data['server']['port'] = args.port
    if args.whisper_url:
        config.data['whisper']['url'] = args.whisper_url
    if args.no_jwt:
        config.data['jwt']['enabled'] = False
    
    # Initialize components
    jwt_auth = JWTAuthenticator(config)
    hallucination_filter = HallucinationFilter(config)
    whisper_client = WhisperClient(config, hallucination_filter)
    
    # Print startup info
    logger.info("="*70)
    logger.info("JITSI-WHISPER BRIDGE")
    logger.info(f"Version: 2.0 with configuration support")
    logger.info(f"Whisper: {config.get('whisper.url')}")
    logger.info(f"Chunk duration: {config.get('whisper.chunk_duration_ms')}ms")
    logger.info(f"JWT: {'ENABLED' if jwt_auth.enabled else 'DISABLED'}")
    logger.info(f"Auto-detect code: '{config.get('language.auto_detect_code')}'")
    logger.info(f"Started: {datetime.now()}")
    logger.info("="*70)
    
    # Start server
    host = config.get('server.host')
    port = config.get('server.port')
    
    server = await websockets.serve(
        lambda ws, path: handle_client(ws, path, config, jwt_auth, whisper_client),
        host,
        port,
        ping_interval=config.get('server.ping_interval'),
        ping_timeout=config.get('server.ping_timeout'),
        max_size=config.get('server.max_size'),
        compression=None
    )
    
    logger.info(f"✓ Server listening on ws://{host}:{port}")
    logger.info("✓ Ready for connections")
    logger.info("")
    
    await server.wait_closed()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\nShutting down...")
