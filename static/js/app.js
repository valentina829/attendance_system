/**
 * app.js
 * Small behaviours shared by every page:
 *   1. The mobile navigation toggle.
 *   2. A confirmation prompt for forms marked with data-confirm="...",
 *      and a "type the new name" prompt for forms marked data-prompt="...".
 *   3. csrfHeaders(): headers for fetch() POSTs (the server rejects any
 *      POST that doesn't carry the page's anti-forgery token).
 */

const navToggle = document.getElementById("nav-toggle");
const navMenu = document.getElementById("nav-menu");

navToggle.addEventListener("click", () => {
    navMenu.classList.toggle("open");
});

document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (e) => {
        if (!window.confirm(form.dataset.confirm)) {
            e.preventDefault();
        }
    });
});

// Rename buttons: ask for the new name and put it in the form's "name" field.
document.querySelectorAll("form[data-prompt]").forEach((form) => {
    form.addEventListener("submit", (e) => {
        const value = window.prompt(form.dataset.prompt, form.dataset.value || "");
        if (value === null || !value.trim()) {
            e.preventDefault();
            return;
        }
        form.querySelector("input[name=name]").value = value.trim();
    });
});

function csrfHeaders() {
    return {
        "Content-Type": "application/json",
        "X-CSRFToken": document.querySelector('meta[name="csrf-token"]').content,
    };
}
