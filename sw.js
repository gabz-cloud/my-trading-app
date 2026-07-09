// sw.js — Service Worker สำหรับแจ้งเตือนแบบพื้นหลัง (Web Push)
// ไฟล์นี้ทำงานแยกจากหน้าเว็บหลัก รันอยู่เบื้องหลังแม้ปิดแท็บ/เบราว์เซอร์ไปแล้ว

self.addEventListener('install', function (event) {
    self.skipWaiting();
});

self.addEventListener('activate', function (event) {
    event.waitUntil(self.clients.claim());
});

// เมื่อ server ส่ง push event เข้ามา (จาก scheduled job ที่เช็คราคาทุก 5 นาที)
self.addEventListener('push', function (event) {
    let data = { title: '🔔 แจ้งเตือนหุ้น', body: '' };
    try {
        if (event.data) {
            data = event.data.json();
        }
    } catch (e) {
        if (event.data) {
            data.body = event.data.text();
        }
    }

    const title = data.title || '🔔 แจ้งเตือนหุ้น';
    const options = {
        body: data.body || '',
        icon: 'https://cdn-icons-png.flaticon.com/512/3421/3421111.png',
        badge: 'https://cdn-icons-png.flaticon.com/512/3421/3421111.png',
        vibrate: [200, 100, 200]
    };

    event.waitUntil(self.registration.showNotification(title, options));
});

// คลิกที่ notification แล้วเปิดหน้าเว็บ (หรือโฟกัสแท็บที่เปิดอยู่แล้ว)
self.addEventListener('notificationclick', function (event) {
    event.notification.close();
    event.waitUntil(
        self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (clientList) {
            for (const client of clientList) {
                if ('focus' in client) return client.focus();
            }
            if (self.clients.openWindow) return self.clients.openWindow('/');
        })
    );
});
