--!strict
-- Rarity tiers in ascending order. Weights are relative spawn odds on the conveyor;
-- they do not need to sum to 100. Colours are RGB 0-255 so this file stays free of
-- Roblox types and can be tested under Lune.

export type Rarity = {
	name: string,
	order: number,
	weight: number,
	color: { number },
}

local Rarities: { Rarity } = {
	{ name = "Common", order = 1, weight = 60, color = { 170, 170, 170 } },
	{ name = "Uncommon", order = 2, weight = 25, color = { 85, 200, 95 } },
	{ name = "Rare", order = 3, weight = 10, color = { 60, 140, 255 } },
	{ name = "Epic", order = 4, weight = 4, color = { 170, 80, 255 } },
	{ name = "Legendary", order = 5, weight = 0.9, color = { 255, 175, 40 } },
	{ name = "Mythic", order = 6, weight = 0.09, color = { 255, 60, 90 } },
	{ name = "Secret", order = 7, weight = 0.01, color = { 20, 20, 20 } },
}

local byName: { [string]: Rarity } = {}
for _, rarity in Rarities do
	byName[rarity.name] = rarity
end

return {
	list = Rarities,
	byName = byName,
}
