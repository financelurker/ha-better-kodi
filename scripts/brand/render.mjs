// Export the brand icons from docs/brand/*.svg to custom_components/better_kodi/brand/.
//
//   node scripts/brand/render.mjs
//
// PLAYWRIGHT_MODULE: path to playwright's index.mjs if it isn't installed next to this repo.
import { resolve } from "node:path";

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ?? "playwright");
const source = resolve("docs/brand");
const target = resolve("custom_components/better_kodi/brand");

const browser = await chromium.launch();
for (const name of ["icon", "dark_icon"]) {
  for (const [size, suffix] of [[256, ""], [512, "@2x"]]) {
    const page = await browser.newPage({ viewport: { width: size, height: size } });
    await page.goto(`file://${source}/${name}.svg`);
    await page.evaluate((s) => {
      const svg = document.querySelector("svg");
      svg.setAttribute("width", s);
      svg.setAttribute("height", s);
    }, size);
    await page.screenshot({ path: `${target}/${name}${suffix}.png`, omitBackground: true });
    await page.close();
  }
}
await browser.close();
