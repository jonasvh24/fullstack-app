local Assert = require("./assert")
local Format = require("../src/shared/Util/Format")

return {
	["small numbers stay whole"] = function()
		Assert.equal(Format.abbreviate(0), "0")
		Assert.equal(Format.abbreviate(7.9), "7")
		Assert.equal(Format.abbreviate(999), "999")
	end,

	["thousands and up get a suffix with one decimal"] = function()
		Assert.equal(Format.abbreviate(1000), "1K")
		Assert.equal(Format.abbreviate(1234), "1.2K")
		Assert.equal(Format.abbreviate(1_300), "1.3K")
		Assert.equal(Format.abbreviate(45_000), "45K")
		Assert.equal(Format.abbreviate(5_600_000), "5.6M")
		Assert.equal(Format.abbreviate(50_000_000), "50M")
		Assert.equal(Format.abbreviate(7_100_000_000), "7.1B")
	end,

	["floors instead of rounding so the HUD never overstates cash"] = function()
		Assert.equal(Format.abbreviate(999_950), "999.9K")
		Assert.equal(Format.abbreviate(1_999), "1.9K")
	end,

	["negative values keep their sign"] = function()
		Assert.equal(Format.abbreviate(-2_500), "-2.5K")
	end,

	["beyond the last suffix stays on the last suffix"] = function()
		Assert.equal(Format.abbreviate(1e24), "1000000Qi")
	end,

	["cash, rate and duration helpers"] = function()
		Assert.equal(Format.cash(2_500), "$2.5K")
		Assert.equal(Format.perSecond(350), "$350/s")
		Assert.equal(Format.duration(60), "1:00")
		Assert.equal(Format.duration(9.7), "0:09")
		Assert.equal(Format.duration(-5), "0:00")
	end,
}
