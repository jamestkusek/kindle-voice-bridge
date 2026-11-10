import argparse
import http.client
import ipaddress
import json
import os
import struct
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


DEFAULT_TIMEOUT = 30
MAX_RESPONSE_BYTES = 65536


class UploadError(Exception):
    def __init__(self, message, status=None, code=None):
        super(UploadError, self).__init__(message)
        self.status = status
        self.code = code


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_ERROR_MESSAGES = {
    "unauthorized": "check the token",
    "payload_too_large": "recording too long",
    "duration_exceeds_limit": "recording too long",
    "unsupported_media_type": "the recording format was rejected",
    "invalid_wav": "the recording format was rejected",
    "unsupported_wav_format": "the recording format was rejected",
    "storage_error": "server could not store the recording",
    "internal_error": "server could not store the recording",
    "not_found": "server URL is wrong",
    "method_not_allowed": "server URL is wrong",
}


def _upload_url(base_url, allow_insecure_lan):
    if (not isinstance(base_url, str) or not base_url or
            any(ord(c) <= 32 or ord(c) == 127 for c in base_url) or
            any(c in base_url for c in "?#\\")):
        raise UploadError("Invalid server base URL")
    try:
        parts = urlsplit(base_url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        raise UploadError("Invalid server base URL")
    if (parts.scheme not in ("http", "https") or not host or
            parts.username is not None or parts.password is not None or
            (port is not None and port == 0)):
        raise UploadError("Invalid server base URL")
    if parts.scheme == "http" and not allow_insecure_lan:
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host.lower() == "localhost"
        if not loopback:
            raise UploadError("Plain HTTP outside loopback requires explicit LAN approval")
    return base_url.rstrip("/") + "/v1/recordings"


def _validate_wav(body):
    if len(body) < 44 or body[:4] != b"RIFF" or body[8:12] != b"WAVE":
        raise UploadError("The sample is not a valid PCM WAV file")
    end = 8 + struct.unpack_from("<I", body, 4)[0]
    if end < 12 or end > len(body):
        raise UploadError("The sample is not a valid PCM WAV file")
    position = 12
    fmt = None
    data_size = None
    while position < end:
        if end - position < 8:
            raise UploadError("The sample is not a valid PCM WAV file")
        chunk_id = body[position:position + 4]
        size = struct.unpack_from("<I", body, position + 4)[0]
        start = position + 8
        position = start + size + (size % 2)
        if position > end:
            raise UploadError("The sample is not a valid PCM WAV file")
        if chunk_id == b"fmt ":
            if fmt is not None or size < 16:
                raise UploadError("The sample is not a valid PCM WAV file")
            fmt = struct.unpack_from("<HHIIHH", body, start)
        elif chunk_id == b"data":
            if fmt is None or data_size is not None:
                raise UploadError("The sample is not a valid PCM WAV file")
            data_size = size
    if fmt is None or data_size is None:
        raise UploadError("The sample is not a valid PCM WAV file")
    audio_format, channels, rate, byte_rate, align, bits = fmt
    if (audio_format != 1 or channels != 1 or bits not in (8, 16) or
            not 8000 <= rate <= 48000):
        raise UploadError("The sample must be mono PCM WAV, 8 or 16 bit, at 8-48 kHz")
    if (align != channels * bits // 8 or byte_rate != rate * align or
            data_size == 0 or data_size % align):
        raise UploadError("The sample is not a valid PCM WAV file")


def _parse_response(response):
    raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, RuntimeError):
        return None


def _server_error(status, payload):
    code = None
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        value = payload["error"].get("code")
        if isinstance(value, str):
            code = value
    message = _ERROR_MESSAGES.get(code, "upload rejected")
    return UploadError("HTTP {}: {}".format(status, message), status, code)


def upload_recording(path, base_url, token, timeout=DEFAULT_TIMEOUT,
                     allow_insecure_lan=False):
    if (not isinstance(token, str) or not token or
            any(ord(c) < 33 or ord(c) > 126 for c in token)):
        raise UploadError("Bearer token must be non-empty printable ASCII without spaces")
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or
            not 0 < timeout <= 300):
        raise UploadError("Timeout must be greater than 0 and at most 300 seconds")
    if not isinstance(allow_insecure_lan, bool):
        raise UploadError("LAN approval must be a boolean")
    url = _upload_url(base_url, allow_insecure_lan)
    try:
        with open(path, "rb") as sample:
            body = sample.read()
    except (OSError, ValueError, TypeError):
        raise UploadError("Could not read the sample file")
    _validate_wav(body)
    try:
        request = Request(url, data=body, headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "audio/wav",
            "Accept": "application/json",
            "Content-Length": str(len(body)),
        }, method="POST")
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        try:
            response = opener.open(request, timeout=timeout)
        except HTTPError as error:
            response = error
        with response:
            status = response.getcode()
            payload = _parse_response(response)
        if status != 201:
            raise _server_error(status, payload)
        if (not isinstance(payload, dict) or not isinstance(payload.get("id"), str)
                or not payload["id"].strip()):
            raise UploadError("Server returned an invalid 201 response; the recording may be stored", status=201)
        return payload
    except UploadError:
        raise
    except (OSError, URLError, ValueError, UnicodeError, http.client.HTTPException):
        raise UploadError("Upload failed or timed out; the server may have stored the recording") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Upload one PCM WAV recording")
    parser.add_argument("path", help="path to the WAV sample")
    args = parser.parse_args(argv)
    base_url = os.environ.get("KVB_SERVER_URL")
    token = os.environ.get("KVB_TOKEN")
    if not base_url or not token:
        print("Set KVB_SERVER_URL and KVB_TOKEN before uploading", file=sys.stderr)
        return 1
    try:
        result = upload_recording(
            args.path, base_url, token,
            allow_insecure_lan=os.environ.get("KVB_ALLOW_INSECURE_LAN") == "1")
    except UploadError as error:
        print("Upload failed: {}".format(error), file=sys.stderr)
        return 1
    recording_id = result["id"]
    if token in recording_id:
        print("Recording stored (ID omitted to protect the token)")
    else:
        print("Recording stored: {}".format(json.dumps(recording_id)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
