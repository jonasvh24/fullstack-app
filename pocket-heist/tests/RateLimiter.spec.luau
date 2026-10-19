local Assert = require("./assert")
local RateLimiter = require("../src/shared/Util/RateLimiter")

return {
	["allows a burst up to capacity, then refuses"] = function()
		local t = 0
		local limiter = RateLimiter.new(3, 1, function()
			return t
		end)
		Assert.truthy(limiter:allow("p1"))
		Assert.truthy(limiter:allow("p1"))
		Assert.truthy(limiter:allow("p1"))
		Assert.equal(limiter:allow("p1"), false, "4th call")
	end,

	["refills over time and never above capacity"] = function()
		local t = 0
		local limiter = RateLimiter.new(2, 1, function()
			return t
		end)
		limiter:allow("p1")
		limiter:allow("p1")
		Assert.equal(limiter:allow("p1"), false)
		t = 1
		Assert.truthy(limiter:allow("p1"), "after 1s")
		t = 100
		Assert.truthy(limiter:allow("p1"))
		Assert.truthy(limiter:allow("p1"))
		Assert.equal(limiter:allow("p1"), false, "capped at capacity")
	end,

	["keys are independent and forget resets"] = function()
		local t = 0
		local limiter = RateLimiter.new(1, 0, function()
			return t
		end)
		Assert.truthy(limiter:allow("p1"))
		Assert.truthy(limiter:allow("p2"))
		Assert.equal(limiter:allow("p1"), false)
		limiter:forget("p1")
		Assert.truthy(limiter:allow("p1"), "after forget")
	end,
}
