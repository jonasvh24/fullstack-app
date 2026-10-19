local Assert = require("./assert")
local WeightedRandom = require("../src/shared/Util/WeightedRandom")

local function fixed(value: number)
	return function()
		return value
	end
end

local items = {
	{ id = "a", weight = 1 },
	{ id = "b", weight = 3 },
	{ id = "zero", weight = 0 },
}
local function weightOf(item)
	return item.weight
end

return {
	["roll maps onto cumulative weight bands"] = function()
		Assert.equal((WeightedRandom.pick(items, weightOf, fixed(0)) :: any).id, "a")
		Assert.equal((WeightedRandom.pick(items, weightOf, fixed(0.24)) :: any).id, "a")
		Assert.equal((WeightedRandom.pick(items, weightOf, fixed(0.25)) :: any).id, "b")
		Assert.equal((WeightedRandom.pick(items, weightOf, fixed(0.99)) :: any).id, "b")
	end,

	["zero-weight items are never picked, even on a roll of 1"] = function()
		Assert.equal((WeightedRandom.pick(items, weightOf, fixed(1)) :: any).id, "b")
	end,

	["empty or all-zero lists return nil"] = function()
		Assert.equal(WeightedRandom.pick({}, weightOf, fixed(0.5)), nil)
		Assert.equal(WeightedRandom.pick({ { weight = 0 } }, weightOf, fixed(0.5)), nil)
	end,

	["distribution matches weights over many rolls"] = function()
		math.randomseed(42)
		local counts = { a = 0, b = 0 }
		local n = 40_000
		for _ = 1, n do
			local item = WeightedRandom.pick(items, weightOf) :: any
			counts[item.id] += 1
		end
		Assert.near(counts.a / n, 0.25, 0.01, "share of a")
	end,
}
