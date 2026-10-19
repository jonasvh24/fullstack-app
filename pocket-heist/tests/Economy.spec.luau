local Assert = require("./assert")
local Critters = require("../src/shared/Config/Critters")
local Economy = require("../src/shared/Config/Economy")
local Rarities = require("../src/shared/Config/Rarities")

return {
	["every Critter has a known rarity and positive numbers"] = function()
		for _, critter in Critters.list do
			Assert.truthy(Rarities.byName[critter.rarity], `{critter.id} rarity`)
			Assert.truthy(critter.cost > 0, `{critter.id} cost`)
			Assert.truthy(critter.incomePerSec > 0, `{critter.id} income`)
		end
	end,

	["ids are unique"] = function()
		local seen = {}
		for _, critter in Critters.list do
			Assert.equal(seen[critter.id], nil, `duplicate {critter.id}`)
			seen[critter.id] = true
		end
	end,

	["every rarity has at least one Critter"] = function()
		for _, rarity in Rarities.list do
			Assert.truthy(Critters.byRarity[rarity.name], `{rarity.name} has Critters`)
		end
	end,

	["payback time never falls as rarity rises, and stays between 10s and 10min"] = function()
		local sorted = table.clone(Critters.list)
		table.sort(sorted, function(a, b)
			local ra, rb = Rarities.byName[a.rarity].order, Rarities.byName[b.rarity].order
			if ra ~= rb then
				return ra < rb
			end
			return a.cost < b.cost
		end)
		local previous = 0
		for _, critter in sorted do
			local payback = critter.cost / critter.incomePerSec
			Assert.truthy(payback >= previous, `{critter.id} payback {payback}s < {previous}s`)
			Assert.truthy(payback >= 10 and payback <= 600, `{critter.id} payback {payback}s`)
			previous = payback
		end
	end,

	["rarity weights fall as rarity rises"] = function()
		for i = 2, #Rarities.list do
			Assert.truthy(Rarities.list[i].weight < Rarities.list[i - 1].weight, Rarities.list[i].name)
		end
	end,

	["rebirth cost grows geometrically, multiplier linearly"] = function()
		Assert.equal(Economy.rebirthCost(0), 1_000_000)
		Assert.equal(Economy.rebirthCost(2), 9_000_000)
		Assert.equal(Economy.incomeMultiplier(0), 1)
		Assert.equal(Economy.incomeMultiplier(3), 2.5)
	end,

	["plot income sums base incomes then applies the multiplier"] = function()
		Assert.equal(Economy.incomePerSec({ 1, 2, 45 }, 0), 48)
		Assert.equal(Economy.incomePerSec({ 1, 2, 45 }, 2), 96)
		Assert.equal(Economy.incomePerSec({}, 5), 0)
	end,

	["canAfford rejects negative costs"] = function()
		Assert.truthy(Economy.canAfford(100, 100))
		Assert.equal(Economy.canAfford(99, 100), false)
		Assert.equal(Economy.canAfford(100, -1), false)
	end,

	["a lock leaves raiders a window"] = function()
		Assert.truthy(Economy.LOCK_COOLDOWN > 0)
	end,
}
