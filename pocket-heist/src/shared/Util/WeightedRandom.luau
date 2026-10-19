--!strict
-- Weighted pick over any list. The random source is injected so tests are
-- deterministic and the server can pass a seeded Random:NextNumber.

local WeightedRandom = {}

function WeightedRandom.pick<T>(items: { T }, weightOf: (T) -> number, random: (() -> number)?): T?
	local total = 0
	for _, item in items do
		total += math.max(0, weightOf(item))
	end
	if total <= 0 then
		return nil
	end

	local roll = (if random then random() else math.random()) * total
	local last: T? = nil
	for _, item in items do
		local weight = math.max(0, weightOf(item))
		if weight > 0 then
			last = item
			roll -= weight
			if roll < 0 then
				return item
			end
		end
	end
	-- Floating-point remainder when roll lands on exactly total.
	return last
end

return WeightedRandom
