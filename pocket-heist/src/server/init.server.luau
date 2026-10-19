-- Server entry point. Every service is a ModuleScript exposing Init() and Start().
-- Init runs for all services first (wire up state, no yielding), then Start
-- (connect events, spawn loops), so no service starts against a half-built peer.

local ReplicatedStorage = game:GetService("ReplicatedStorage")

-- Create RemoteEvents before any client can ask for them.
require(ReplicatedStorage.Shared.Remotes)

local Services = script.Services

-- Order matters: data loads before plots are assigned, plots exist before
-- anything spawns onto them.
local ORDER = {
	"DataService",
	"LeaderstatsService",
	"PlotService",
	"ConveyorService",
	"IncomeService",
	"LockService",
	"StealService",
	"RebirthService",
}

local loaded = {}
for _, name in ORDER do
	loaded[name] = require(Services:WaitForChild(name))
end

for _, name in ORDER do
	loaded[name].Init(loaded)
end

for _, name in ORDER do
	task.spawn(loaded[name].Start)
end

print(`[PocketHeist] server started with {#ORDER} services`)
