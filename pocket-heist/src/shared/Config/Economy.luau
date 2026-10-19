--!strict
-- Tunable numbers and the pure maths built on them. No Roblox APIs here, so every
-- formula is covered by Lune tests before it reaches a live server.

local Economy = {}

Economy.STARTING_CASH = 100
Economy.AUTOSAVE_INTERVAL = 60 -- seconds; DataStore write budget is per-minute

-- World layout
Economy.MAX_PLOTS = 8 -- one per player; server size is capped to match
Economy.SLOTS_PER_PLOT = 10

-- Conveyor
Economy.CONVEYOR_SPAWN_INTERVAL = 3 -- seconds between Critters
Economy.CONVEYOR_SPEED = 8 -- studs per second

-- Stealing and defence
Economy.INTERACT_RANGE = 12 -- studs; server rejects prompts from further away
Economy.LOCK_DURATION = 60 -- seconds a base stays sealed
Economy.LOCK_COOLDOWN = 15 -- gap after a lock expires, so raiders always get a window

-- Rebirth
Economy.REBIRTH_BASE_COST = 1_000_000
Economy.REBIRTH_COST_GROWTH = 3
Economy.REBIRTH_MULTIPLIER_STEP = 0.5

function Economy.rebirthCost(rebirths: number): number
	return Economy.REBIRTH_BASE_COST * Economy.REBIRTH_COST_GROWTH ^ rebirths
end

-- Applied to all passive income. Linear rather than compounding: compounding
-- multipliers on top of a 3x cost curve make late rebirths trivial.
function Economy.incomeMultiplier(rebirths: number): number
	return 1 + Economy.REBIRTH_MULTIPLIER_STEP * rebirths
end

function Economy.incomePerSec(baseIncomes: { number }, rebirths: number): number
	local total = 0
	for _, income in baseIncomes do
		total += income
	end
	return total * Economy.incomeMultiplier(rebirths)
end

function Economy.canAfford(cash: number, cost: number): boolean
	return cost >= 0 and cash >= cost
end

return Economy
