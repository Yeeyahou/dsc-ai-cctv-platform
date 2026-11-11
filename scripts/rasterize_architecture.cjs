// Run with Sharp in the document-authoring Node environment and Korean fonts installed.
const fs = require("node:fs");
const path = require("node:path");
const sharp = require("sharp");

const folder = path.resolve(__dirname, "../docs/diagrams");
const sources = fs.readdirSync(folder).filter((name) => name.endsWith("-v1.2.svg")).sort();

(async () => {
  for (const name of sources) {
    await sharp(path.join(folder, name)).png().toFile(path.join(folder, name.replace(/\.svg$/, ".png")));
  }
  process.stdout.write(`Rendered ${sources.length} architecture diagrams\n`);
})().catch((error) => {
  process.stderr.write(`${error}\n`);
  process.exitCode = 1;
});
