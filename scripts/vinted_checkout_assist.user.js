// ==UserScript==
// @name         BotRadar — Achat assisté (déclenche « Acheter » une fois)
// @namespace    https://radar.aminezegmou.fr/
// @version      1.0
// @description  Déclenche une seule fois le bouton natif « Acheter » d'une annonce ouverte depuis une alerte BotRadar (marqueur d'intention), puis s'arrête avant le checkout. Le point relais et le paiement restent entièrement manuels.
// @author       BotRadar
// @match        https://www.vinted.fr/items/*
// @match        https://vinted.fr/items/*
// @run-at       document-start
// @grant        none
// ==/UserScript==

(() => {
    'use strict';

    // Doit rester identique à CHECKOUT_ASSIST_MARKER (app/bot/keyboards/listings.py) :
    // un test lit ce fichier et compare les deux constantes.
    const MARKER = 'botradar-acheter';
    const WAIT_TIMEOUT_MS = 10000; // attente bornée du rendu de l'interface
    const POLL_INTERVAL_MS = 250;

    // ------------------------------------------------------------------
    // Logique pure : aucune référence au DOM, testée hors navigateur
    // (tests/js/checkout_assist_harness.mjs).
    // ------------------------------------------------------------------

    function readMarkerId(hash) {
        if (typeof hash !== 'string' || !hash) return null;
        const match = hash.match(new RegExp(MARKER + '=(\\d+)'));
        return match ? match[1] : null;
    }

    function itemIdFromPath(pathname) {
        const match = /^\/items\/(\d+)(?:[-/]|$)/.exec(pathname || '');
        return match ? match[1] : null;
    }

    function decide(input) {
        // `wait` : rien encore, l'appelant continuera jusqu'à sa borne.
        // `abort` : aucune raison d'attendre, on s'arrête sans cliquer.
        // `click` : un unique bouton natif actionnable, pour le bon article.
        const { markerId, pathItemId, buttons = [], blocked } = input;
        if (!markerId) return { action: 'abort', reason: 'no-marker' };
        if (blocked) return { action: 'abort', reason: 'blocked' };
        if (!pathItemId || markerId !== pathItemId) {
            return { action: 'abort', reason: 'item-mismatch' };
        }
        const actionable = buttons.filter(
            (button) => button && button.enabled !== false && button.visible !== false
        );
        if (actionable.length > 1) return { action: 'abort', reason: 'ambiguous-button' };
        if (actionable.length === 1) return { action: 'click', button: actionable[0] };
        return { action: 'wait', reason: 'no-button' };
    }

    async function runShell(env) {
        // Une seule passe : le marqueur est lu puis consommé avant toute
        // attente, donc ni rechargement ni retour arrière ne redéclenchent.
        const markerId = readMarkerId(env.location.hash);
        if (!markerId) return 'no-marker';
        env.consumeMarker();

        const pathItemId = itemIdFromPath(env.location.pathname);
        const deadline = env.now() + env.timeoutMs;

        while (env.now() < deadline) {
            const decision = decide({
                markerId,
                pathItemId,
                buttons: env.collectBuyButtons(),
                blocked: env.isBlocked(),
            });
            if (decision.action === 'click') {
                env.click(decision.button.button);
                return 'clicked';
            }
            if (decision.action === 'abort') return decision.reason;
            await env.sleep(env.pollIntervalMs);
        }
        return 'timeout';
    }

    // ------------------------------------------------------------------
    // Enveloppe navigateur : la seule partie qui touche au DOM réel.
    // Aucun cookie, aucun appel réseau, aucun clic hors du bouton natif.
    // ------------------------------------------------------------------

    function isVisible(element) {
        // Uniquement les styles calculés : dans un onglet ouvert en
        // arrière-plan, Chromium rend des rectangles de taille nulle pour
        // toute la chaîne DOM — s'en servir comme critère refuserait de
        // cliquer alors que le bouton est bien là. `display`/`visibility`/
        // `opacity` distinguent, eux, une variante réellement masquée.
        if (!element) return false;
        const style = window.getComputedStyle(element);
        return style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0';
    }

    function exactTextBuyButtons() {
        const found = [];
        const buttons = document.querySelectorAll('button');
        for (const button of buttons) {
            if (button.closest('footer, header, nav, #onetrust-consent-sdk')) continue;
            const text = (button.textContent || '').trim().toLowerCase();
            if (text === 'acheter' || text === 'buy' || text === 'acheter maintenant' || text === 'buy now') {
                found.push(button);
            }
        }
        return found;
    }

    function collectBuyButtons() {
        // Le bouton natif de la page article porte ce test-id ; sans lui, un
        // libellé exact fait repli. Un bouton en trop = ambiguïté = pas de clic
        // (arbitré par `decide`, pas ici).
        const primary = document.querySelectorAll('[data-testid="item-buy-button"]');
        const elements = primary.length ? Array.from(primary) : exactTextBuyButtons();
        return elements.map((element) => ({
            button: element,
            enabled: element.disabled !== true,
            visible: isVisible(element),
        }));
    }

    function pageLooksBlocked() {
        // Toute page qui n'est plus l'article (connexion, redirection) ou qui
        // porte un défi anti-robot s'arrête là : aucun clic, fiche utilisable
        // manuellement.
        if (!/^\/items\//.test(window.location.pathname)) return true;
        return Boolean(
            document.querySelector(
                'iframe[src*="captcha-delivery.com"], iframe[src*="datadome"], ' +
                    '[id*="datadome"], [class*="datadome"], #challenge-form'
            )
        );
    }

    function realEnv() {
        return {
            location: window.location,
            timeoutMs: WAIT_TIMEOUT_MS,
            pollIntervalMs: POLL_INTERVAL_MS,
            now: () => Date.now(),
            sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
            consumeMarker: () => {
                // Le fragment ne part jamais au serveur ; le retirer de
                // l'historique empêche tout redéclenchement (rechargement,
                // retour arrière) une fois l'intention traitée.
                const clean = window.location.pathname + window.location.search;
                window.history.replaceState(null, '', clean);
            },
            collectBuyButtons,
            isBlocked: pageLooksBlocked,
            click: (button) => button.click(),
        };
    }

    // Exposition pour le harnais de test (sans effet sur la page).
    if (typeof globalThis !== 'undefined') {
        globalThis.BotRadarCheckoutAssist = {
            MARKER,
            decide,
            runShell,
            readMarkerId,
            itemIdFromPath,
        };
    }

    if (typeof document !== 'undefined' && document.querySelector) {
        Promise.resolve(runShell(realEnv())).catch(() => {
            // Un échec du script ne doit jamais gêner la fiche article.
        });
    }
})();
