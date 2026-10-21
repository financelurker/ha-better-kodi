// Takes the README screenshots from a Home Assistant prepared by setup_ha.py.
//
//   node scripts/screenshots/shoot.mjs [base url] [out dir] [only this screenshot]
//
// PLAYWRIGHT_MODULE: path to playwright's index.mjs if it isn't installed next to this repo.
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ?? "playwright");

const [base = "http://127.0.0.1:8123", out = "docs/images", only] = process.argv.slice(2);
const USER = "demo";
const PASSWORD = "demo-password-1";
const PLAYER = "media_player.living_room_kodi_2";
const DEVICE = process.env.DEVICE_ID;

const browser = await chromium.launch();
const context = await browser.newContext({
  viewport: { width: 1280, height: 820 },
  deviceScaleFactor: 1.5,
  colorScheme: "light",
  locale: "en-US",
});
const page = await context.newPage();

async function settle(ms = 2000) {
  await page.waitForLoadState("networkidle").catch(() => {});
  await page.waitForTimeout(ms);
}

// Media browser cards have no accessible name; click a card's title (not the
// play button in its middle).
async function openCard(title) {
  await page
    .locator("ha-media-player-browse .child .title")
    .filter({ hasText: new RegExp(`^\\s*${title}\\s*$`) })
    .first()
    .click();
  await settle(3000);
}

async function shot(name, options = {}) {
  const { locator, clip, fullPage, omitBackground } = options;
  await page.mouse.move(1279, 819); // no hover effects or tooltips
  const path = `${out}/${name}.png`;
  if (locator) await locator.screenshot({ path, animations: "disabled" });
  else await page.screenshot({ path, clip, fullPage, omitBackground, animations: "disabled" });
  console.log(path);
}

const shots = {
  // Dashboard: media control card plus the link field and its buttons
  async dashboard() {
    await page.goto(`${base}/kodi-demo/0`);
    await settle();
    await page.getByRole("textbox", { name: "Link" }).fill("https://youtu.be/Xq7mB2vRk9w");
    await settle(800);
    await shot("dashboard", { clip: { x: 500, y: 60, width: 540, height: 490 } });
  },

  // Media browser: the Queue folder next to Kodi's library
  async "media-browser"() {
    await page.goto(`${base}/media-browser/${PLAYER}`);
    await settle(3000);
    await shot("media-browser", { clip: { x: 256, y: 0, width: 1024, height: 318 } });
  },

  // The queue, with the playing entry marked
  async queue() {
    await page.goto(`${base}/media-browser/${PLAYER}`);
    await settle(3000);
    await openCard("Queue");
    // List layout: the queue's titles are long
    await page.getByRole("button", { name: "Menu" }).last().click({ force: true });
    await page.getByRole("menuitem", { name: /list/i }).click().catch(() => {});
    await settle(1500);
    await page.mouse.click(1000, 700); // away from the menu button (focus ring)
    await shot("queue", { clip: { x: 256, y: 0, width: 1024, height: 360 } });
    await page.getByRole("button", { name: "Menu" }).last().click({ force: true });
    await page.getByRole("menuitem", { name: /auto/i }).click().catch(() => {});
  },

  // A movie: play now, play next, add to queue, replace queue
  async choices() {
    await page.goto(`${base}/media-browser/${PLAYER}`);
    await settle(3000);
    await openCard("Movies");
    await openCard("Winter Orchard");
    await shot("choices", { clip: { x: 256, y: 0, width: 1024, height: 630 } });
  },

  // Device page with everything Better Kodi adds
  async device() {
    await page.setViewportSize({ width: 1280, height: 1400 });
    await page.goto(`${base}/config/devices/device/${DEVICE}`);
    await settle(3000);
    await shot("device", { clip: { x: 256, y: 0, width: 700, height: 1226 } });
    await page.setViewportSize({ width: 1280, height: 820 });
  },

  // Options dialog
  async options() {
    await page.setViewportSize({ width: 1280, height: 2200 });
    await page.goto(`${base}/config/integrations/integration/better_kodi`);
    await settle(3000);
    await page
      .getByRole("button", { name: "Configure", exact: true })
      .filter({ visible: true })
      .first()
      .click({ force: true });
    await settle(3000);
    // The dialog's surface: the rounded box the title sits in
    const box = await page.getByText("Better Kodi options", { exact: true }).evaluate((title) => {
      for (let n = title; n; n = n.assignedSlot ?? n.parentElement ?? n.getRootNode().host) {
        const rect = n.getBoundingClientRect();
        if (parseFloat(getComputedStyle(n).borderTopLeftRadius) >= 12 && rect.height > 400) {
          // Transparent around the rounded corners: no backdrop, no page behind
          const style = document.createElement("style");
          style.textContent = "dialog::backdrop { background: transparent !important; }";
          n.getRootNode().append(style);
          const main = document.querySelector("home-assistant").shadowRoot.querySelector("home-assistant-main");
          main.style.visibility = "hidden";
          for (const element of [document.documentElement, document.body]) {
            element.style.setProperty("background", "transparent", "important");
          }
          return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
        }
      }
      return null;
    });
    await shot("options", { clip: box, omitBackground: true });
    await page.keyboard.press("Escape");
    await page.setViewportSize({ width: 1280, height: 820 });
  },
};

await page.goto(base);
await page.getByRole("textbox", { name: "Username" }).fill(USER);
await page.getByRole("textbox", { name: "Password" }).fill(PASSWORD);
await page.getByRole("button", { name: "Log in" }).click();
await page.waitForURL((url) => !url.pathname.startsWith("/auth"), { timeout: 30000 });
await settle();

for (const [name, run] of Object.entries(shots)) {
  if (!only || only === name) {
    try {
      await run();
    } catch (err) {
      console.error(`${name}: ${err.message.split("\n").slice(0, 12).join("\n")}`);
      await page.screenshot({ path: `${out}/${name}-failed.png` });
    }
  }
}
await browser.close();
