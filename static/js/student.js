/**
 * student.js
 * Drives the two-step check-in flow on the student dashboard:
 *
 *   STEP 1 - QR scan:
 *     Uses the Html5Qrcode library to open the camera and decode a QR
 *     code (or to read one from a photo of it). The QR holds a check-in
 *     URL carrying session_id + token. Those are POSTed to /scan-qr.
 *     On success, Step 2 is revealed.
 *
 *   STEP 2 - Face verification:
 *     Opens the camera again, captures a still frame, and POSTs it as a
 *     base64 image to /verify-face. On success, attendance is marked.
 */

// ------------------------- STEP 1: QR SCAN -------------------------
const startQrBtn = document.getElementById("start-qr-btn");
const qrPhotoBtn = document.getElementById("qr-photo-btn");
const qrFileInput = document.getElementById("qr-file");
const qrStatus = document.getElementById("qr-status");
const stepFace = document.getElementById("step-face");

let html5QrCode = null;
let qrHandled = false;   // a code was decoded and is being verified

function getScanner() {
    if (typeof Html5Qrcode === "undefined") {
        qrStatus.textContent = "The QR scanner could not be loaded. Please reload the page.";
        return null;
    }
    if (!html5QrCode) {
        html5QrCode = new Html5Qrcode("qr-reader", {
            formatsToSupport: [Html5QrcodeSupportedFormats.QR_CODE],
            // Use the browser's built-in detector where it exists (faster on phones).
            experimentalFeatures: { useBarCodeDetectorIfSupported: true },
            verbose: false,
        });
    }
    return html5QrCode;
}

function setQrButtons(enabled) {
    startQrBtn.disabled = !enabled;
    qrPhotoBtn.disabled = !enabled;
}

startQrBtn.addEventListener("click", async () => {
    const scanner = getScanner();
    if (!scanner) return;

    setQrButtons(false);
    qrHandled = false;
    qrStatus.textContent = "Starting camera...";

    try {
        await scanner.start(
            { facingMode: "environment" }, // prefer back camera on phones
            {
                fps: 10,
                // Scan a square covering most of the camera picture.
                qrbox: (width, height) => {
                    const side = Math.floor(Math.min(width, height) * 0.8);
                    return { width: side, height: side };
                },
            },
            onQrScanSuccess,
            () => { /* ignore per-frame decode failures */ }
        );
        qrStatus.textContent = "Point the camera at the QR code shown by your professor.";
    } catch (err) {
        qrStatus.textContent = "Could not start the camera: " + err +
            " You can use 'Scan from a photo' instead.";
        setQrButtons(true);
    }
});

// ---- Fallback: read the QR code from a photo taken with the phone ----
qrPhotoBtn.addEventListener("click", () => qrFileInput.click());

qrFileInput.addEventListener("change", async () => {
    const file = qrFileInput.files[0];
    qrFileInput.value = "";
    const scanner = getScanner();
    if (!file || !scanner) return;

    setQrButtons(false);
    qrStatus.textContent = "Reading the photo...";
    try {
        const decodedText = await scanner.scanFile(file, false);
        await verifyQrText(decodedText);
    } catch (err) {
        qrStatus.textContent = "❌ No QR code was found in that photo. Please try a sharper, closer one.";
        setQrButtons(true);
    }
});

async function onQrScanSuccess(decodedText) {
    // The callback can fire for several frames in a row: handle the first only.
    if (qrHandled) return;
    qrHandled = true;

    await html5QrCode.stop().catch(() => {});
    html5QrCode.clear();
    await verifyQrText(decodedText);
}

async function verifyQrText(decodedText) {
    const payload = parseQrPayload(decodedText);
    if (!payload) {
        qrStatus.textContent = "❌ That is not a lecture QR code.";
        setQrButtons(true);
        return;
    }

    qrStatus.textContent = "Verifying QR code...";

    try {
        const res = await fetch("/scan-qr", {
            method: "POST",
            headers: csrfHeaders(),
            body: JSON.stringify({ session_id: payload.session_id, token: payload.token }),
        });
        const data = await res.json();

        if (data.success) {
            qrStatus.textContent = "✅ " + data.message;
            stepFace.classList.remove("hidden-block");
            stepFace.scrollIntoView({ behavior: "smooth" });
        } else {
            qrStatus.textContent = "❌ " + data.message;
            setQrButtons(true);
        }
    } catch (err) {
        qrStatus.textContent = "Network error while verifying QR code.";
        setQrButtons(true);
    }
}

/**
 * Extracts {session_id, token} from the text inside a QR code.
 * The QR normally holds a check-in URL (.../checkin?session_id=..&token=..);
 * the older JSON format {"session_id":..,"token":".."} is still accepted.
 * Returns null if the text is neither.
 */
function parseQrPayload(text) {
    try {
        const url = new URL(text);
        const sessionId = parseInt(url.searchParams.get("session_id"), 10);
        const token = url.searchParams.get("token");
        if (!Number.isNaN(sessionId) && token) {
            return { session_id: sessionId, token: token };
        }
    } catch (e) { /* not a URL - try JSON below */ }

    try {
        const json = JSON.parse(text);
        if (json && json.session_id != null && json.token) {
            return { session_id: json.session_id, token: json.token };
        }
    } catch (e) { /* not JSON either */ }

    return null;
}

// ------------------------- STEP 2: FACE VERIFY -------------------------
const faceVideo = document.getElementById("face-video");
const faceCanvas = document.getElementById("face-canvas");
const startFaceCameraBtn = document.getElementById("start-face-camera-btn");
const verifyFaceBtn = document.getElementById("verify-face-btn");
const faceStatus = document.getElementById("face-status");

let faceStream = null;

startFaceCameraBtn.addEventListener("click", async () => {
    try {
        faceStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "user" } });
        faceVideo.srcObject = faceStream;
        verifyFaceBtn.disabled = false;
        faceStatus.textContent = "Camera is live. Look directly at the camera and click 'Verify & mark present'.";
    } catch (err) {
        faceStatus.textContent = "Could not access camera: " + err.message;
    }
});

verifyFaceBtn.addEventListener("click", async () => {
    verifyFaceBtn.disabled = true;
    faceStatus.textContent = "Verifying your face, please wait...";

    faceCanvas.width = faceVideo.videoWidth;
    faceCanvas.height = faceVideo.videoHeight;
    const ctx = faceCanvas.getContext("2d");
    ctx.drawImage(faceVideo, 0, 0, faceCanvas.width, faceCanvas.height);
    const dataUrl = faceCanvas.toDataURL("image/jpeg", 0.92);

    try {
        const res = await fetch("/verify-face", {
            method: "POST",
            headers: csrfHeaders(),
            body: JSON.stringify({ image: dataUrl }),
        });
        const data = await res.json();

        if (data.success) {
            faceStatus.textContent = "✅ " + data.message + " (" + data.timestamp + ")";
            stopFaceCamera();
            setTimeout(() => window.location.reload(), 1500);
        } else {
            faceStatus.textContent = "❌ " + data.message;
            verifyFaceBtn.disabled = false;
        }
    } catch (err) {
        faceStatus.textContent = "Network error while verifying face.";
        verifyFaceBtn.disabled = false;
    }
});

function stopFaceCamera() {
    if (faceStream) {
        faceStream.getTracks().forEach((track) => track.stop());
        faceStream = null;
    }
}
