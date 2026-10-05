"""
qr_utils.py
-----------
Small helper to generate a QR code image (as a base64 PNG data-URL) that
can be embedded directly into an <img src="..."> tag with no need to
save a physical file on disk.
"""

import base64
import io

import qrcode


def generate_qr_base64(checkin_url: str) -> str:
    """
    Encodes the session's check-in URL (which carries session_id + token)
    inside a QR code and returns a base64 data-URL string ready to use as
    an <img src="...">.

    Encoding a full URL (rather than raw JSON) means the QR works both
    with the in-app scanner AND with a phone's normal camera app, which
    simply opens the link in the browser.
    """
    payload = checkin_url

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
