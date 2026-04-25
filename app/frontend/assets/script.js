const form = document.querySelector('#form');
const urlInput = document.querySelector('#url');
const aliasInput = document.querySelector('#alias');
const expiresAtInput = document.querySelector('#expiresAt');
const submitButton = document.querySelector('.submit-button');
const message = document.querySelector('#toast');
const rulesButton = document.querySelector('#rulesButton');
const apiButton = document.querySelector('#apiButton');
const rulesDialog = document.querySelector('#rulesDialog');
const closeRules = document.querySelector('#closeRules');

let shortLinkIsReady = false;

function addProtocol(url) {
    return /^https?:\/\//i.test(url) ? url : 'https://' + url;
}

async function copyText(text) {
    try {
        await navigator.clipboard.writeText(text);
    } catch (error) {
        urlInput.select();
        document.execCommand('copy');
    }
}

function buildPayload() {
    const originalUrl = urlInput.value.trim();
    if (!looksLikeUrl(originalUrl)) {
        throw new Error('Введите корректную ссылку, например example.com или https://example.com/page.');
    }
    const payload = {
        original_url: addProtocol(originalUrl),
    };
    if (aliasInput.value.trim()) {
        payload.custom_alias = aliasInput.value.trim();
    }
    if (expiresAtInput.value) {
        payload.expires_at = new Date(expiresAtInput.value).toISOString();
    }
    return payload;
}

function looksLikeUrl(value) {
    if (!value || /\s/.test(value)) {
        return false;
    }
    try {
        const parsed = new URL(addProtocol(value));
        const host = parsed.hostname;
        return parsed.protocol === 'http:' || parsed.protocol === 'https:'
            ? host.includes('.') && !host.startsWith('.') && !host.endsWith('.')
            : false;
    } catch (error) {
        return false;
    }
}

async function createShortLink() {
    const response = await fetch('/api/v1/links', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(buildPayload()),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
        throw new Error(formatApiError(data));
    }
    return data.short_url;
}

function formatApiError(data) {
    const detail = data.detail;
    if (!detail) {
        return 'Не удалось создать ссылку.';
    }
    if (typeof detail === 'string') {
        return detail;
    }
    if (Array.isArray(detail)) {
        return detail.map((item) => item.msg || JSON.stringify(item)).join('; ');
    }
    return detail.message || JSON.stringify(detail);
}

async function handleFormSubmit(event) {
    event.preventDefault();

    if (shortLinkIsReady) {
        await copyText(urlInput.value);
        message.textContent = 'Скопировано.';
        return;
    }

    submitButton.disabled = true;
    submitButton.textContent = 'Ждем...';
    try {
        const shortLink = await createShortLink();
        urlInput.value = shortLink;
        aliasInput.value = '';
        expiresAtInput.value = '';
        submitButton.textContent = 'Копировать';
        shortLinkIsReady = true;
        message.textContent = 'Готово: ссылка создана на сервере.';
    } catch (error) {
        submitButton.textContent = 'Сократить';
        message.textContent = error.message;
    } finally {
        submitButton.disabled = false;
    }
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
aliasInput.addEventListener('input', resetButton);
expiresAtInput.addEventListener('input', resetButton);
rulesButton.addEventListener('click', () => rulesDialog.showModal());
closeRules.addEventListener('click', () => rulesDialog.close());
apiButton.addEventListener('click', () => {
    location.href = '/docs';
});
