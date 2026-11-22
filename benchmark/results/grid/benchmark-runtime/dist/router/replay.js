import { readFileSync } from "node:fs";
import { rankPairs } from "./joint-optimizer.js";
const inputs = JSON.parse(readFileSync(0, "utf8"));
process.stdout.write(JSON.stringify(inputs.map(rankPairs)));
