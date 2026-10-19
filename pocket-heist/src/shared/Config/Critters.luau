--!strict
-- Every collectible in the game. Original characters only: no third-party meme IP.
-- Balance rule (enforced in tests/Economy.spec.luau): payback time, cost / incomePerSec,
-- never falls as rarity rises, so a rarer Critter is always a longer-term investment.

export type Critter = {
	id: string,
	name: string,
	rarity: string,
	cost: number,
	incomePerSec: number,
}

local Critters: { Critter } = {
	{ id = "pebble_pup", name = "Pebble Pup", rarity = "Common", cost = 25, incomePerSec = 1 },
	{ id = "sock_gremlin", name = "Sock Gremlin", rarity = "Common", cost = 60, incomePerSec = 2 },
	{ id = "toaster_toad", name = "Toaster Toad", rarity = "Uncommon", cost = 250, incomePerSec = 6 },
	{ id = "bubble_bat", name = "Bubble Bat", rarity = "Uncommon", cost = 500, incomePerSec = 11 },
	{ id = "cactus_cobra", name = "Cactus Cobra", rarity = "Rare", cost = 2_500, incomePerSec = 45 },
	{ id = "disco_moth", name = "Disco Moth", rarity = "Rare", cost = 5_000, incomePerSec = 85 },
	{ id = "jelly_yeti", name = "Jelly Yeti", rarity = "Epic", cost = 25_000, incomePerSec = 350 },
	{ id = "turbo_snail", name = "Turbo Snail", rarity = "Epic", cost = 60_000, incomePerSec = 800 },
	{
		id = "lava_lemur",
		name = "Lava Lemur",
		rarity = "Legendary",
		cost = 300_000,
		incomePerSec = 3_500,
	},
	{
		id = "comet_corgi",
		name = "Comet Corgi",
		rarity = "Legendary",
		cost = 750_000,
		incomePerSec = 8_000,
	},
	{
		id = "thunder_walrus",
		name = "Thunder Walrus",
		rarity = "Mythic",
		cost = 5_000_000,
		incomePerSec = 45_000,
	},
	{
		id = "galaxy_goose",
		name = "Galaxy Goose",
		rarity = "Secret",
		cost = 50_000_000,
		incomePerSec = 400_000,
	},
}

local byId: { [string]: Critter } = {}
local byRarity: { [string]: { Critter } } = {}
for _, critter in Critters do
	byId[critter.id] = critter
	byRarity[critter.rarity] = byRarity[critter.rarity] or {}
	table.insert(byRarity[critter.rarity], critter)
end

return {
	list = Critters,
	byId = byId,
	byRarity = byRarity,
}
