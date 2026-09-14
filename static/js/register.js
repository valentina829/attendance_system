/**
 * register.js
 * Handles:
 *   1. Showing/hiding the student-only fields (Student ID + face capture)
 *      depending on the selected role.
 *   2. Accessing the webcam and capturing a still photo of the student's
 *      face, which is stored as a base64 string in a hidden input and
 *      submitted along with the registration form.
 */

const roleSelect = document.getElementById("role");
const studentFields = document.getElementById("student-fields");

const video = document.getElementById("reg-video");
const canvas = document.getElementById("reg-canvas");
const preview = document.getElementById("reg-preview");
const faceImageInput = document.getElementById("face_image");

const startCameraBtn = document.getElementById("start-camera-btn");
const captureBtn = document.getElementById("capture-btn");
const retakeBtn = document.getElementById("retake-btn");
const captureStatus = document.getElementById("capture-status");

let mediaStream = null;

// ---- Show/hide student-only fields based on role ----
roleSelect.addEventListener("change", () => {
    if (roleSelect.value === "student") {
        studentFields.classList.remove("hidden-block");
    } else {
        studentFields.classList.add("hidden-block");
        stopCamera();
    }
});

// ---- Start the webcam ----
startCameraBtn.addEventListener("click", async () => {
    try {
        mediaStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "user" } });
        video.srcObject = mediaStream;
        video.classList.remove("hidden-block");
        preview.classList.add("hidden-block");
        captureBtn.disabled = false;
        captureStatus.textContent = "Camera is live. Center your face and click 'Capture Photo'.";
    } catch (err) {
        captureStatus.textContent = "Could not access camera: " + err.message;
    }
});

// ---- Capture a still frame from the video into the canvas ----
captureBtn.addEventListener("click", () => {
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    const ctx = canvas.getContext("2d");
    ctx.drawImage(video, 0, 0, canvas.width, canvas.height);

    const dataUrl = canvas.toDataURL("image/png");
    faceImageInput.value = dataUrl;

    preview.src = dataUrl;
    preview.classList.remove("hidden-block");
    video.classList.add("hidden-block");

    captureBtn.disabled = true;
    retakeBtn.classList.remove("hidden-block");
    captureStatus.textContent = "Photo captured. You can retake it if needed, or submit the form.";

    stopCamera();
});

// ---- Retake: clear captured photo and restart the camera ----
retakeBtn.addEventListener("click", async () => {
    faceImageInput.value = "";
    preview.classList.add("hidden-block");
    retakeBtn.classList.add("hidden-block");
    startCameraBtn.click();
});

function stopCamera() {
    if (mediaStream) {
        mediaStream.getTracks().forEach((track) => track.stop());
        mediaStream = null;
    }
}

// ---- Guard against submitting without a captured photo (students only) ----
document.getElementById("register-form").addEventListener("submit", (e) => {
    if (roleSelect.value === "student" && !faceImageInput.value) {
        e.preventDefault();
        captureStatus.textContent = "Please capture a face photo before submitting.";
    }
});
