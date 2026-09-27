// ==UserScript==
// @name         BotRadar — Vinted Auto-Buy & Drop Sniper
// @namespace    https://radar.aminezegmou.fr/
// @version      4.3
// @description  Snipe automatique ultra-rapide sur Vinted : clique sur Acheter, sélectionne automatiquement Chronopost/Mondial Relay et vous laisse appuyer sur Payer
// @author       BotRadar
// @match        https://www.vinted.fr/items/*
// @match        https://vinted.fr/items/*
// @match        https://www.vinted.fr/checkout*
// @match        https://vinted.fr/checkout*
// @match        https://www.vinted.fr/transaction/buy/*
// @match        https://vinted.fr/transaction/buy/*
// @run-at       document-start
// @grant        none
// ==/UserScript==

(function () {
    'use strict';

    const MARKER = 'radar_autobuy=1';
    const FLAG_KEY = 'radar_autobuy_pending';
    const ITEM_KEY = 'radar_autobuy_item';
    const DROP_TARGET_KEY = 'radar_drop_target_ms';
    const TIMEOUT_MS = 20000; // 20 secondes max

    // STRICTEMENT DÉSACTIVÉ : C'est vous qui cliquez sur "Payer"
    const AUTO_PAY_AT_CHECKOUT = false;

    let activeWorker = null;
    let dropSniperArmed = false;
    let targetDropTimestamp = 0;
    let hudElement = null;

    // --- Audio Feedback (Web Audio API) ---
    function playBeep(freq = 880, duration = 0.15, type = 'sine') {
        try {
            const ctx = new (window.AudioContext || window.webkitAudioContext)();
            const osc = ctx.createOscillator();
            const gain = ctx.createGain();
            osc.type = type;
            osc.frequency.setValueAtTime(freq, ctx.currentTime);
            gain.gain.setValueAtTime(0.15, ctx.currentTime);
            gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + duration);
            osc.connect(gain);
            gain.connect(ctx.destination);
            osc.start();
            osc.stop(ctx.currentTime + duration);
        } catch (e) {}
    }

    function playSuccessChime() {
        playBeep(587.33, 0.12); // D5
        setTimeout(() => playBeep(880, 0.18), 120); // A5
        setTimeout(() => playBeep(1174.66, 0.25), 250); // D6
    }

    // --- Bannière en haut à droite ---
    function updateBanner(text, type = 'info') {
        let banner = document.getElementById('radar-autobuy-banner');
        if (!banner) {
            banner = document.createElement('div');
            banner.id = 'radar-autobuy-banner';
            banner.style.cssText = `
                position: fixed !important;
                top: 16px !important;
                right: 16px !important;
                z-index: 2147483647 !important;
                padding: 12px 20px !important;
                border-radius: 8px !important;
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif !important;
                font-size: 13px !important;
                font-weight: 700 !important;
                color: #ffffff !important;
                box-shadow: 0 4px 18px rgba(0,0,0,0.35) !important;
                transition: all 0.2s ease !important;
                pointer-events: none !important;
            `;
            document.documentElement.appendChild(banner);
        }
        if (type === 'info') banner.style.backgroundColor = '#09b1ba';
        else if (type === 'success') banner.style.backgroundColor = '#10b981';
        else if (type === 'warning') banner.style.backgroundColor = '#f59e0b';
        else if (type === 'error') banner.style.backgroundColor = '#ef4444';
        banner.textContent = text;
    }

    function isVisible(el) {
        if (!el) return false;
        try {
            const style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
            const rect = el.getBoundingClientRect();
            return (rect.width > 0 && rect.height > 0);
        } catch (e) {
            return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
        }
    }

    function dispatchFullClick(el) {
        if (!el) return;
        el.scrollIntoView({ block: 'center', inline: 'center' });
        const events = ['pointerdown', 'mousedown', 'focus', 'pointerup', 'mouseup', 'click'];
        for (const evtName of events) {
            try {
                const Ctor = evtName.startsWith('pointer') ? (window.PointerEvent || MouseEvent) : MouseEvent;
                const evt = new Ctor(evtName, { bubbles: true, cancelable: true, view: window, detail: 1 });
                el.dispatchEvent(evt);
            } catch (e) {}
        }
        try { el.click(); } catch (e) {}
    }

    function findBuyButton() {
        const exact = document.querySelector('button[data-testid="item-buy-button"], [data-testid="item-buy-button"], button[data-testid="buy-button"]');
        if (exact && !exact.disabled && isVisible(exact) && !exact.closest('footer, #onetrust-consent-sdk, header, nav')) {
            return exact;
        }

        const buttons = document.querySelectorAll('button');
        for (const btn of buttons) {
            if (btn.closest('footer, #onetrust-consent-sdk, header, nav')) continue;
            const text = (btn.textContent || '').trim().toLowerCase();
            if (text === 'acheter' || text === 'buy' || text === 'acheter maintenant' || text === 'buy now') {
                if (!btn.disabled && isVisible(btn)) return btn;
            }
        }
        return null;
    }

    function triggerBuyButtonClick(button) {
        console.log('[Radar AutoBuy] 🎯 Clic sur le bouton Acheter...', button);
        updateBanner('🎯 [BotRadar] Clic sur Acheter ! Redirection en cours...', 'success');
        playBeep(1200, 0.25);
        dispatchFullClick(button);

        const reactPropKey = Object.keys(button).find(k => k.startsWith('__reactProps'));
        if (reactPropKey && button[reactPropKey] && typeof button[reactPropKey].onClick === 'function') {
            try {
                button[reactPropKey].onClick({
                    preventDefault: () => {},
                    stopPropagation: () => {},
                    isDefaultPrevented: () => false,
                    isPropagationStopped: () => false,
                    target: button,
                    currentTarget: button
                });
            } catch (err) {}
        }
    }

    // --- Web Worker Anti-Throttling (Chrome Background Protection) ---
    function createUnthrottledTimerWorker() {
        const workerCode = `
            let timerId = null;
            self.onmessage = function(e) {
                if (e.data.action === 'start') {
                    const interval = e.data.interval || 100;
                    if (timerId) clearInterval(timerId);
                    timerId = setInterval(() => {
                        self.postMessage({ type: 'tick', time: Date.now() });
                    }, interval);
                } else if (e.data.action === 'stop') {
                    if (timerId) clearInterval(timerId);
                    timerId = null;
                }
            };
        `;
        const blob = new Blob([workerCode], { type: 'application/javascript' });
        return new Worker(URL.createObjectURL(blob));
    }

    // --- Extraction robuste du compte à rebours Vinted ---
    function parseVintedDropTarget() {
        const text = (document.body ? document.body.innerText : '') || '';
        if (!text) return null;

        const lower = text.toLowerCase();
        const idx = lower.indexOf("commence dans");
        if (idx !== -1) {
            const snippet = text.substring(idx, idx + 300);
            const numbers = snippet.match(/\\b\\d+\\b/g);
            if (numbers && numbers.length >= 4) {
                const d = parseInt(numbers[0], 10);
                const h = parseInt(numbers[1], 10);
                const m = parseInt(numbers[2], 10);
                const s = parseInt(numbers[3], 10);
                return Date.now() + (((d * 24 + h) * 60 + m) * 60 + s) * 1000;
            } else if (numbers && numbers.length === 3) {
                const h = parseInt(numbers[0], 10);
                const m = parseInt(numbers[1], 10);
                const s = parseInt(numbers[2], 10);
                return Date.now() + ((h * 60 + m) * 60 + s) * 1000;
            }
        }

        const linear = text.match(/(\\d+)\\s*j(?:ours?)?\\s*(\\d+)\\s*h(?:eures?)?\\s*(\\d+)\\s*m(?:in(?:utes?)?)?\\s*(\\d+)\\s*s(?:ec(?:ondes?)?)?/i);
        if (linear) {
            const d = parseInt(linear[1], 10);
            const h = parseInt(linear[2], 10);
            const m = parseInt(linear[3], 10);
            const s = parseInt(linear[4], 10);
            return Date.now() + (((d * 24 + h) * 60 + m) * 60 + s) * 1000;
        }

        return null;
    }

    // =========================================================================
    // HUD VISUEL IMMUNISÉ CONTRE LA HYDRATATION REACT
    // =========================================================================

    function createOrGetHUD(itemId) {
        let hud = document.getElementById('radar-sniper-hud');
        if (hud && document.documentElement.contains(hud)) {
            return hud;
        }

        if (!hud) {
            hud = document.createElement('div');
            hud.id = 'radar-sniper-hud';
            hud.style.cssText = `
                position: fixed !important;
                bottom: 24px !important;
                right: 24px !important;
                z-index: 2147483647 !important;
                background: rgba(15, 23, 42, 0.97) !important;
                backdrop-filter: blur(12px) !important;
                border: 2px solid #09b1ba !important;
                border-radius: 14px !important;
                padding: 18px 22px !important;
                color: #ffffff !important;
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif !important;
                box-shadow: 0 14px 40px rgba(0,0,0,0.7) !important;
                min-width: 310px !important;
                max-width: 360px !important;
                display: block !important;
                opacity: 1 !important;
                visibility: visible !important;
                pointer-events: auto !important;
            `;

            hud.innerHTML = `
                <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px;">
                    <span style="font-weight: 800; color: #09b1ba; font-size: 14px; letter-spacing: 0.5px;">🎯 RADAR DROP SNIPER</span>
                    <span id="radar-hud-badge" style="font-size: 11px; background: ${dropSniperArmed ? '#10b981' : '#3b82f6'}; color: white; padding: 2px 8px; border-radius: 12px; font-weight: 700;">
                        ${dropSniperArmed ? '🟢 ARMÉ' : 'SCANNING...'}
                    </span>
                </div>
                <div id="radar-hud-label" style="font-size: 12px; color: #94a3b8; margin-bottom: 4px;">
                    ${dropSniperArmed ? 'Vente programmée dans :' : 'Statut du drop :'}
                </div>
                <div id="radar-hud-countdown" style="font-size: 24px; font-weight: 800; color: #f8fafc; font-family: monospace; letter-spacing: 1.5px; margin-bottom: 12px;">
                    ${dropSniperArmed ? 'Calcul...' : 'Recherche en cours...'}
                </div>
                <div id="radar-hud-target-date" style="font-size: 11px; color: #38bdf8; margin-bottom: 12px; display: ${dropSniperArmed ? 'block' : 'none'};">
                    ${dropSniperArmed && targetDropTimestamp ? '📅 Cible : ' + new Date(targetDropTimestamp).toLocaleString() : ''}
                </div>

                <div style="background: rgba(255,255,255,0.06); padding: 8px 10px; border-radius: 8px; margin-bottom: 12px; font-size: 11px; color: #cbd5e1; line-height: 1.4;">
                    📦 <b>Mode manuel sécurisé</b> : Sélection automatique du relais, <u>vous validez le paiement final</u>.
                </div>

                <div style="display: flex; gap: 8px;">
                    <button id="radar-hud-btn-test" style="flex: 1; padding: 8px 12px; background: #09b1ba; border: none; border-radius: 6px; color: white; font-weight: 700; font-size: 12px; cursor: pointer;">
                        🚀 Déclencher l’achat
                    </button>
                    <button id="radar-hud-btn-edit" style="padding: 8px 10px; background: rgba(255,255,255,0.1); border: 1px solid rgba(255,255,255,0.2); border-radius: 6px; color: #cbd5e1; font-size: 12px; cursor: pointer;">
                        ⚙️ Régler
                    </button>
                </div>
            `;
        }

        document.documentElement.appendChild(hud);
        hudElement = hud;

        const testBtn = hud.querySelector('#radar-hud-btn-test');
        if (testBtn) {
            testBtn.onclick = () => {
                sessionStorage.setItem(FLAG_KEY, 'true');
                if (itemId) sessionStorage.setItem(ITEM_KEY, itemId);
                const btn = findBuyButton();
                if (btn) {
                    triggerBuyButtonClick(btn);
                } else {
                    updateBanner('⚠️ Bouton "Acheter" non encore actif', 'warning');
                    playBeep(440, 0.2, 'sawtooth');
                }
            };
        }

        const editBtn = hud.querySelector('#radar-hud-btn-edit');
        if (editBtn) {
            editBtn.onclick = () => {
                const input = prompt("Régler manuellement l'heure cible du Drop (YYYY-MM-DD HH:MM:SS) :", "2026-09-16 16:07:00");
                if (input) {
                    const parsed = new Date(input.replace(' ', 'T')).getTime();
                    if (!isNaN(parsed) && parsed > Date.now()) {
                        targetDropTimestamp = parsed;
                        armDropSniper(targetDropTimestamp, itemId);
                        updateBanner("🎯 Heure cible configurée !", "success");
                    } else {
                        alert("Date invalide ou passée !");
                    }
                }
            };
        }

        return hud;
    }

    function armDropSniper(targetMs, itemId) {
        dropSniperArmed = true;
        targetDropTimestamp = targetMs;
        localStorage.setItem(DROP_TARGET_KEY, targetMs.toString());
        sessionStorage.setItem(FLAG_KEY, 'true');
        if (itemId) sessionStorage.setItem(ITEM_KEY, itemId);

        const hud = createOrGetHUD(itemId);
        const badge = hud.querySelector('#radar-hud-badge');
        const label = hud.querySelector('#radar-hud-label');
        const targetDateEl = hud.querySelector('#radar-hud-target-date');

        if (badge) {
            badge.textContent = '🟢 SNIPER ARMÉ';
            badge.style.background = '#10b981';
        }
        if (label) label.textContent = 'Déclenchement du drop dans :';
        if (targetDateEl) {
            const d = new Date(targetMs);
            targetDateEl.textContent = `📅 Cible : ${d.toLocaleDateString()} à ${d.toLocaleTimeString()}`;
            targetDateEl.style.display = 'block';
        }

        if (activeWorker) activeWorker.postMessage({ action: 'stop' });
        activeWorker = createUnthrottledTimerWorker();

        let highFreq = false;
        let warned5s = false;
        let buySent = false;

        activeWorker.postMessage({ action: 'start', interval: 250 });

        activeWorker.onmessage = function (e) {
            if (buySent) return;
            const now = e.data.time || Date.now();
            const diffMs = targetDropTimestamp - now;

            const currentHud = createOrGetHUD(itemId);
            const countdownEl = currentHud.querySelector('#radar-hud-countdown');
            if (countdownEl) {
                if (diffMs > 0) {
                    const totalSecs = Math.floor(diffMs / 1000);
                    const days = Math.floor(totalSecs / 86400);
                    const hours = Math.floor((totalSecs % 86400) / 3600);
                    const mins = Math.floor((totalSecs % 3600) / 60);
                    const secs = totalSecs % 60;
                    countdownEl.textContent = `${days > 0 ? days + 'j ' : ''}${String(hours).padStart(2, '0')}h ${String(mins).padStart(2, '0')}m ${String(secs).padStart(2, '0')}s`;
                } else {
                    countdownEl.textContent = '⚡ EN COURS ! ⚡';
                    countdownEl.style.color = '#10b981';
                }
            }

            if (diffMs <= 10000 && !highFreq) {
                highFreq = true;
                activeWorker.postMessage({ action: 'start', interval: 25 });
                console.log('[Radar Drop Sniper] ⚡ T-10s : Scan ultra-haute fréquence 25ms engagé !');
            }

            if (diffMs <= 5000 && diffMs > 0 && !warned5s) {
                warned5s = true;
                playBeep(980, 0.35);
                updateBanner('🚨 [BotRadar] Drop dans 5 secondes ! Tenez-vous prêt !', 'warning');
            }

            if (diffMs <= 30) {
                const btn = findBuyButton();
                if (btn) {
                    buySent = true;
                    activeWorker.postMessage({ action: 'stop' });
                    triggerBuyButtonClick(btn);
                    return;
                }

                if (diffMs < -700 && !buySent) {
                    buySent = true;
                    activeWorker.postMessage({ action: 'stop' });
                    console.log('[Radar Drop Sniper] 🔄 Rechargement forcé de la page pour rafraîchir React...');
                    location.reload();
                }
            }
        };
    }

    // =========================================================================
    // 1. GESTION DE LA PAGE ARTICLE (/items/...)
    // =========================================================================

    function handleItemPage() {
        const itemIdMatch = location.pathname.match(/\/items\/(\d+)/);
        const itemId = itemIdMatch ? itemIdMatch[1] : null;

        // Création immédiate et indestructible du HUD
        createOrGetHUD(itemId);

        // Guardian : vérifie toutes les 800ms que React n'a pas viré le HUD
        setInterval(() => {
            createOrGetHUD(itemId);
        }, 800);

        let checkCount = 0;
        const poller = setInterval(() => {
            checkCount++;
            const detectedTarget = parseVintedDropTarget();

            if (detectedTarget && detectedTarget > Date.now()) {
                clearInterval(poller);
                console.log(`[Radar Drop Sniper] 🎯 Drop détecté dans le DOM à ${new Date(detectedTarget).toLocaleString()}`);
                armDropSniper(detectedTarget, itemId);
                updateBanner('🎯 [BotRadar] Vente programmée détectée — Sniper armé !', 'success');
                return;
            }

            if (checkCount >= 50) { // 10 secondes de recherche
                clearInterval(poller);
                if (!dropSniperArmed) {
                    const hud = createOrGetHUD(itemId);
                    const badge = hud.querySelector('#radar-hud-badge');
                    const label = hud.querySelector('#radar-hud-label');
                    const countdownEl = hud.querySelector('#radar-hud-countdown');

                    if (badge) {
                        badge.textContent = 'VENTE CLASSIQUE';
                        badge.style.background = '#64748b';
                    }
                    if (label) label.textContent = 'Aucun décompte de drop détecté';
                    if (countdownEl) countdownEl.textContent = 'Article disponible';

                    if (location.hash.includes(MARKER) || location.search.includes(MARKER)) {
                        runStandardAutoBuy(itemId);
                    }
                }
            }
        }, 200);
    }

    function runStandardAutoBuy(itemId) {
        const startTime = Date.now();
        let dispatched = false;
        const intervalId = setInterval(() => {
            if (dispatched) return;
            const btn = findBuyButton();
            if (btn) {
                dispatched = true;
                clearInterval(intervalId);
                triggerBuyButtonClick(btn);
            }
            if (Date.now() - startTime > 15000) {
                clearInterval(intervalId);
            }
        }, 80);
    }

    // =========================================================================
    // 2. GESTION DE LA PAGE CHECKOUT (/checkout & /transaction/buy/...)
    // =========================================================================

    function handleCheckoutPage() {
        const isPending = sessionStorage.getItem(FLAG_KEY) === 'true';

        // Ne s'active que si cet achat provient d'un snipe BotRadar
        if (!isPending) {
            console.log('[Radar Checkout] Achat manuel détecté (pas de marqueur BotRadar). Script en veille.');
            return;
        }

        console.log('[Radar Checkout] 📦 Alerte BotRadar détectée ! Prise en charge automatique du point de livraison...');
        updateBanner('⚡ [BotRadar] Recherche du point Chronopost / Mondial Relay...', 'info');

        const startTime = Date.now();
        let selectedTarget = null;
        let selectedCarrierName = '';
        let cardClickedTime = 0;
        let pickupConfirmed = false;
        let checkoutIntervalId = null;

        function getPickupModal() {
            const dialogs = document.querySelectorAll('div[role="dialog"], .web_ui__Modal__content, [class*="Modal"]');
            for (const d of dialogs) {
                const text = (d.textContent || '').toLowerCase();
                if (text.includes('point de retrait') || text.includes('points relais') || text.includes('dépôt')) {
                    return d;
                }
            }
            return null;
        }

        function openPickupModalIfNeeded() {
            const candidates = document.querySelectorAll('button, a[role="button"], a, div[role="button"]');
            for (const btn of candidates) {
                const text = (btn.textContent || '').trim().toLowerCase();
                if (
                    text.includes('choisir un point') ||
                    text.includes('sélectionner un point') ||
                    text.includes('changer le point') ||
                    text.includes('modifier le point') ||
                    (text.includes('point de retrait') && (text.includes('choisir') || text.includes('sélectionner') || text.includes('modifier')))
                ) {
                    if (isVisible(btn) && !btn.disabled) {
                        console.log('[Radar Checkout] 📦 Clic pour ouvrir la modal de point relais :', btn);
                        btn.click();
                        return true;
                    }
                }
            }
            return false;
        }

        // Extrait les cartes individuelles de points relais dans la liste de gauche
        function getPickupCards(modal) {
            const candidates = modal.querySelectorAll('div, li, [role="button"], button');
            const cards = [];

            for (const el of candidates) {
                const text = (el.textContent || '').trim();
                // Une carte individuelle contient exactement un prix (€) et une mention de disponibilité
                if (
                    text.includes('€') &&
                    (text.includes('jours') || text.includes('ouvrés') || text.includes('Disponible')) &&
                    !text.toLowerCase().includes('confirmer') &&
                    !text.toLowerCase().includes('points relais dans cette zone') &&
                    text.length >= 25 && text.length <= 260
                ) {
                    const countEuros = (text.match(/€/g) || []).length;
                    if (countEuros === 1) {
                        cards.push(el);
                    }
                }
            }
            return cards;
        }

        // Sélectionne la meilleure carte selon l'ordre : Chronopost > Mondial Relay > Premier disponible
        function findBestCard(cards) {
            // Priorité 1 : Chronopost
            for (const card of cards) {
                const text = card.textContent.toLowerCase();
                if (text.includes('chronopost')) {
                    return { card, carrier: 'Chronopost' };
                }
            }

            // Priorité 2 : Mondial Relay
            for (const card of cards) {
                const text = card.textContent.toLowerCase();
                if (text.includes('mondial') || text.includes('relay')) {
                    return { card, carrier: 'Mondial Relay' };
                }
            }

            // Priorité 3 : Premier point relais disponible (Fallback)
            if (cards.length > 0) {
                return { card: cards[0], carrier: 'Premier point disponible' };
            }

            return null;
        }

        function highlightPayButton() {
            const buttons = document.querySelectorAll('button, [role="button"]');
            for (const btn of buttons) {
                const text = (btn.textContent || '').trim().toLowerCase();
                if (text === 'payer' || text === 'valider et payer' || text === 'payer maintenant' || text.includes('payer')) {
                    if (isVisible(btn) && !btn.disabled && !btn.closest('footer')) {
                        // Met en valeur le bouton Payer pour que vous puissiez appuyer dessus instantanément
                        btn.style.outline = '4px solid #10b981';
                        btn.style.boxShadow = '0 0 25px rgba(16, 185, 129, 0.9)';
                        btn.scrollIntoView({ block: 'center', behavior: 'smooth' });
                        btn.focus();
                        return;
                    }
                }
            }
        }

        function processModal(modal) {
            if (pickupConfirmed) return;

            // Étape 1 : Si la carte n'a pas encore été cliquée, on la trouve et on clique dessus
            if (!cardClickedTime) {
                const cards = getPickupCards(modal);
                if (cards.length === 0) return;

                const best = findBestCard(cards);
                if (best) {
                    selectedTarget = best.card;
                    selectedCarrierName = best.carrier;
                    cardClickedTime = Date.now();

                    console.log(`[Radar Checkout] 📍 Clic sur le point relais (${best.carrier}) :`, best.card);
                    updateBanner(`📍 [BotRadar] ${best.carrier} sélectionné... En attente`, 'info');

                    dispatchFullClick(best.card);

                    const innerClickable = best.card.querySelector('button, [role="button"], a');
                    if (innerClickable) dispatchFullClick(innerClickable);

                    const parentClickable = best.card.closest('li, [role="button"]');
                    if (parentClickable && parentClickable !== best.card) dispatchFullClick(parentClickable);
                }
                return;
            }

            // Étape 2 : Laisser 400ms à React pour actualiser l'état et le panneau de confirmation
            const elapsedSinceCardClick = Date.now() - cardClickedTime;
            if (elapsedSinceCardClick < 400) {
                return;
            }

            // Étape 3 : Clic sur le bouton "Confirmer" une fois l'état React à jour
            const buttons = modal.querySelectorAll('button, [role="button"]');
            for (const btn of buttons) {
                const btnText = (btn.textContent || '').trim().toLowerCase();
                if (btnText === 'confirmer' || btnText === 'confirm') {
                    if (isVisible(btn) && !btn.disabled) {
                        pickupConfirmed = true;
                        clearInterval(checkoutIntervalId);
                        console.log(`[Radar Checkout] 🎯 Clic sur "Confirmer" (${selectedCarrierName}) !`);
                        
                        // Son de succès & notification claire : à vous d'appuyer sur Payer !
                        playSuccessChime();
                        updateBanner(`✅ [BotRadar] ${selectedCarrierName} validé ! À vous de cliquer sur "Payer" 💳`, 'success');

                        dispatchFullClick(btn);

                        // Nettoyage de l'indicateur de session
                        sessionStorage.removeItem(FLAG_KEY);
                        sessionStorage.removeItem(ITEM_KEY);

                        // Met en surbrillance verte éclatante le bouton "Payer"
                        setTimeout(() => {
                            highlightPayButton();
                        }, 500);

                        setTimeout(() => {
                            const banner = document.getElementById('radar-autobuy-banner');
                            if (banner) banner.remove();
                        }, 8000);
                        return;
                    }
                }
            }
        }

        function attemptCheckout() {
            if (pickupConfirmed) return;

            const elapsed = Date.now() - startTime;
            if (elapsed >= TIMEOUT_MS) {
                clearInterval(checkoutIntervalId);
                sessionStorage.removeItem(FLAG_KEY);
                sessionStorage.removeItem(ITEM_KEY);
                console.warn('[Radar Checkout] ⏱️ Fin du délai d’attente du point relais.');
                return;
            }

            const modal = getPickupModal();
            if (modal) {
                processModal(modal);
            } else {
                openPickupModalIfNeeded();
            }
        }

        checkoutIntervalId = setInterval(attemptCheckout, 150);
    }

    // =========================================================================
    // ROUTAGE
    // =========================================================================

    if (location.pathname.startsWith('/items/')) {
        handleItemPage();
    } else if (location.pathname.includes('/checkout') || location.pathname.includes('/transaction/buy')) {
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', handleCheckoutPage);
        } else {
            handleCheckoutPage();
        }
    }
})();
