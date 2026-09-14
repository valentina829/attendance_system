"""
qr_utils.py
-----------
Small helper to generate a QR code image (as a base64 PNG data-URL) that
can be embedded directly into an <img src="..."> tag with no need to
save a physical file on disk.
"""

import base64
import io
import json

import qrcode


def generate_qr_base64(session_id: int, token: str) -> str:
    """
    Encodes {session_id, token} as JSON inside a QR code and returns a
    base64 data-URL string ready to use as an <img src="...">.

    Encoding both fields (not just the token) lets the student's browser
    quickly look up which session it belongs to without an extra request.
    """
    payload = json.dumps({"session_id": session_id, "token": token})

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=4,
    )
    qr.add_data(payload)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("utf-8")

    return f"data:image/png;base64,{encoded}"
