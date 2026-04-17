/**
 * Handles the visual transition and submission lock for the authorization terminal.
 * @param {Event} event - The click event from the submission button.
 */
function login(event) {
    const btn = event.currentTarget;
    const form = btn.closest('form');

    // Validate form requirements before triggering visual effects
    if (!form.checkValidity()) {
        return; // Allow the browser to handle native "required" field alerts
    }

    // 1. Visual Feedback: Transition to "Processing" state
    btn.style.color = "#ff4d4d";
    btn.style.borderColor = "#ff4d4d";
    btn.style.textShadow = "0 0 10px #ff4d4d";
    btn.style.transform = "scale(0.95)";
    
    // Update button text to reflect terminal status
    const btnText = btn.querySelector('.btn-text');
    if (btnText) {
        btnText.innerText = "AUTHORIZING...";
    }

    // 2. Security Lock: Prevent redundant requests (Double-click spam)
    // A 10ms delay ensures the browser successfully dispatches the POST request 
    // before the UI element becomes non-interactive.
    setTimeout(() => {
        btn.style.pointerEvents = "none";
        btn.style.opacity = "0.7";
        btn.style.cursor = "not-allowed";
    }, 10);
}