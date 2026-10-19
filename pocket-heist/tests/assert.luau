-- Minimal assertions for the Lune runner. Messages carry both values so a failure
-- reads without re-running under a debugger.

local Assert = {}

function Assert.equal(actual: any, expected: any, label: string?)
	if actual ~= expected then
		error(`{label or "value"}: expected {tostring(expected)}, got {tostring(actual)}`, 2)
	end
end

function Assert.truthy(value: any, label: string?)
	if not value then
		error(`{label or "value"}: expected truthy, got {tostring(value)}`, 2)
	end
end

function Assert.near(actual: number, expected: number, tolerance: number, label: string?)
	if math.abs(actual - expected) > tolerance then
		error(`{label or "value"}: expected {expected} ± {tolerance}, got {actual}`, 2)
	end
end

return Assert
