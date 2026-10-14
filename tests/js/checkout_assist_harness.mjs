/**
 * Harnais de test du userscript d'achat assisté, sans navigateur.
 *
 * Le script expose sa logique pure (`decide`, `runShell`) : ce harnais
 * l'exécute dans un contexte `vm` sans DOM et rejoue les cas qui décident
 * qu'un clic doit avoir lieu, ou pas. Lancé par
 * `tests/unit/test_vinted_checkout_assist.py` ; sortie 0 si tout passe.
 */

import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const scriptPath = path.join(here, "..", "..", "scripts", "vinted_checkout_assist.user.js");
const source = fs.readFileSync(scriptPath, "utf8");

const sandbox = { console };
vm.createContext(sandbox);
vm.runInContext(source, sandbox);

const assist = sandbox.BotRadarCheckoutAssist;
if (!assist) {
  console.error("FAIL: le script n'expose pas BotRadarCheckoutAssist");
  process.exit(1);
}

const failures = [];
let passed = 0;

function check(name, condition, detail = "") {
  if (condition) {
    passed += 1;
    return;
  }
  failures.push(`${name}${detail ? " — " + detail : ""}`);
}

function makeEnv(overrides = {}) {
  const calls = { clicks: 0, consumed: 0 };
  let clock = 0;
  const env = {
    location: { hash: "#botradar-acheter=123", pathname: "/items/123-console" },
    timeoutMs: 50,
    pollIntervalMs: 10,
    now: () => clock,
    sleep: async (ms) => {
      clock += ms;
    },
    consumeMarker: () => {
      calls.consumed += 1;
      env.location.hash = "";
    },
    collectBuyButtons: () => [],
    isBlocked: () => false,
    click: () => {
      calls.clicks += 1;
    },
    ...overrides,
  };
  return { env, calls };
}

function button(extra = {}) {
  return { enabled: true, visible: true, ...extra };
}

async function main() {
  // --- lecture du marqueur et de l'identifiant d'article -------------------
  check("readMarkerId lit l'identifiant", assist.readMarkerId("#botradar-acheter=42") === "42");
  check(
    "readMarkerId accepte un fragment composé",
    assist.readMarkerId("#autre=1&botradar-acheter=42") === "42"
  );
  check("readMarkerId ignore un autre fragment", assist.readMarkerId("#autre=1") === null);
  check("readMarkerId refuse un identifiant non numérique", assist.readMarkerId("#botradar-acheter=abc") === null);
  check("itemIdFromPath lit la page article", assist.itemIdFromPath("/items/123-console") === "123");
  check("itemIdFromPath refuse une autre page", assist.itemIdFromPath("/checkout") === null);

  // --- absence d'intention : rien ne se passe ------------------------------
  {
    const { env, calls } = makeEnv({ location: { hash: "", pathname: "/items/123-console" } });
    const result = await assist.runShell(env);
    check("sans marqueur : aucun traitement", result === "no-marker", result);
    check("sans marqueur : aucun clic", calls.clicks === 0);
    check("sans marqueur : marqueur non consommé", calls.consumed === 0);
  }

  // --- mauvais article : le marqueur ne correspond pas à la page -----------
  {
    const { env, calls } = makeEnv({
      location: { hash: "#botradar-acheter=999", pathname: "/items/123-console" },
      collectBuyButtons: () => [button()],
    });
    const result = await assist.runShell(env);
    check("mauvais article : abandon", result === "item-mismatch", result);
    check("mauvais article : aucun clic", calls.clicks === 0);
  }

  // --- page bloquée ou redirigée (connexion, CAPTCHA) ----------------------
  for (const [label, blocked] of [
    ["connexion requise", true],
    ["défi anti-robot", true],
  ]) {
    const { env, calls } = makeEnv({
      isBlocked: () => blocked,
      collectBuyButtons: () => [button()],
    });
    const result = await assist.runShell(env);
    check(`${label} : abandon`, result === "blocked", result);
    check(`${label} : aucun clic`, calls.clicks === 0);
  }

  // --- bouton absent (article vendu, page en cours) → attente bornée -------
  {
    const { env, calls } = makeEnv({ collectBuyButtons: () => [] });
    const result = await assist.runShell(env);
    check("bouton absent : attente bornée puis abandon", result === "timeout", result);
    check("bouton absent : aucun clic", calls.clicks === 0);
  }

  // --- bouton désactivé ou invisible : pas actionnable ---------------------
  {
    const { env, calls } = makeEnv({
      collectBuyButtons: () => [button({ enabled: false })],
    });
    const result = await assist.runShell(env);
    check("bouton désactivé : aucun clic", calls.clicks === 0, result);
  }
  {
    const { env, calls } = makeEnv({
      collectBuyButtons: () => [button({ visible: false })],
    });
    const result = await assist.runShell(env);
    check("bouton invisible : aucun clic", calls.clicks === 0, result);
  }

  // --- bouton ambigu : deux candidats actionnables -------------------------
  {
    const { env, calls } = makeEnv({
      collectBuyButtons: () => [button(), button()],
    });
    const result = await assist.runShell(env);
    check("bouton ambigu : abandon", result === "ambiguous-button", result);
    check("bouton ambigu : aucun clic", calls.clicks === 0);
  }

  // --- un seul candidat actionnable : un clic, une seule fois --------------
  {
    let polls = 0;
    const { env, calls } = makeEnv({
      collectBuyButtons: () => {
        polls += 1;
        return polls >= 2 ? [button(), button({ visible: false })] : [];
      },
    });
    const result = await assist.runShell(env);
    check("bouton unique après rendu : clic", result === "clicked", result);
    check("bouton unique : clic unique", calls.clicks === 1, String(calls.clicks));
    check("bouton unique : marqueur consommé avant le clic", calls.consumed === 1);
    check("bouton unique : le clic a suivi la consommation", calls.consumed === 1 && calls.clicks === 1);
  }

  // --- après consommation, un second passage ne reclique pas ---------------
  {
    const { env, calls } = makeEnv({
      collectBuyButtons: () => [button()],
    });
    const first = await assist.runShell(env);
    const second = await assist.runShell(env);
    check("premier passage : clic", first === "clicked", first);
    check("second passage après consommation : aucun traitement", second === "no-marker", second);
    check("un seul clic au total", calls.clicks === 1, String(calls.clicks));
  }

  // --- le clic n'a lieu qu'une fois même si le bouton reste présent --------
  {
    let clicks = 0;
    const { env } = makeEnv({
      collectBuyButtons: () => [button()],
      click: () => {
        clicks += 1;
      },
    });
    await assist.runShell(env);
    await new Promise((resolve) => setTimeout(resolve, 30));
    check("aucun clic répété après le retour", clicks === 1, String(clicks));
  }

  if (failures.length) {
    console.error(`FAIL ${failures.length}/${passed + failures.length}`);
    for (const failure of failures) console.error(" - " + failure);
    process.exit(1);
  }
  console.log(`OK ${passed} cas`);
}

main().catch((error) => {
  console.error("FAIL: exception —", error && error.message);
  process.exit(1);
});
