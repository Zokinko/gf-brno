// Spoločné pre obe stránky: registrácia offline režimu, rada pre iPhone a upozornenie bez signálu.
if ("serviceWorker" in navigator) {
  addEventListener("load", () => navigator.serviceWorker.register("sw.js").catch(() => {}));
}

(() => {
  const HINT_KEY = "gf-brno-ios-hint";
  const box = (css, html) => {
    const el = document.createElement("div");
    el.style.cssText = "position:fixed;left:50%;transform:translateX(-50%);z-index:1000;max-width:calc(100% - 32px);"
      + "background:var(--card);color:var(--fg);border:1px solid var(--line);box-shadow:0 4px 16px rgba(0,0,0,.18);"
      + "font:14px/1.4 system-ui,sans-serif;" + css;
    el.innerHTML = html;
    document.body.appendChild(el);
    return el;
  };

  // iPhone/iPad nemá tlačidlo „Nainštalovať“, poradíme ručný postup (len v Safari, kým to nezavrieš).
  const ios = /iphone|ipad|ipod/i.test(navigator.userAgent)
    || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  const standalone = navigator.standalone || matchMedia("(display-mode: standalone)").matches;
  let dismissed = false;
  try { dismissed = localStorage.getItem(HINT_KEY) === "1"; } catch {}
  if (ios && !standalone && !dismissed) {
    addEventListener("load", () => {
      const el = box("bottom:16px;width:360px;border-radius:12px;padding:12px 36px 12px 14px;",
        `📲 <strong>Pridaj si aplikáciu na plochu:</strong> v Safari ťukni na <strong>Zdieľať</strong>
         (štvorec so šípkou) a potom na <strong>Pridať na plochu</strong>.
         <button aria-label="Zavrieť" style="position:absolute;top:6px;right:8px;border:0;background:none;
           color:inherit;font-size:20px;cursor:pointer">×</button>`);
      el.querySelector("button").onclick = () => {
        el.remove();
        try { localStorage.setItem(HINT_KEY, "1"); } catch {}
      };
    });
  }

  let pill = null;
  const update = () => {
    if (navigator.onLine) { pill?.remove(); pill = null; return; }
    pill ??= box("top:10px;border-radius:999px;padding:4px 14px;font-size:13px;",
      "📴 Offline: zobrazujem naposledy uložené dáta");
  };
  addEventListener("online", update);
  addEventListener("offline", update);
  addEventListener("load", update);
})();
