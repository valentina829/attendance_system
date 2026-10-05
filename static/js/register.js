/**
 * register.js
 * Face photo picker for a form with id="face-form" (used by the
 * registration page and by the "face photo" page). The photo can be
 *   - captured with the webcam, or
 *   - uploaded from the device (file / phone gallery).
 * Either way it ends up as a base64 JPEG in a hidden input and is
 * submitted along with the form.
 */

const video = document.getElementById("reg-video");
const canvas = document.getElementById("reg-canvas");
const preview = document.getElementById("reg-preview");
const faceImageInput = document.getElementById("face_image");
const faceFileInput = document.getElementById("face-file");

const startCameraBtn = document.getElementById("start-camera-btn");
const captureBtn = document.getElementById("capture-btn");
const uploadBtn = document.getElementById("upload-btn");
const captureStatus = document.getElementById("capture-status");

const MAX_PHOTO_SIDE = 1000;   // photos are scaled down to this before upload

let mediaStream = null;

// ---- Draws a video frame or an image into the canvas (scaled down)
//      and stores the result as the photo to submit. ----
function usePhoto(source, width, height) {
    const scale = Math.min(1, MAX_PHOTO_SIDE / Math.max(width, height));
    canvas.width = Math.round(width * scale);
    canvas.height = Math.round(height * scale);
    canvas.getContext("2d").drawImage(source, 0, 0, canvas.width, canvas.height);

    const dataUrl = canvas.toDataURL("image/jpeg", 0.92);
    faceImageInput.value = dataUrl;

    preview.src = dataUrl;
    preview.classList.remove("hidden-block");
    video.classList.add("hidden-block");
}

// ---- Start the webcam ----
startCameraBtn.addEventListener("click", async () => {
    try {
        mediaStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "user" } });
        video.srcObject = mediaStream;
        video.classList.remove("hidden-block");
        preview.classList.add("hidden-block");
        faceImageInput.value = "";
        captureBtn.disabled = false;
        startCameraBtn.textContent = "Use camera";
        captureStatus.textContent = "Camera is live. Center your face and click 'Capture photo'.";
    } catch (err) {
        captureStatus.textContent = "Could not access the camera: " + err.message +
            ". You can upload a photo instead.";
    }
});

// ---- Capture a still frame from the video ----
captureBtn.addEventListener("click", () => {
    usePhoto(video, video.videoWidth, video.videoHeight);
    stopCamera();
    captureBtn.disabled = true;
    startCameraBtn.textContent = "Retake";
    captureStatus.textContent = "Photo captured. You can retake it, or submit the form.";
});

// ---- Upload a photo from the device ----
uploadBtn.addEventListener("click", () => faceFileInput.click());

faceFileInput.addEventListener("change", () => {
    const file = faceFileInput.files[0];
    faceFileInput.value = "";
    if (!file) return;

    const image = new Image();
    const objectUrl = URL.createObjectURL(file);
    image.onload = () => {
        stopCamera();
        captureBtn.disabled = true;
        usePhoto(image, image.naturalWidth, image.naturalHeight);
        URL.revokeObjectURL(objectUrl);
        captureStatus.textContent = "Photo selected. You can choose another one, or submit the form.";
    };
    image.onerror = () => {
        URL.revokeObjectURL(objectUrl);
        captureStatus.textContent = "That file could not be opened as a picture. Please choose a JPG or PNG photo.";
    };
    image.src = objectUrl;
});

function stopCamera() {
    if (mediaStream) {
        mediaStream.getTracks().forEach((track) => track.stop());
        mediaStream = null;
    }
}

// ---- Guard against submitting without a photo ----
document.getElementById("face-form").addEventListener("submit", (e) => {
    if (!faceImageInput.value) {
        e.preventDefault();
        captureStatus.textContent = "Please take or upload a face photo before submitting.";
    }
});
