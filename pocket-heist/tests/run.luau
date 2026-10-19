-- Headless test runner: `lune run tests/run` from the project root.
-- Covers the pure modules in src/shared; anything touching Roblox APIs is
-- verified by a Studio playtest instead (see README).

local process = require("@lune/process")

local SPECS = {
	Economy = require("./Economy.spec"),
	Format = require("./Format.spec"),
	RateLimiter = require("./RateLimiter.spec"),
	WeightedRandom = require("./WeightedRandom.spec"),
}

local passed, failed = 0, 0
local suiteNames = {}
for name in SPECS do
	table.insert(suiteNames, name)
end
table.sort(suiteNames)

for _, suiteName in suiteNames do
	local cases = SPECS[suiteName]
	local caseNames = {}
	for name in cases do
		table.insert(caseNames, name)
	end
	table.sort(caseNames)

	for _, caseName in caseNames do
		local ok, err = pcall(cases[caseName])
		if ok then
			passed += 1
			print(`  PASS  {suiteName}: {caseName}`)
		else
			failed += 1
			print(`  FAIL  {suiteName}: {caseName}\n        {err}`)
		end
	end
end

print(`\n{passed} passed, {failed} failed`)
process.exit(if failed == 0 then 0 else 1)
