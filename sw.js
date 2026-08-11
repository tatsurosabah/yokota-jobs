/* Yokota Jobs — Service Worker
 *
 * ネットワーク優先: HTML と jobs.json
 *   画面もデータも、更新したら次に開いたとき反映されてほしい。
 *   cache-first にすると index.html を直しても古い画面が出続ける。
 * キャッシュ優先: アイコン等の変化しない静的ファイル
 * どちらもオフライン時はキャッシュにフォールバックする。
 */
const CACHE = "yokota-jobs-v2";
const SHELL = ["./", "./index.html", "./manifest.json", "./icon-180.png", "./icon-512.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys()
      .then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;

  const path = new URL(req.url).pathname;
  const fresh = req.mode === "navigate"          // ページ遷移
             || path.endsWith("jobs.json")
             || path.endsWith(".html")
             || path.endsWith("/");

  if (fresh) {
    // network-first。取れたらキャッシュも更新し、落ちていたらキャッシュで凌ぐ
    e.respondWith(
      fetch(req)
        .then(res => {
          const copy = res.clone();
          caches.open(CACHE).then(c => c.put(req, copy));
          return res;
        })
        .catch(() => caches.match(req).then(hit => hit || caches.match("./index.html")))
    );
    return;
  }

  // 静的ファイルは cache-first
  e.respondWith(
    caches.match(req).then(hit => hit || fetch(req).then(res => {
      if (res.ok && res.type === "basic") {
        const copy = res.clone();
        caches.open(CACHE).then(c => c.put(req, copy));
      }
      return res;
    }))
  );
});
