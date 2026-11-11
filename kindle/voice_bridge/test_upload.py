import json
import math
import os
import struct
import sys
import tempfile
import wave


SAMPLE_RATE = 8000
SAMPLE_COUNT = 2000
TIMEOUT_SECONDS = 30


def _read_config(path):
    with open(path, "r", encoding="utf-8") as config_file:
        return config_file.read()


def _write_sample(sample_file):
    with wave.open(sample_file, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(SAMPLE_RATE)
        samples = (
            int(8000 * math.sin(2 * math.pi * 440 * index / SAMPLE_RATE))
            for index in range(SAMPLE_COUNT)
        )
        wav_file.writeframes(b"".join(struct.pack("<h", sample) for sample in samples))


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if len(argv) != 1:
        print("Provide one configuration file path.", file=sys.stderr)
        return 1

    try:
        text = _read_config(argv[0])
    except Exception:
        print("Could not read the configuration file.", file=sys.stderr)
        return 1

    try:
        config = json.loads(text)
    except (ValueError, RecursionError):
        print("The configuration is invalid.", file=sys.stderr)
        return 1

    if (not isinstance(config, dict) or
            not isinstance(config.get("server_url"), str) or
            not config["server_url"] or
            not isinstance(config.get("token"), str) or
            not config["token"]):
        print("The configuration is invalid.", file=sys.stderr)
        return 1

    try:
        import uploader
    except Exception:
        print("Could not load the uploader.", file=sys.stderr)
        return 1

    sample_path = None
    error_message = None
    cleanup_failed = False
    try:
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".wav", dir="/tmp",
                                         delete=False) as sample_file:
            sample_path = sample_file.name
            _write_sample(sample_file)
        try:
            uploader.upload_recording(
                sample_path, config["server_url"], config["token"],
                timeout=TIMEOUT_SECONDS, allow_insecure_lan=True)
        except uploader.UploadError as error:
            error_message = "Upload failed: {}".format(error)
        except Exception:
            error_message = "The upload failed. The server may have stored the sample."
    except Exception:
        error_message = "Could not create the WAV file."
    finally:
        if sample_path is not None:
            try:
                os.remove(sample_path)
            except OSError:
                cleanup_failed = True

    if error_message:
        print(error_message, file=sys.stderr)
    if cleanup_failed:
        print("Could not delete the temporary WAV file.", file=sys.stderr)
    if error_message or cleanup_failed:
        return 1
    print("The upload succeeded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
