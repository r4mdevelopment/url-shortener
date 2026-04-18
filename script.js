const form = document.querySelector('#form');
const urlInput = document.querySelector('#url');
const submitButton = document.querySelector('.submit-button');
const message = document.querySelector('#toast');
const rulesButton = document.querySelector('#rulesButton');
const apiButton = document.querySelector('#apiButton');
const rulesDialog = document.querySelector('#rulesDialog');
const closeRules = document.querySelector('#closeRules');

let shortLinkIsReady = false;

function makeShortCode(url) {
    let hash = 0;

    // Простая генерация одинакового короткого кода для одной и той же ссылки
    for (let i = 0; i < url.length; i++) {
        hash = (hash * 31 + url.charCodeAt(i)) >>> 0;
    }

    return hash.toString(36).slice(0, 7);
}

function addProtocol(url) {
    const hasProtocol = /^https?:\/\//i.test(url);

    if (hasProtocol) {
        return url;
    }

    return 'https://' + url;
}

function makeShortLink(url) {
    const shortCode = makeShortCode(url);

    if (location.host) {
        return location.origin + '/' + shortCode;
    }

    return 'short.ly/' + shortCode;
}

async function copyText(text) {
    try {
        await navigator.clipboard.writeText(text);
    } catch (error) {
        urlInput.select();
        document.execCommand('copy');
    }
}

async function handleFormSubmit(event) {
    event.preventDefault();

    if (shortLinkIsReady) {
        await copyText(urlInput.value);
        message.textContent = 'Скопировано.';
        return;
    }

    const originalUrl = addProtocol(urlInput.value.trim());
    const shortLink = makeShortLink(originalUrl);

    urlInput.value = shortLink;
    submitButton.textContent = 'Копировать';
    shortLinkIsReady = true;
    message.textContent = 'Готово: ссылка стала короче.';
}

function resetButton() {
    if (!shortLinkIsReady) {
        return;
    }

    shortLinkIsReady = false;
    submitButton.textContent = 'Сократить';
    message.textContent = '';
}

form.addEventListener('submit', handleFormSubmit);
urlInput.addEventListener('input', resetButton);
rulesButton.addEventListener('click', () => {
    rulesDialog.showModal();
});
closeRules.addEventListener('click', () => {
    rulesDialog.close();
});
apiButton.addEventListener('click', () => {
    location.href = '/docx';
});
