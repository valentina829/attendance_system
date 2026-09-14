/**
 * student.js
 * Drives the two-step check-in flow on the student dashboard:
 *
 *   STEP 1 - QR scan:
 *     Uses the Html5Qrcode library to open the camera and decode a QR
 *     code. The QR payload is JSON: {"session_id": <int>, "token": "<str>"}.
 *     That payload is POSTed to /scan-qr. On success, Step 2 is revealed.
 *
 *   STEP 2 - Face verification:
 *     Opens the camera again, captures a still frame, and POSTs it as a
 *     base64 image to /verify-face. On success, attendance is marked.
 */

// ------------------------- STEP 1: QR SCAN -------------------------
const startQrBtn = document.getElementById("start-qr-btn");
const qrStatus = document.getElementById("qr-status");
const stepFace = document.getElementById("step-face");

let html5QrCode = null;

startQrBtn.addEventListener("click", async () => {
    startQrBtn.disabled = true;
    qrStatus.textContent = "Starting camera...";

    html5QrCode = new Html5Qrcode("qr-reader");

    try {
        await html5QrCode.start(
            { facingMode: "environment" }, // prefer back camera on phones
            { fps: 10, qrbox: { width: 240, height: 240 } },
            onQrScanSuccess,
            () => { /* ignore per-frame decode failures */ }
        );
        qrStatus.textContent = "Point the camera at the QR code shown by your professor.";
    } catch (err) {
        qrStatus.textContent = "Could not start camera: " + err;
        startQrBtn.disabled = false;
    }
});

async function onQrScanSuccess(decodedText) {
    // Prevent the callback firing multiple times for the same frame burst
    if (html5QrCode) {
        await html5QrCode.stop().catch(() => {});
    }

    let payload;
    try {
        payload = JSON.parse(decodedText);
    } catch (e) {
        qrStatus.textContent = "Unrecognized QR code format.";
        startQrBtn.disabled = false;
        return;
    }

    qrStatus.textContent = "Verifying QR code...";

    try {
        const res = await fetch("/scan-qr", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ session_id: payload.session_id, token: payload.token }),
        });
        const data = await res.json();

        if (data.success) {
            qrStatus.textContent = "✅ " + data.message;
            stepFace.classList.remove("hidden-block");
            stepFace.scrollIntoView({ behavior: "smooth" });
        } else {
            qrStatus.textContent = "❌ " + data.message;
            startQrBtn.disabled = false;
        }
    } catch (err) {
        qrStatus.textContent = "Network error while verifying QR code.";
        startQrBtn.disabled = false;
    }
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
        faceStatus.textContent = "Camera is live. Look directly at the camera and click 'Verify & Mark Present'.";
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
    const dataUrl = faceCanvas.toDataURL("image/png");

    try {
        const res = await fetch("/verify-face", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
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
