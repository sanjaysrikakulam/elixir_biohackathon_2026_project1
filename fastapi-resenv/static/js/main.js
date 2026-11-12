// Copy to clipboard function
function copyToClipboard(elementId, button) {
    const element = document.getElementById(elementId);
    const text = element.textContent;
    const successId = elementId + '-success';

    navigator.clipboard.writeText(text).then(() => {
        const successMsg = document.getElementById(successId);
        successMsg.textContent = 'Copied!';
        successMsg.style.opacity = '1';

        setTimeout(() => {
            successMsg.textContent = '';
            successMsg.style.opacity = '0';
        }, 2000);

        const originalBtnHtml = button.innerHTML;
        button.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="color: #28a745;"><polyline points="20 6 9 17 4 12"/></svg>';

        setTimeout(() => {
            button.innerHTML = originalBtnHtml;
        }, 2000);
    }).catch(err => {
        console.error('Failed to copy: ', err);
        const successMsg = document.getElementById(successId);
        successMsg.textContent = 'Copy failed';
        successMsg.style.color = '#dc3545';
    });
}

// Smooth scrolling for anchor links
document.querySelectorAll('a[href^="#"]').forEach(anchor => {
    anchor.addEventListener('click', function (e) {
        e.preventDefault();
        const target = document.querySelector(this.getAttribute('href'));
        if (target) {
            target.scrollIntoView({
                behavior: 'smooth',
                block: 'start'
            });
        }
    });
});
