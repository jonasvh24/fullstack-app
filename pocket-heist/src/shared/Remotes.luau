-- Single source of truth for RemoteEvent names. The server creates them; clients
-- wait for them. Clients only ever send intents; the server decides outcomes.

local ReplicatedStorage = game:GetService("ReplicatedStorage")
local RunService = game:GetService("RunService")

local NAMES = {
	-- client -> server intents
	"BuyCritter",
	"StealCritter",
	"LockBase",
	"Rebirth",
	-- server -> client feedback
	"Notify",
}

local folder: Folder
if RunService:IsServer() then
	folder = Instance.new("Folder")
	folder.Name = "Remotes"
	for _, name in NAMES do
		local remote = Instance.new("RemoteEvent")
		remote.Name = name
		remote.Parent = folder
	end
	folder.Parent = ReplicatedStorage
else
	folder = ReplicatedStorage:WaitForChild("Remotes") :: Folder
end

local Remotes = {}

function Remotes.get(name: string): RemoteEvent
	local remote = folder:FindFirstChild(name)
	assert(remote and remote:IsA("RemoteEvent"), `Unknown remote "{name}"`)
	return remote
end

return Remotes
