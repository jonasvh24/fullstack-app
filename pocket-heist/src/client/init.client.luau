-- Client entry point. Controllers render state and forward input as intents;
-- they never decide outcomes. Same Init/Start contract as the server.

local Controllers = script.Controllers

local ORDER = {
	"HudController",
	"PromptController",
	"RebirthController",
}

local loaded = {}
for _, name in ORDER do
	loaded[name] = require(Controllers:WaitForChild(name))
end

for _, name in ORDER do
	loaded[name].Init(loaded)
end

for _, name in ORDER do
	task.spawn(loaded[name].Start)
end
