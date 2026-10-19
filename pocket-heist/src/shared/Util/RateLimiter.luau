--!strict
-- Per-key token bucket for RemoteEvent handlers. Exploiters can fire remotes at
-- any rate; this caps how often one player's intent is even considered.
-- The clock is injected so tests do not depend on wall time.

export type RateLimiter = {
	allow: (self: RateLimiter, key: any) -> boolean,
	forget: (self: RateLimiter, key: any) -> (),
}

local RateLimiter = {}

function RateLimiter.new(capacity: number, refillPerSec: number, clock: (() -> number)?): RateLimiter
	local now = clock or os.clock
	local buckets: { [any]: { tokens: number, updated: number } } = {}

	local limiter = {}

	function limiter.allow(_self: RateLimiter, key: any): boolean
		local t = now()
		local bucket = buckets[key]
		if not bucket then
			bucket = { tokens = capacity, updated = t }
			buckets[key] = bucket
		end
		bucket.tokens = math.min(capacity, bucket.tokens + (t - bucket.updated) * refillPerSec)
		bucket.updated = t
		if bucket.tokens >= 1 then
			bucket.tokens -= 1
			return true
		end
		return false
	end

	-- Call on PlayerRemoving so the table does not grow for the server's lifetime.
	function limiter.forget(_self: RateLimiter, key: any)
		buckets[key] = nil
	end

	return (limiter :: any) :: RateLimiter
end

return RateLimiter
