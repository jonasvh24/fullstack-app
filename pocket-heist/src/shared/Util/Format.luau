--!strict
-- Number formatting for the HUD. Values are floored, never rounded: showing $1M
-- when a player holds $999.95K would tell them they can afford a $1M Critter.

local SUFFIXES = { "", "K", "M", "B", "T", "Qa", "Qi" }

local Format = {}

function Format.abbreviate(n: number): string
	local sign = if n < 0 then "-" else ""
	n = math.abs(n)

	if n < 1000 then
		return sign .. tostring(math.floor(n))
	end

	local tier = math.min(math.floor(math.log10(n) / 3), #SUFFIXES - 1)
	-- Divide once by 10^(3*tier - 1) to get tenths directly; avoids float drift from n / 1000 * 10.
	local tenths = math.floor(n / 10 ^ (3 * tier - 1))
	local whole = tenths // 10
	local decimal = tenths % 10

	local body = if decimal == 0 then tostring(whole) else `{whole}.{decimal}`
	return sign .. body .. SUFFIXES[tier + 1]
end

function Format.cash(n: number): string
	return "$" .. Format.abbreviate(n)
end

function Format.perSecond(n: number): string
	return Format.cash(n) .. "/s"
end

function Format.duration(seconds: number): string
	seconds = math.max(0, math.floor(seconds))
	return string.format("%d:%02d", seconds // 60, seconds % 60)
end

return Format
