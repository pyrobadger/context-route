import { readFileSync } from "node:fs";
import { policyVersion, rankPairs } from "./joint-optimizer.js";
if (process.argv.includes("--policy-version")) {
    process.stdout.write(policyVersion);
}
else {
    const inputs = JSON.parse(readFileSync(0, "utf8"));
    process.stdout.write(JSON.stringify(inputs.map(rankPairs)));
}
