(function () {
    const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || '';
    const publicKey = document.querySelector('meta[name="push-public-key"]')?.content || '';
    const POLL_INTERVAL_MS = 15000;
    let serviceWorkerRegistration;

    function urlBase64ToUint8Array(value) {
        const padding = '='.repeat((4 - value.length % 4) % 4);
        const base64 = (value + padding).replace(/-/g, '+').replace(/_/g, '/');
        const rawData = window.atob(base64);
        return Uint8Array.from([...rawData].map((character) => character.charCodeAt(0)));
    }

    async function registerServiceWorker() {
        if (!('serviceWorker' in navigator)) return null;
        try {
            serviceWorkerRegistration = await navigator.serviceWorker.register(
                new URL('service-worker.js', document.baseURI).pathname
            );
            return serviceWorkerRegistration;
        } catch (error) {
            console.warn('Não foi possível registrar o PWA.', error);
            return null;
        }
    }

    async function updateNotificationUi() {
        const button = document.getElementById('notificationButton');
        const status = document.getElementById('notificationStatus');
        if (!button) return;
        if (!('PushManager' in window) || !('Notification' in window)) {
            button.disabled = true;
            status.textContent = 'Este navegador não oferece suporte a notificações Web Push.';
            return;
        }
        const registration = serviceWorkerRegistration || await registerServiceWorker();
        const subscription = registration && await registration.pushManager.getSubscription();
        if (subscription) {
            button.textContent = 'Desativar notificações';
            button.dataset.enabled = 'true';
            status.textContent = 'Notificações ativas neste dispositivo.';
        } else {
            button.textContent = 'Ativar notificações';
            button.dataset.enabled = 'false';
            status.textContent = Notification.permission === 'denied'
                ? 'A permissão foi bloqueada nas configurações do navegador.'
                : '';
        }
    }

    async function toggleNotifications() {
        const button = document.getElementById('notificationButton');
        const registration = serviceWorkerRegistration || await registerServiceWorker();
        if (!registration || !publicKey) return;
        button.disabled = true;
        try {
            const current = await registration.pushManager.getSubscription();
            if (current) {
                await fetch(new URL('api/push/unsubscribe', document.baseURI), {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken},
                    body: JSON.stringify({endpoint: current.endpoint})
                });
                await current.unsubscribe();
            } else {
                const permission = await Notification.requestPermission();
                if (permission !== 'granted') throw new Error('Permissão de notificação não concedida.');
                const subscription = await registration.pushManager.subscribe({
                    userVisibleOnly: true,
                    applicationServerKey: urlBase64ToUint8Array(publicKey)
                });
                const response = await fetch(new URL('api/push/subscribe', document.baseURI), {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken},
                    body: JSON.stringify(subscription)
                });
                if (!response.ok) throw new Error('O servidor não aceitou a assinatura.');
            }
        } catch (error) {
            const status = document.getElementById('notificationStatus');
            if (status) status.textContent = error.message;
        } finally {
            button.disabled = false;
            await updateNotificationUi();
        }
    }

    function setupUpload() {
        const form = document.getElementById('uploadForm');
        const dropZone = document.getElementById('dropZone');
        const fileInput = document.getElementById('fileInput');
        const fileList = document.getElementById('fileList');
        const submit = document.getElementById('uploadButton');
        if (!form || !dropZone || !fileInput) return;

        function updateFileList() {
            const files = Array.from(fileInput.files).sort((left, right) => {
                return right.lastModified - left.lastModified || left.name.localeCompare(right.name);
            });
            fileList.replaceChildren();
            files.forEach((file) => {
                const item = document.createElement('div');
                item.className = 'selected-file';
                item.textContent = file.name;
                fileList.appendChild(item);
            });
            submit.disabled = files.length === 0;
            submit.textContent = files.length ? `Analisar ${files.length} arquivo${files.length > 1 ? 's' : ''}` : 'Analisar arquivos';
        }

        ['dragenter', 'dragover'].forEach((eventName) => dropZone.addEventListener(eventName, (event) => {
            event.preventDefault();
            dropZone.classList.add('dragover');
        }));
        ['dragleave', 'drop'].forEach((eventName) => dropZone.addEventListener(eventName, () => dropZone.classList.remove('dragover')));
        dropZone.addEventListener('drop', (event) => {
            event.preventDefault();
            fileInput.files = event.dataTransfer.files;
            updateFileList();
        });
        fileInput.addEventListener('change', updateFileList);

        // A double submit would import the same snapshot twice.
        form.addEventListener('submit', (event) => {
            if (form.dataset.submitting === 'true') {
                event.preventDefault();
                return;
            }
            form.dataset.submitting = 'true';
            submit.disabled = true;
            submit.setAttribute('aria-busy', 'true');
            submit.textContent = 'Analisando… não feche esta página';
        });
    }

    function setupPasswordToggles() {
        document.querySelectorAll('[data-password-toggle]').forEach((button) => {
            button.addEventListener('click', () => {
                const input = document.getElementById(button.dataset.passwordToggle);
                const isHidden = input.type === 'password';
                input.type = isHidden ? 'text' : 'password';
                button.textContent = isHidden ? 'Ocultar' : 'Mostrar';
                button.setAttribute('aria-label', isHidden ? 'Ocultar senha' : 'Mostrar senha');
            });
        });
    }

    function setupConfirmations() {
        document.querySelectorAll('form[data-confirm]').forEach((form) => {
            form.addEventListener('submit', (event) => {
                if (!window.confirm(form.dataset.confirm)) event.preventDefault();
            });
        });
    }

    function clockIcon() {
        const ns = 'http://www.w3.org/2000/svg';
        const svg = document.createElementNS(ns, 'svg');
        for (const [name, value] of Object.entries({
            class: 'icon', width: '14', height: '14', viewBox: '0 0 24 24', fill: 'none',
            stroke: 'currentColor', 'stroke-width': '2', 'stroke-linecap': 'round',
            'stroke-linejoin': 'round', 'aria-hidden': 'true', focusable: 'false'
        })) svg.setAttribute(name, value);
        const circle = document.createElementNS(ns, 'circle');
        circle.setAttribute('cx', '12'); circle.setAttribute('cy', '12'); circle.setAttribute('r', '9');
        const hands = document.createElementNS(ns, 'path');
        hands.setAttribute('d', 'M12 7v5l3 2');
        svg.append(circle, hands);
        return svg;
    }

    function renderValidationProgress(card, data) {
        const progressText = document.getElementById('validationProgressText');
        const percent = document.getElementById('validationProgressPercent');
        const bar = document.getElementById('validationProgressBar');
        const queue = document.getElementById('validationQueue');
        const nextTimeEl = document.getElementById('validationNextTime');

        if (progressText) {
            progressText.textContent = `${data.completed} de ${data.total} concluídas · ${data.queued} na fila${data.running ? ` · ${data.running} em validação` : ''}`;
        }
        if (percent) percent.textContent = `${data.percent}%`;
        if (bar) {
            bar.style.width = `${data.percent}%`;
            bar.parentElement?.setAttribute('aria-valuenow', String(data.percent));
        }

        if (nextTimeEl) {
            nextTimeEl.replaceChildren();
            if (data.next_attempt_formatted && (data.queued || data.running)) {
                nextTimeEl.hidden = false;
                nextTimeEl.append(clockIcon(), ` Próxima averiguação prevista às ${data.next_attempt_formatted}`);
            } else {
                nextTimeEl.hidden = true;
            }
        }

        if (queue && Array.isArray(data.items)) {
            queue.replaceChildren();
            data.items.forEach((item) => {
                const itemEl = document.createElement('div');
                itemEl.className = 'validation-queue-item';

                const headerEl = document.createElement('div');
                headerEl.className = 'validation-item-header';
                const usernameEl = document.createElement('strong');
                usernameEl.textContent = `@${item.username}`;
                const badgeEl = document.createElement('span');
                badgeEl.className = `validation-badge ${item.badge_class || 'badge-queued'}`;
                badgeEl.textContent = item.status_label || item.status;
                headerEl.append(usernameEl, badgeEl);

                const subEl = document.createElement('div');
                subEl.className = 'validation-item-sub';
                subEl.textContent = item.sub_label || '';

                itemEl.append(headerEl, subEl);
                queue.appendChild(itemEl);
            });
        }

        // The lists above were rendered by the server: offer a refresh when results arrive.
        const banner = document.getElementById('validationRefresh');
        if (banner && Number(card.dataset.completed) !== data.completed) banner.hidden = false;
    }

    function setupValidationProgress() {
        const card = document.getElementById('validationProgress');
        if (!card) return;
        let timer = null;
        let active = card.dataset.active === 'true';

        const refresh = async () => {
            try {
                const response = await fetch(card.dataset.statusUrl, {headers: {'Accept': 'application/json'}});
                if (!response.ok) return;
                const data = await response.json();
                active = data.active;
                renderValidationProgress(card, data);
            } catch (error) {
                console.warn('Não foi possível atualizar o progresso das validações.', error);
            }
            if (!active) stop();
        };
        const start = () => {
            if (timer === null && active) timer = window.setInterval(refresh, POLL_INTERVAL_MS);
        };
        const stop = () => {
            if (timer !== null) window.clearInterval(timer);
            timer = null;
        };

        // Poll only while a batch is running and the tab is visible.
        document.addEventListener('visibilitychange', () => {
            if (document.hidden) { stop(); } else if (active) { refresh(); start(); }
        });
        if (active) { refresh(); start(); }
    }

    document.addEventListener('DOMContentLoaded', async () => {
        setupPasswordToggles();
        setupConfirmations();
        setupUpload();
        setupValidationProgress();
        await registerServiceWorker();
        await updateNotificationUi();
        document.getElementById('notificationButton')?.addEventListener('click', toggleNotifications);
    });
})();
