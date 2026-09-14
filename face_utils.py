"""
face_utils.py
-------------
Wraps the `face_recognition` library (built on dlib) with two simple
helper functions used by app.py:

    1. extract_face_encoding(image_bytes) -> 128-d encoding or None
    2. compare_faces(known_encoding, new_encoding) -> (is_match, distance)

Keeping this logic in its own module keeps app.py focused on routing/HTTP
concerns, and makes the AI part easy to test or swap out independently.
"""

import base64
import io
import json

import face_recognition
import numpy as np
from PIL import Image

# How strict the face match must be. face_recognition returns a "distance"
# between two face encodings (lower = more similar). 0.6 is the library's
# own recommended default; we use a slightly stricter 0.5 to reduce the
# chance of a false "present" mark in an attendance system.
FACE_MATCH_TOLERANCE = 0.5


def decode_base64_image(data_url: str) -> np.ndarray:
    """
    Converts a base64 data-URL (e.g. "data:image/png;base64,AAAA...")
    coming from the browser's <canvas>.toDataURL() into an RGB numpy
    array that face_recognition can work with.
    """
    if "," in data_url:
        # Strip the "data:image/png;base64," header if present
        data_url = data_url.split(",", 1)[1]

    image_bytes = base64.b64decode(data_url)
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    return np.array(image)


def extract_face_encoding(image_array: np.ndarray):
    """
    Detects a face in the given image and returns its 128-dimensional
    face encoding (a numeric "fingerprint" of the face).

    Returns:
        - numpy array of shape (128,) if exactly one face is found
        - None if zero faces are found
        - raises ValueError if MORE than one face is found (ambiguous —
          we don't want to register/verify with a group photo)
    """
    face_locations = face_recognition.face_locations(image_array)

    if len(face_locations) == 0:
        return None
    if len(face_locations) > 1:
        raise ValueError("Multiple faces detected. Please use a photo with only one face.")

    encodings = face_recognition.face_encodings(image_array, known_face_locations=face_locations)
    return encodings[0]


def encoding_to_json(encoding: np.ndarray) -> str:
    """Serializes a numpy face encoding to a JSON string for DB storage."""
    return json.dumps(encoding.tolist())


def encoding_from_json(encoding_json: str) -> np.ndarray:
    """Deserializes a JSON string back into a numpy face encoding."""
    return np.array(json.loads(encoding_json))


def compare_faces(known_encoding: np.ndarray, new_encoding: np.ndarray):
    """
    Compares a stored (known) face encoding against a freshly captured one.

    Returns:
        (is_match: bool, distance: float)
        distance is included so the frontend/logs can show a confidence
        score if desired (0.0 = identical, higher = more different).
    """
    distance = face_recognition.face_distance([known_encoding], new_encoding)[0]
    is_match = distance <= FACE_MATCH_TOLERANCE
    return bool(is_match), float(distance)
