(function () {
    const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || '';
    const publicKey = document.querySelector('meta[name="push-public-key"]')?.content || '';
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
        const dropZone = document.getElementById('dropZone');
        const fileInput = document.getElementById('fileInput');
        const fileList = document.getElementById('fileList');
        const submit = document.getElementById('uploadButton');
        if (!dropZone || !fileInput) return;

        function updateFileList() {
            const files = Array.from(fileInput.files);
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
    }

    document.addEventListener('DOMContentLoaded', async () => {
        setupUpload();
        await registerServiceWorker();
        await updateNotificationUi();
        document.getElementById('notificationButton')?.addEventListener('click', toggleNotifications);
    });
})();
